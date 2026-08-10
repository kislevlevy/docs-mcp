"""Incremental indexing.

Sources are discovered by convention: every directory under `docs/` is a source
named after the directory (a trailing `-docs` is dropped). Drop a folder in, run
the indexer, it is searchable.

Re-indexing is a content-hash diff, so an update only re-embeds what actually
changed. Editing one file in a 2000-file corpus costs one file's worth of work.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from . import embed, store
from .chunk import embedding_text, split_document
from .config import DOC_EXTENSIONS, settings


@dataclass(slots=True)
class Stats:
    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    chunks: int = 0
    failed: int = 0


def source_name(directory: Path) -> str:
    name = directory.name
    return name[: -len("-docs")] if name.endswith("-docs") and len(name) > len("-docs") else name


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
            raise SystemExit(f"unknown source {only!r}; available: {', '.join(sources) or '(none)'}")
        return {only: sources[only]}
    return sources


def walk(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in DOC_EXTENSIONS and not p.name.startswith(".")
    )


def _digest(path: Path) -> tuple[str, bytes]:
    raw = path.read_bytes()
    return hashlib.sha256(raw).hexdigest(), raw


def index_file(db: sqlite3.Connection, source: str, root: Path, path: Path, sha: str, raw: bytes) -> int:
    text = raw.decode("utf-8", errors="replace")
    rel_path = path.relative_to(root).as_posix()
    title, chunks = split_document(text, path.suffix)
    vectors = (
        embed.embed_passages(embedding_text(source, c.heading_path, c.text) for c in chunks) if chunks else []
    )
    store.upsert_file(
        db,
        source=source,
        rel_path=rel_path,
        sha256=sha,
        title=title,
        size=len(raw),
        chunks=chunks,
        vectors=vectors,
    )
    return len(chunks)


def reindex(*, force: bool = False, only: str | None = None, quiet: bool = False) -> Stats:
    started = time.monotonic()
    sources = discover(settings.docs_dir, only)

    db = store.connect(settings.db_path)
    stored_model = store.get_meta(db, "dense_model")
    stored_dim = store.get_meta(db, "dim")
    if stored_model and stored_model != settings.dense_model:
        # Vectors from a different model are not comparable; rebuild rather than mix.
        print(f"embedding model changed ({stored_model} -> {settings.dense_model}); forcing full rebuild")
        force = True
        stored_dim = None
    # Only load the model when we actually need its width, so an all-unchanged run
    # never pays model startup (nor touches the network).
    dim = int(stored_dim) if stored_dim else embed.dimension()
    store.create_schema(db, dim)

    stats = Stats()
    for source, root in sources.items():
        known = {
            row["rel_path"]: row
            for row in db.execute("SELECT id, rel_path, sha256 FROM files WHERE source = ?", (source,))
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
                count = index_file(db, source, root, path, sha, raw)
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
                print(f"  ... {stats.added + stats.changed} files, {stats.chunks} chunks")

        for rel_path, row in known.items():
            if rel_path not in seen:
                store.delete_file(db, row["id"])
                stats.removed += 1
        db.commit()

    # Sources whose directory was deleted entirely.
    if only is None:
        for row in db.execute("SELECT DISTINCT source FROM files").fetchall():
            if row["source"] not in sources:
                for stale in db.execute("SELECT id FROM files WHERE source = ?", (row["source"],)).fetchall():
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
