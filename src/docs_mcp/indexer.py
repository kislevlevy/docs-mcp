"""Incremental indexing.

Sources are discovered by convention: every directory under `docs/` is a source
named after the directory (a trailing `-docs` is dropped). Drop a folder in, run
the indexer, it is searchable.

Re-indexing is a content-hash diff, so an update only re-embeds what actually
changed. Editing one file in a 2000-file corpus costs one file's worth of work.
"""

from __future__ import annotations

import hashlib
import io
import pickle
import sqlite3
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader

from . import embed, store
from .chunk import embedding_text, split_document
from .config import settings
from .formats import walk_supported
from .sources import SourceSpec


@dataclass(slots=True)
class Stats:
    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    chunks: int = 0
    failed: int = 0
    skipped: int = 0
    error: str | None = None


def source_name(directory: Path) -> str:
    name = directory.name
    return (
        name[: -len("-docs")]
        if name.endswith("-docs") and len(name) > len("-docs")
        else name
    )


def discover(docs_dir: Path, only: str | None = None) -> dict[str, Path]:
    if not docs_dir.is_dir():
        raise SystemExit(f"docs directory not found: {docs_dir}")
    sources = {
        source_name(child): child
        for child in sorted(docs_dir.iterdir())
        if child.is_dir() and not child.name.startswith(".")
    }
    if only:
        if only not in sources:
            raise SystemExit(
                f"unknown source {only!r}; available: {', '.join(sources) or '(none)'}"
            )
        return {only: sources[only]}
    return sources


def walk(root: Path) -> list[Path]:
    files, _ = walk_supported(root, "git")
    return files


def _digest(path: Path) -> tuple[str, bytes]:
    raw = path.read_bytes()
    return hashlib.sha256(raw).hexdigest(), raw


def index_file(
    db: sqlite3.Connection,
    source: str,
    source_id: int | None,
    root: Path,
    path: Path,
    sha: str,
    raw: bytes,
) -> int:
    text = raw.decode("utf-8", errors="replace")
    rel_path = path.relative_to(root).as_posix()
    title, chunks = split_document(text, path.suffix)
    vectors = (
        embed.embed_passages(
            embedding_text(source, c.heading_path, c.text) for c in chunks
        )
        if chunks
        else []
    )
    store.upsert_file(
        db,
        source=source,
        source_id=source_id,
        rel_path=rel_path,
        sha256=sha,
        title=title,
        size=len(raw),
        chunks=chunks,
        vectors=vectors,
    )
    return len(chunks)


