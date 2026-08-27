"""Incremental rich-document materialization and atomic publication."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pypdf

from .config import RICH_DOCUMENT_EXTENSIONS, settings
from .document import (
    MANIFEST_VERSION,
    MARKDOWN_RENDERER_VERSION,
    MATERIALIZATION_FORMAT_VERSION,
    NORMALIZATION_VERSION,
    SEGMENTATION_VERSION,
    MaterializationStats,
    ResourceLimits,
)
from .formats import walk_supported
from .materialized import (
    BlockLocation,
    MaterializedDocument,
    MaterializedManifest,
    ParserIdentity,
    load_manifest,
    manifest_json,
)
from .normalize import slugify
from .parsers import parse_document
from .quality import MaterializationError
from .render_markdown import render_topic
from .segment import segment_topics


@dataclass(frozen=True, slots=True)
class MaterializeResult:
    stats: MaterializationStats
    failed_origins: tuple[str, ...] = ()


def configured_limits() -> ResourceLimits:
    return ResourceLimits(
        max_input_bytes=settings.max_rich_bytes,
        max_pdf_pages=settings.max_pdf_pages,
        max_extracted_characters=settings.max_extracted_chars,
        max_docx_entries=settings.max_docx_entries,
        max_docx_expanded_bytes=settings.max_docx_expanded_bytes,
        max_rendered_pixels=settings.max_rendered_pixels,
        max_processing_seconds=settings.max_rich_processing_seconds,
    )


def stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pipeline_fingerprint(suffix: str) -> str:
    backend = (
        f"pypdf:{getattr(pypdf, '__version__', 'unknown')}"
        if suffix.lower() == ".pdf"
        else "docx-ooxml:1"
    )
    payload = {
        "manifest": MANIFEST_VERSION,
        "format": MATERIALIZATION_FORMAT_VERSION,
        "normalization": NORMALIZATION_VERSION,
        "segmentation": SEGMENTATION_VERSION,
        "renderer": MARKDOWN_RENDERER_VERSION,
        "backend": backend,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _output_directory(materialized_root: Path, source: str, origin_path: str) -> Path:
    relative = PurePosixPath(origin_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("unsafe rich-document origin path")
    return materialized_root / source / Path(*relative.parts)


def _filename(topic, media_type: str) -> str:
    slug = slugify(topic.title)
    if media_type == "application/pdf":
        first = topic.blocks[0]
        if topic.title.startswith("Untitled section (pages "):
            pages = [block.page for block in topic.blocks if block.page is not None]
            return f"p{min(pages):04d}-p{max(pages):04d}-untitled-section.md"
        return f"{topic.section_id}-{slug}.md"
    return f"{topic.section_id}-{slug}.md"


def _location(block) -> BlockLocation:
    return BlockLocation(
        page=block.page,
        document_index=block.index if block.page is None else None,
        index=block.page_block_index,
    )


def _build_manifest(source: str, origin_path: str, origin_sha: str, path: Path, target: Path) -> MaterializedManifest:
    parsed = parse_document(path, configured_limits())
    topics = segment_topics(parsed)
    documents: list[MaterializedDocument] = []
    used_paths: set[str] = set()
    for topic in topics:
        filename = _filename(topic, parsed.media_type)
        if filename in used_paths:
            raise MaterializationError("duplicate_topic_path", f"duplicate generated path: {filename}")
        used_paths.add(filename)
        markdown = render_topic(topic).encode("utf-8")
        (target / filename).write_bytes(markdown)
        pages = [block.page for block in topic.blocks if block.page is not None]
        labels = list(dict.fromkeys(block.page_label for block in topic.blocks if block.page_label))
        documents.append(
            MaterializedDocument(
                path=filename,
                section_id=topic.section_id,
                title=topic.title,
                heading_path=list(topic.heading_path),
                page_start=min(pages) if pages else None,
                page_end=max(pages) if pages else None,
                page_labels=labels,
                block_start=_location(topic.blocks[0]),
                block_end=_location(topic.blocks[-1]),
                block_count=len(topic.blocks),
                section_confidence=topic.confidence,
                content_kinds=sorted({block.kind for block in topic.blocks}),
                extraction_methods=sorted({block.method for block in topic.blocks}),
                warnings=sorted(
                    {warning.code for warning in topic.warnings}
                    | {
                        warning.code
                        for block in topic.blocks
                        for warning in block.warnings
                    }
                ),
                content_sha256=hashlib.sha256(markdown).hexdigest(),
            )
        )
    manifest = MaterializedManifest(
        source=source,
        origin_path=origin_path,
        origin_media_type=parsed.media_type,
        origin_sha256=origin_sha,
        page_count=parsed.page_count,
        parser=ParserIdentity(name=parsed.parser_name, version=parsed.parser_version),
        pipeline_fingerprint=pipeline_fingerprint(path.suffix),
        warnings=sorted(warning.code for warning in parsed.warnings),
        ocr_pages=sorted(
            {
                block.page
                for block in parsed.blocks
                if block.method == "ocr" and block.page is not None
            }
        ),
        documents=documents,
    )
    (target / "_manifest.json").write_bytes(manifest_json(manifest))
    load_manifest(target, expected_source=source, expected_origin=origin_path)
    return manifest


def _atomic_publish(candidate: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(destination.name + ".previous")
    if backup.exists():
        shutil.rmtree(backup)
    if destination.exists():
        os.replace(destination, backup)
    try:
        os.replace(candidate, destination)
    except Exception:
        if backup.exists():
            os.replace(backup, destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def materialize_file(
    source: str,
    root: Path,
    path: Path,
    materialized_root: Path,
    *,
    force: bool = False,
    strict: bool = False,
) -> tuple[str, MaterializedManifest]:
    origin_path = path.relative_to(root).as_posix()
    origin_sha = stream_sha256(path)
    destination = _output_directory(materialized_root, source, origin_path)
    if destination.is_dir() and not force:
        try:
            current = load_manifest(destination, expected_source=source, expected_origin=origin_path)
        except ValueError:
            current = None
        if current and current.origin_sha256 == origin_sha and current.pipeline_fingerprint == pipeline_fingerprint(path.suffix):
            if strict and (current.warnings or any(document.warnings for document in current.documents)):
                raise MaterializationError("strict_warning", "existing materialization contains warnings")
            return "unchanged", current

    temp_root = materialized_root.parent / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    candidate = Path(tempfile.mkdtemp(prefix=f"materialize-{source}-", dir=temp_root))
    try:
        manifest = _build_manifest(source, origin_path, origin_sha, path, candidate)
        if strict and (manifest.warnings or any(document.warnings for document in manifest.documents)):
            raise MaterializationError("strict_warning", "materialization produced warnings")
        action = "changed" if destination.exists() else "added"
        _atomic_publish(candidate, destination)
        return action, manifest
    finally:
        if candidate.exists():
            shutil.rmtree(candidate, ignore_errors=True)


def _failure_path(materialized_root: Path, source: str) -> Path:
    return materialized_root / source / "_failures.json"


def _write_failures(materialized_root: Path, source: str, failures: dict[str, dict]) -> None:
    path = _failure_path(materialized_root, source)
    if not failures:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(failures, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def read_failures(materialized_root: Path, source: str) -> dict[str, dict]:
    path = _failure_path(materialized_root, source)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def materialize_source(
    source: str,
    root: Path,
    materialized_root: Path | None = None,
    *,
    force: bool = False,
    strict: bool = False,
) -> MaterializeResult:
    started = time.monotonic()
    materialized_root = (materialized_root or settings.materialized_dir).expanduser().resolve()
    files, _ = walk_supported(root, "local")
    rich_files = [path for path in files if path.suffix.lower() in RICH_DOCUMENT_EXTENSIONS]
    stats = MaterializationStats()
    failures = read_failures(materialized_root, source)
    seen: set[str] = set()
    failed_origins: list[str] = []
    for path in rich_files:
        origin = path.relative_to(root).as_posix()
        seen.add(origin)
        try:
            action, manifest = materialize_file(source, root, path, materialized_root, force=force, strict=strict)
            setattr(stats, action, getattr(stats, action) + 1)
            stats.topics += len(manifest.documents)
            stats.pages += manifest.page_count or 0
            stats.blocks += sum(
                document.block_count
                or max(1, document.block_end.index - document.block_start.index + 1)
                for document in manifest.documents
            )
            stats.warnings += len(manifest.warnings) + sum(len(document.warnings) for document in manifest.documents)
            stats.ocr_pages += len(manifest.ocr_pages)
            stats.parser_identities.append(
                f"{manifest.parser.name}@{manifest.parser.version}"
            )
            failures.pop(origin, None)
        except Exception as exc:
            stats.failed += 1
            code = exc.code if isinstance(exc, MaterializationError) else "materialization_failed"
            message = str(exc)[:500] or type(exc).__name__
            try:
                input_sha = stream_sha256(path)
            except OSError:
                input_sha = ""
            failures[origin] = {
                "input_sha256": input_sha,
                "error_code": code,
                "message": message,
                "attempted_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            stats.errors.append(f"{origin}: {code}: {message}")
            failed_origins.append(origin)

    source_root = materialized_root / source
    if source_root.is_dir():
        for manifest_path in sorted(source_root.rglob("_manifest.json")):
            try:
                manifest = load_manifest(manifest_path.parent, expected_source=source)
            except ValueError:
                continue
            if manifest.origin_path not in seen:
                shutil.rmtree(manifest_path.parent)
                stats.removed += 1
                failures.pop(manifest.origin_path, None)
        # Prune empty mirrored parent directories, but retain source failure state.
        for directory in sorted((entry for entry in source_root.rglob("*") if entry.is_dir()), key=lambda p: len(p.parts), reverse=True):
            if not any(directory.iterdir()):
                directory.rmdir()
    for origin in set(failures) - seen:
        failures.pop(origin, None)
    _write_failures(materialized_root, source, failures)
    stats.parser_identities = sorted(set(stats.parser_identities))
    stats.elapsed_seconds = time.monotonic() - started
    return MaterializeResult(stats, tuple(failed_origins))


def inspect_materialization(source: str, path: str, materialized_root: Path | None = None) -> dict:
    root = (materialized_root or settings.materialized_dir).expanduser().resolve()
    source_root = root / source
    requested = PurePosixPath(path)
    if requested.is_absolute() or any(part in {"", ".", ".."} for part in requested.parts):
        raise ValueError("unsafe inspection path")
    origin_directory = source_root / Path(*requested.parts)
    if (origin_directory / "_manifest.json").is_file():
        manifest = load_manifest(origin_directory, expected_source=source, expected_origin=path)
        result = manifest.model_dump(mode="json")
        failure = read_failures(root, source).get(path)
        if failure:
            result["last_failed_attempt"] = failure
        return result
    for manifest_path in sorted(source_root.rglob("_manifest.json")) if source_root.is_dir() else []:
        manifest = load_manifest(manifest_path.parent, expected_source=source)
        prefix = f"{manifest.origin_path}/"
        if path.startswith(prefix):
            generated = path[len(prefix) :]
            entry = next((document for document in manifest.documents if document.path == generated), None)
            if entry is not None:
                return {
                    "source": source,
                    "origin_path": manifest.origin_path,
                    "origin_media_type": manifest.origin_media_type,
                    "parser": manifest.parser.model_dump(),
                    "pipeline_fingerprint": manifest.pipeline_fingerprint,
                    "document": entry.model_dump(mode="json"),
                    "markdown": (manifest_path.parent / entry.path).read_text(encoding="utf-8"),
                }
    failure = read_failures(root, source).get(path)
    if failure:
        return {"source": source, "origin_path": path, "failure": failure}
    raise ValueError(f"no materialization found: {source}/{path}")


def _materialize_configured_unlocked(
    *, source: str | None = None, force: bool = False, strict: bool = False
) -> dict[str, MaterializeResult]:
    """Acquire configured sources and update materialized output without indexing."""
    from .acquire import acquire
    from .sources import load

    config = load()
    assert config is not None
    available = {spec.name for spec in config.sources}
    if source and source not in available:
        raise ValueError(
            f"Unknown source {source!r}; configured sources: {', '.join(sorted(available)) or '(none)'}"
        )
    results: dict[str, MaterializeResult] = {}
    for spec in config.sources:
        if source and spec.name != source:
            continue
        if spec.type != "local":
            results[spec.name] = MaterializeResult(MaterializationStats())
            continue
        origin = Path(spec.origin).expanduser().resolve()
        output_root = settings.materialized_dir.expanduser().resolve()
        try:
            output_root.relative_to(origin)
            overlaps = True
        except ValueError:
            overlaps = False
        try:
            origin.relative_to(output_root)
            overlaps = True
        except ValueError:
            pass
        if overlaps:
            raise ValueError(
                f"MATERIALIZED_DIR must not contain or be contained by local source '{spec.name}'"
            )
        snapshot = acquire(spec, settings.state_dir)
        try:
            results[spec.name] = materialize_source(
                spec.name,
                snapshot.path,
                settings.materialized_dir,
                force=force,
                strict=strict,
            )
        finally:
            if snapshot.path.exists():
                shutil.rmtree(snapshot.path)
    return results


def materialize_configured(
    *, source: str | None = None, force: bool = False, strict: bool = False
) -> dict[str, MaterializeResult]:
    """Serialize standalone materialization with the normal sync workflow."""
    lock_path = settings.state_dir.expanduser().resolve() / "locks" / "sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            return _materialize_configured_unlocked(
                source=source, force=force, strict=strict
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
