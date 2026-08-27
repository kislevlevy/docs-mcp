"""Incremental indexing.

Sources are discovered by convention: every directory under `docs/` is a source
named after the directory (a trailing `-docs` is dropped). Drop a folder in, run
the indexer, it is searchable.

Re-indexing is a content-hash diff, so an update only re-embeds what actually
changed. Editing one file in a 2000-file corpus costs one file's worth of work.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import sqlite3
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

from . import embed, store
from .chunk import embedding_text, split_document, split_materialized_document
from .config import settings
from .formats import walk_supported
from .materialize import materialize_source
from .materialized import IndexDocument, discover_index_documents
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
    materialization_failed: int = 0
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
        origin_path=rel_path,
        origin_media_type={
            ".md": "text/markdown",
            ".mdx": "text/mdx",
            ".rst": "text/x-rst",
            ".txt": "text/plain",
        }.get(path.suffix.lower(), "text/plain"),
    )
    return len(chunks)


def _prepare_document(document: IndexDocument) -> tuple[str, bytes, str | None, list]:
    raw = document.filesystem_path.read_bytes()
    if b"\x00" in raw:
        raise ValueError("binary content in a text document")
    text = raw.decode("utf-8")
    if document.section_id is None:
        title, chunks = split_document(text, document.filesystem_path.suffix)
        title = title or document.title
    else:
        kinds = tuple(str(value) for value in document.metadata.get("content_kinds", []))
        title, chunks = split_materialized_document(
            text,
            default_page=document.page_start,
            content_kinds=kinds,
        )
        title = document.title or title
        manifest_heading = " > ".join(
            str(value) for value in document.metadata.get("heading_path", [])
        )
        if manifest_heading:
            chunks = [
                replace(chunk, heading_path=manifest_heading) for chunk in chunks
            ]
    searchable_metadata = {
        "origin_path": document.origin_path,
        "origin_media_type": document.origin_media_type,
        "section_id": document.section_id,
        "page_start": document.page_start,
        "page_end": document.page_end,
        "heading_path": document.metadata.get("heading_path", []),
        "search_aliases": document.metadata.get("search_aliases", []),
        "materialization_fingerprint": document.metadata.get("materialization_fingerprint"),
    }
    digest = hashlib.sha256()
    digest.update(raw)
    digest.update(json.dumps(searchable_metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())
    return digest.hexdigest(), raw, title, chunks


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
    _, stats.skipped = walk_supported(root, spec.type)
    if spec.type == "local":
        materialization = materialize_source(
            spec.name,
            root,
            settings.materialized_dir,
            force=force,
        )
        stats.materialization_failed = materialization.stats.failed
        if materialization.stats.errors:
            stats.error = "; ".join(materialization.stats.errors)[:500]
    documents = discover_index_documents(
        spec.name, root, settings.materialized_dir
    )
    if progress:
        progress(10, f"indexing 0/{len(documents)} documents")
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
        for position, document in enumerate(documents, 1):
            rel_path = document.logical_path
            seen.add(rel_path)
            sha, raw, title, chunks = _prepare_document(document)
            previous = known.get(rel_path)
            if previous and previous["sha256"] == sha and not force:
                stats.unchanged += 1
                if progress:
                    progress(
                        10 + int(position / max(1, len(documents)) * 85),
                        f"indexing {position}/{len(documents)} documents",
                    )
                continue
            vectors = (
                embed.embed_passages(
                    embedding_text(spec.name, c.heading_path, c.text) for c in chunks
                )
                if chunks
                else []
            )
            pickle.dump(
                (document, sha, title, len(raw), chunks, vectors),
                candidates,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
            if progress:
                progress(
                    10 + int(position / max(1, len(documents)) * 85),
                    f"indexing {position}/{len(documents)} documents",
                )
    except Exception as exc:
        db.rollback()
        stats.failed = 1
        stats.error = (
            f"{rel_path}: {type(exc).__name__}: {exc}"
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
                document, sha, title, size, chunks, vectors = pickle.load(candidates)
            except EOFError:
                break
            rel_path = document.logical_path
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
                origin_path=document.origin_path,
                origin_media_type=document.origin_media_type,
                section_id=document.section_id,
                page_start=document.page_start,
                page_end=document.page_end,
                page_labels=document.metadata.get("page_labels", []),
                extraction_methods=document.metadata.get("extraction_methods", []),
                warnings=document.metadata.get("warnings", []),
                materialization_fingerprint=document.metadata.get("materialization_fingerprint"),
                content_kinds=document.metadata.get("content_kinds", []),
                search_aliases=document.metadata.get("search_aliases", []),
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