def _docx_text(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            xml = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ValueError("invalid DOCX container") from exc
    root = ElementTree.fromstring(xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []
    for paragraph in root.findall(".//w:p", ns):
        text = "".join(
            node.text or "" for node in paragraph.findall(".//w:t", ns)
        ).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def _pdf_text(raw: bytes) -> str:
    """Extract text from a native PDF with a real PDF parser."""
    if not raw.startswith(b"%PDF"):
        raise ValueError("invalid PDF signature")
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise ValueError("invalid or unreadable PDF") from exc
    if not text.strip():
        raise ValueError("PDF contains no extractable text")
    return text


def _parse_bytes(spec: SourceSpec, path: Path, raw: bytes) -> tuple[str | None, list]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        text = _docx_text(raw)
        parse_suffix = ".txt"
    elif suffix == ".pdf":
        text = _pdf_text(raw)
        parse_suffix = ".txt"
    else:
        if b"\x00" in raw:
            raise ValueError("binary content in a text document")
        text = raw.decode("utf-8")
        parse_suffix = suffix
    return split_document(text, parse_suffix)


def index_snapshot(
    db: sqlite3.Connection,
    spec: SourceSpec,
    root: Path,
    *,
    source_id: int,
    force: bool = False,
    quiet: bool = False,
    progress: Callable[[int, str], None] | None = None,
) -> Stats:
    """Prepare all changed files first, then publish one source in one transaction."""
    stats = Stats()
    paths, stats.skipped = walk_supported(root, spec.type)
    if progress:
        progress(10, f"indexing 0/{len(paths)} files")
    known = {
        row["rel_path"]: row
        for row in db.execute(
            "SELECT id, rel_path, sha256 FROM files WHERE source = ?", (spec.name,)
        )
    }
    seen: set[str] = set()
    candidates = tempfile.SpooledTemporaryFile(
        max_size=max(1, settings.candidate_memory_mb) * 1024 * 1024
    )
    try:
        for position, path in enumerate(paths, 1):
            rel_path = path.relative_to(root).as_posix()
            seen.add(rel_path)
            sha, raw = _digest(path)
            previous = known.get(rel_path)
            if previous and previous["sha256"] == sha and not force:
                stats.unchanged += 1
                if progress:
                    progress(
                        10 + int(position / max(1, len(paths)) * 85),
                        f"indexing {position}/{len(paths)} files",
                    )
                continue
            title, chunks = _parse_bytes(spec, path, raw)
            vectors = (
                embed.embed_passages(
                    embedding_text(spec.name, c.heading_path, c.text) for c in chunks
                )
                if chunks
                else []
            )
            pickle.dump(
                (rel_path, sha, title, len(raw), chunks, vectors),
                candidates,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
            if progress:
                progress(
                    10 + int(position / max(1, len(paths)) * 85),
                    f"indexing {position}/{len(paths)} files",
                )
    except Exception as exc:
        db.rollback()
        stats.failed = 1
        stats.error = (
            f"{path.relative_to(root).as_posix()}: {type(exc).__name__}: {exc}"
        )
        if progress:
            progress(100, "failed")
        if not quiet:
            print(f"  ! {spec.name}: {type(exc).__name__}: {exc}")
        candidates.close()
        return stats

    try:
        candidates.seek(0)
        while True:
            try:
                rel_path, sha, title, size, chunks, vectors = pickle.load(candidates)
            except EOFError:
                break
            previous = known.get(rel_path)
            store.upsert_file(
                db,
                source=spec.name,
                source_id=source_id,
                rel_path=rel_path,
                sha256=sha,
                title=title,
                size=size,
                chunks=chunks,
                vectors=vectors,
            )
            stats.changed += int(previous is not None)
            stats.added += int(previous is None)
            stats.chunks += len(chunks)
        for rel_path, row in known.items():
            if rel_path not in seen:
                store.delete_file(db, int(row["id"]))
                stats.removed += 1
    except Exception as exc:
        db.rollback()
        stats.failed = 1
        stats.error = f"database publication failed: {type(exc).__name__}: {exc}"
        stats.added = stats.changed = stats.removed = stats.chunks = 0
        if progress:
            progress(100, "failed")
        candidates.close()
        return stats
    candidates.close()
    # The caller publishes source status and these content changes together.
    # It owns the final commit so a crash cannot expose new content with stale
    # source metadata.
    return stats


def reindex(
    *, force: bool = False, only: str | None = None, quiet: bool = False
) -> Stats:
    started = time.monotonic()
    sources = discover(settings.docs_dir, only)

    db = store.connect(settings.db_path)
    legacy_schema = store.get_meta(db, "schema_version") == "1"
    stored_model = store.get_meta(db, "dense_model")
    stored_dim = store.get_meta(db, "dim")
    if stored_model and stored_model != settings.dense_model:
        if legacy_schema:
            raise RuntimeError(
                "embedding model changed on a legacy index; run "
                "'docs-mcp sync --rebuild'"
            )
        # Vectors from a different model are not comparable; rebuild rather than mix.
        print(
            f"embedding model changed ({stored_model} -> {settings.dense_model}); forcing full rebuild"
        )
        force = True
        stored_dim = None
    # Only load the model when we actually need its width, so an all-unchanged run
    # never pays model startup (nor touches the network).
    dim = int(stored_dim) if stored_dim else embed.dimension()
    if not legacy_schema:
        store.create_schema(db, dim)

    stats = Stats()
    for source, root in sources.items():
        source_id = None
        if not legacy_schema:
            source_id = store.upsert_source(
                db,
                name=source,
                kind="local",
                origin=str(root.resolve()),
                ref=None,
                directory=None,
                description=None,
                desired_config_hash="compatibility-index",
                acquisition_hash="compatibility-index",
                sync_status="success",
                index_status="ready",
            )
            db.commit()
        known = {
            row["rel_path"]: row
            for row in db.execute(
                "SELECT id, rel_path, sha256 FROM files WHERE source = ?", (source,)
            )
        }
        seen: set[str] = set()

        for path in walk(root):
            rel_path = path.relative_to(root).as_posix()
            seen.add(rel_path)
            try:
                sha, raw = _digest(path)
            except OSError as exc:
                print(f"  ! {source}/{rel_path}: {exc}")
                stats.failed += 1
                continue

            previous = known.get(rel_path)
            if previous and previous["sha256"] == sha and not force:
                stats.unchanged += 1
                continue

            try:
                count = index_file(db, source, source_id, root, path, sha, raw)
            except Exception as exc:  # one bad file must not abandon the run
                db.rollback()
                print(f"  ! {source}/{rel_path}: {type(exc).__name__}: {exc}")
                stats.failed += 1
                continue

            db.commit()
            stats.chunks += count
            if previous:
                stats.changed += 1
            else:
                stats.added += 1
            if not quiet and (stats.added + stats.changed) % 100 == 0:
                print(
                    f"  ... {stats.added + stats.changed} files, {stats.chunks} chunks"
                )

        for rel_path, row in known.items():
            if rel_path not in seen:
                store.delete_file(db, row["id"])
                stats.removed += 1
        db.commit()

    # Sources whose directory was deleted entirely.
    if only is None:
        for row in db.execute("SELECT DISTINCT source FROM files").fetchall():
            if row["source"] not in sources:
                for stale in db.execute(
                    "SELECT id FROM files WHERE source = ?", (row["source"],)
                ).fetchall():
                    store.delete_file(db, stale["id"])
                    stats.removed += 1
        db.commit()

    db.execute("PRAGMA optimize")
    db.close()

    elapsed = time.monotonic() - started
    print(
        f"+{stats.added} new  ~{stats.changed} changed  -{stats.removed} removed  "
        f"={stats.unchanged} unchanged  |  {stats.chunks} chunks embedded in {elapsed:.1f}s"
        + (f"  |  {stats.failed} failed" if stats.failed else "")
    )
    return stats
