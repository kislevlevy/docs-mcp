"""SQLite index: FTS5 for lexical, sqlite-vec for dense, RRF to fuse them.

One file, no services. WAL mode lets the indexer write while the server keeps
serving reads.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import sqlite_vec

from . import embed
from .config import DOC_EXTENSIONS, RICH_DOCUMENT_EXTENSIONS, settings
from .document import (
    MANIFEST_VERSION,
    MARKDOWN_RENDERER_VERSION,
    MATERIALIZATION_FORMAT_VERSION,
    NORMALIZATION_VERSION,
    SEGMENTATION_VERSION,
)

SCHEMA_VERSION = 3
PIPELINE_VERSION = 2


def pipeline_fingerprint() -> str:
    payload = {
        "version": PIPELINE_VERSION,
        "dense_model": settings.dense_model,
        "chunk_target": settings.chunk_target,
        "chunk_max": settings.chunk_max,
        "chunk_min": settings.chunk_min,
        "stub_max": settings.stub_max,
        "text_extensions": sorted(DOC_EXTENSIONS),
        "rich_extensions": sorted(RICH_DOCUMENT_EXTENSIONS),
        "manifest_version": MANIFEST_VERSION,
        "materialization_format": MATERIALIZATION_FORMAT_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "segmentation_version": SEGMENTATION_VERSION,
        "markdown_renderer_version": MARKDOWN_RENDERER_VERSION,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class Hit:
    chunk_id: int
    source: str
    path: str
    heading_path: str
    title: str
    text: str
    score: float
    origin_path: str | None = None
    origin_media_type: str | None = None
    section_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    page_labels: tuple[str, ...] = ()
    content_kinds: tuple[str, ...] = ()
    extraction_methods: tuple[str, ...] = ()


def connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if not read_only:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    missing_read_only = read_only and not db_path.exists()
    if missing_read_only:
        # Let a freshly deployed server report an empty index. Its connection is
        # replaced when the atomically published database file appears.
        db = sqlite3.connect(":memory:", timeout=30.0, check_same_thread=False)
    elif read_only:
        db = sqlite3.connect(
            f"file:{db_path.resolve()}?mode=ro",
            uri=True,
            timeout=30.0,
            check_same_thread=False,
        )
    else:
        db = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    if not read_only:
        db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA synchronous=NORMAL")
    if read_only and not missing_read_only:
        # Read-only without the read-only-WAL file-permission trap.
        db.execute("PRAGMA query_only=ON")
    return db


def create_schema(db: sqlite3.Connection, dim: int) -> None:
    existing_version = get_meta(db, "schema_version")
    if existing_version is not None and existing_version != str(SCHEMA_VERSION):
        raise RuntimeError(
            f"index schema {existing_version} cannot be updated in place; "
            "run 'docs-mcp sync --rebuild'"
        )
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS sources (
            id                   INTEGER PRIMARY KEY,
            name                 TEXT NOT NULL UNIQUE COLLATE NOCASE,
            type                 TEXT NOT NULL CHECK(type IN ('git', 'local')),
            origin               TEXT NOT NULL,
            ref                  TEXT,
            source_directory     TEXT,
            description          TEXT,
            desired_config_hash  TEXT NOT NULL DEFAULT '',
            acquisition_hash     TEXT NOT NULL DEFAULT '',
            indexed_config_hash  TEXT,
            sync_status          TEXT NOT NULL DEFAULT 'unknown',
            index_status         TEXT NOT NULL DEFAULT 'absent',
            indexed_revision     TEXT,
            last_attempt_at      TEXT,
            last_success_at      TEXT,
            last_error_code      TEXT,
            last_error_message   TEXT,
            indexed_files        INTEGER NOT NULL DEFAULT 0,
            indexed_chunks       INTEGER NOT NULL DEFAULT 0,
            created_at           TEXT NOT NULL DEFAULT '',
            updated_at           TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS files (
            id                              INTEGER PRIMARY KEY,
            source                          TEXT NOT NULL,
            source_id                       INTEGER NOT NULL REFERENCES sources(id),
            rel_path                        TEXT NOT NULL,
            sha256                          TEXT NOT NULL,
            title                           TEXT,
            bytes                           INTEGER NOT NULL,
            indexed_at                      TEXT NOT NULL,
            origin_path                     TEXT NOT NULL,
            origin_media_type               TEXT NOT NULL,
            section_id                      TEXT,
            page_start                      INTEGER,
            page_end                        INTEGER,
            page_labels_json                TEXT NOT NULL DEFAULT '[]',
            extraction_methods_json         TEXT NOT NULL DEFAULT '[]',
            warnings_json                   TEXT NOT NULL DEFAULT '[]',
            materialization_fingerprint     TEXT,
            UNIQUE(source_id, rel_path)
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id           INTEGER PRIMARY KEY,
            file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            ord          INTEGER NOT NULL,
            heading_path TEXT NOT NULL DEFAULT '',
            text         TEXT NOT NULL,
            page_start   INTEGER,
            page_end     INTEGER,
            provenance_json TEXT NOT NULL DEFAULT '[]',
            content_kinds_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS chunks_file_ord ON chunks(file_id, ord);

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            chunk_id UNINDEXED,
            source_token,
            text,
            heading_path,
            tokenize='unicode61 remove_diacritics 2'
        );
        """)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(files)")}
    if "source_id" not in columns:
        db.execute(
            "ALTER TABLE files ADD COLUMN source_id INTEGER REFERENCES sources(id)"
        )
    db.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
            chunk_id INTEGER PRIMARY KEY,
            source TEXT PARTITION KEY,
            embedding FLOAT[{dim}]
        )
        """)
    db.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    db.execute(
        "INSERT INTO meta(key, value) VALUES('dim', ?) ON CONFLICT(key) DO NOTHING",
        (str(dim),),
    )
    db.execute(
        "INSERT INTO meta(key, value) VALUES('dense_model', ?) ON CONFLICT(key) DO NOTHING",
        (settings.dense_model,),
    )
    db.execute(
        "INSERT INTO meta(key, value) VALUES('pipeline_fingerprint', ?) ON CONFLICT(key) DO NOTHING",
        (pipeline_fingerprint(),),
    )
    db.commit()


_REQUIRED_TABLES = ("files", "chunks", "chunks_fts", "chunks_vec")


def schema_ready(db: sqlite3.Connection) -> bool:
    """True once the indexer has built the schema.

    A freshly deployed server has an empty volume, and every read path has to say
    "no index yet" rather than raise `no such table`.
    """
    marks = ",".join("?" * len(_REQUIRED_TABLES))
    row = db.execute(
        f"SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name IN ({marks})",
        _REQUIRED_TABLES,
    ).fetchone()
    return int(row["n"]) == len(_REQUIRED_TABLES)


def get_meta(db: sqlite3.Connection, key: str) -> str | None:
    try:
        row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["value"] if row else None


def upsert_source(
    db: sqlite3.Connection,
    *,
    name: str,
    kind: str,
    origin: str,
    ref: str | None,
    directory: str | None,
    description: str | None,
    desired_config_hash: str,
    acquisition_hash: str,
    sync_status: str = "pending",
    index_status: str = "absent",
    indexed_config_hash: str | None = None,
    indexed_revision: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    indexed_files: int = 0,
    indexed_chunks: int = 0,
) -> int:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    existing = db.execute(
        "SELECT id, index_status FROM sources WHERE name = ?", (name,)
    ).fetchone()
    if existing:
        db.execute(
            """UPDATE sources SET type=?, origin=?, ref=?, source_directory=?, description=?,
               desired_config_hash=?, acquisition_hash=?, indexed_config_hash=COALESCE(?, indexed_config_hash),
               sync_status=?, index_status=?, indexed_revision=COALESCE(?, indexed_revision),
               last_error_code=?, last_error_message=?, indexed_files=?, indexed_chunks=?, updated_at=?
               WHERE id=?""",
            (
                kind,
                origin,
                ref,
                directory,
                description,
                desired_config_hash,
                acquisition_hash,
                indexed_config_hash,
                sync_status,
                index_status,
                indexed_revision,
                error_code,
                error_message,
                indexed_files,
                indexed_chunks,
                now,
                existing["id"],
            ),
        )
        return int(existing["id"])
    db.execute(
        """INSERT INTO sources
           (name, type, origin, ref, source_directory, description, desired_config_hash,
            acquisition_hash, indexed_config_hash, sync_status, index_status, indexed_revision,
            last_attempt_at, last_success_at, last_error_code, last_error_message,
            indexed_files, indexed_chunks, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            name,
            kind,
            origin,
            ref,
            directory,
            description,
            desired_config_hash,
            acquisition_hash,
            indexed_config_hash,
            sync_status,
            index_status,
            indexed_revision,
            now,
            None,
            error_code,
            error_message,
            indexed_files,
            indexed_chunks,
            now,
            now,
        ),
    )
    return int(
        db.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()["id"]
    )


def mark_source_attempt(
    db: sqlite3.Connection,
    name: str,
    *,
    status: str,
    index_status: str,
    error_code: str | None = None,
    error_message: str | None = None,
    revision: str | None = None,
    config_hash: str | None = None,
) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    db.execute(
        """UPDATE sources SET sync_status=?, index_status=?, last_attempt_at=?,
           last_success_at=CASE WHEN ? = 'success' THEN ? ELSE last_success_at END,
           indexed_config_hash=COALESCE(?, indexed_config_hash), indexed_revision=COALESCE(?, indexed_revision),
           last_error_code=?, last_error_message=?, updated_at=? WHERE name=?""",
        (
            status,
            index_status,
            now,
            status,
            now,
            config_hash,
            revision,
            error_code,
            error_message,
            now,
            name,
        ),
    )


def source_rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    if not _table_exists(db, "sources"):
        return []
    return db.execute("SELECT * FROM sources ORDER BY name").fetchall()


def delete_source(db: sqlite3.Connection, name: str) -> tuple[int, int]:
    rows = db.execute("SELECT id FROM files WHERE source = ?", (name,)).fetchall()
    files = len(rows)
    chunks = 0
    for row in rows:
        chunks += int(
            db.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE file_id = ?", (row["id"],)
            ).fetchone()["n"]
        )
        delete_file(db, int(row["id"]))
    db.execute("DELETE FROM sources WHERE name = ?", (name,))
    return files, chunks


# ---------------------------------------------------------------- writes


def delete_file(db: sqlite3.Connection, file_id: int) -> None:
    ids = [
        r["id"]
        for r in db.execute("SELECT id FROM chunks WHERE file_id = ?", (file_id,))
    ]
    if ids:
        marks = ",".join("?" * len(ids))
        db.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({marks})", ids)
        db.execute(f"DELETE FROM chunks_vec WHERE chunk_id IN ({marks})", ids)
    db.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
    db.execute("DELETE FROM files WHERE id = ?", (file_id,))


def upsert_file(
    db: sqlite3.Connection,
    *,
    source: str,
    source_id: int | None = None,
    rel_path: str,
    sha256: str,
    title: str | None,
    size: int,
    chunks: Sequence,
    vectors: Sequence[np.ndarray],
    origin_path: str | None = None,
    origin_media_type: str = "text/plain",
    section_id: str | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
    page_labels: Sequence[str] = (),
    extraction_methods: Sequence[str] = (),
    warnings: Sequence[str] = (),
    materialization_fingerprint: str | None = None,
    content_kinds: Sequence[str] = (),
    search_aliases: Sequence[str] = (),
) -> int:
    has_source_token = _fts_has_source_token(db)
    if has_source_token:
        row = db.execute(
            "SELECT id FROM files WHERE source_id = ? AND rel_path = ?",
            (source_id, rel_path),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT id FROM files WHERE source = ? AND rel_path = ?",
            (source, rel_path),
        ).fetchone()
    if row:
        delete_file(db, row["id"])

    if has_source_token and source_id is None:
        raise ValueError("source_id is required by the configured index schema")
    indexed_at = datetime.now(UTC).isoformat(timespec="seconds")
    file_columns = {column["name"] for column in db.execute("PRAGMA table_info(files)")}
    has_provenance = "origin_path" in file_columns
    if has_source_token and has_provenance:
        cursor = db.execute(
            """INSERT INTO files(
                source, source_id, rel_path, sha256, title, bytes, indexed_at,
                origin_path, origin_media_type, section_id, page_start, page_end,
                page_labels_json, extraction_methods_json, warnings_json,
                materialization_fingerprint
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source,
                source_id,
                rel_path,
                sha256,
                title,
                size,
                indexed_at,
                origin_path or rel_path,
                origin_media_type,
                section_id,
                page_start,
                page_end,
                json.dumps(list(page_labels), ensure_ascii=False, separators=(",", ":")),
                json.dumps(list(extraction_methods), ensure_ascii=False, separators=(",", ":")),
                json.dumps(list(warnings), ensure_ascii=False, separators=(",", ":")),
                materialization_fingerprint,
            ),
        )
    elif has_source_token:
        cursor = db.execute(
            "INSERT INTO files(source, source_id, rel_path, sha256, title, bytes, indexed_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (source, source_id, rel_path, sha256, title, size, indexed_at),
        )
    else:
        cursor = db.execute(
            "INSERT INTO files(source, rel_path, sha256, title, bytes, indexed_at) "
            "VALUES(?,?,?,?,?,?)",
            (source, rel_path, sha256, title, size, indexed_at),
        )
    file_id = int(cursor.lastrowid)

    chunk_columns = {column["name"] for column in db.execute("PRAGMA table_info(chunks)")}
    for chunk, vector in zip(chunks, vectors, strict=True):
        if "provenance_json" in chunk_columns:
            chunk_id = int(
                db.execute(
                    """INSERT INTO chunks(
                        file_id, ord, heading_path, text, page_start, page_end,
                        provenance_json, content_kinds_json
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        file_id,
                        chunk.ord,
                        chunk.heading_path,
                        chunk.text,
                        getattr(chunk, "page_start", None),
                        getattr(chunk, "page_end", None),
                        json.dumps(list(getattr(chunk, "provenance", ())), ensure_ascii=False, separators=(",", ":")),
                        json.dumps(list(getattr(chunk, "content_kinds", content_kinds)), ensure_ascii=False, separators=(",", ":")),
                    ),
                ).lastrowid
            )
        else:
            chunk_id = int(
                db.execute(
                    "INSERT INTO chunks(file_id, ord, heading_path, text) VALUES(?,?,?,?)",
                    (file_id, chunk.ord, chunk.heading_path, chunk.text),
                ).lastrowid
            )
        lexical_text = chunk.text
        if search_aliases:
            lexical_text += "\n" + "\n".join(search_aliases)
        if has_source_token:
            db.execute(
                "INSERT INTO chunks_fts(chunk_id, source_token, text, heading_path) VALUES(?,?,?,?)",
                (chunk_id, f"s{source_id}", lexical_text, chunk.heading_path),
            )
        else:
            db.execute(
                "INSERT INTO chunks_fts(chunk_id, text, heading_path) VALUES(?,?,?)",
                (chunk_id, lexical_text, chunk.heading_path),
            )
        db.execute(
            "INSERT INTO chunks_vec(chunk_id, source, embedding) VALUES(?,?,?)",
            (chunk_id, source, vector.astype(np.float32).tobytes()),
        )
    return file_id


# ---------------------------------------------------------------- search

# ``\w`` is Unicode-aware in Python, but also includes underscores.  The FTS5
# ``unicode61`` tokenizer treats underscores as separators, so mirror that shape
# while accepting Hebrew (and other Unicode letters) instead of silently turning
# non-ASCII queries into an empty search.
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Identifier-shaped words: acks_late, x-death, worker.concurrency, celery:beat
_IDENT_RE = re.compile(r"[0-9A-Za-z]+(?:[_\-.:][0-9A-Za-z]+)+")


def phrase_query(query: str) -> str | None:
    """FTS5 phrase clauses for identifier-like words in the query.

    `unicode61` splits `acks_late` into `acks` + `late`, so an OR of those tokens
    drowns the real hit in documents that merely say "late". Searching the adjacent
    pair as a phrase pins the exact identifier instead.
    """
    phrases = []
    for identifier in _IDENT_RE.findall(query):
        tokens = _TOKEN_RE.findall(identifier.lower())
        if len(tokens) >= 2:
            phrases.append('"' + " ".join(tokens) + '"')
    return " OR ".join(dict.fromkeys(phrases)) or None


def fts_query(query: str) -> str | None:
    """Turn free text into a safe FTS5 MATCH expression.

    Anything with a `:`, `-`, `*` or quote in it is a MATCH syntax error, so tokens
    are extracted and quoted. OR (not AND) keeps recall on long questions; bm25
    still ranks documents matching more, rarer terms first.
    """
    tokens = {t.lower() for t in _TOKEN_RE.findall(query) if len(t) > 1}
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in sorted(tokens))


def _lexical(
    db: sqlite3.Connection, query: str, sources: Sequence[str] | None, k: int
) -> list[int]:
    return _match(db, fts_query(query), sources, k)


def _match(
    db: sqlite3.Connection, match: str | None, sources: Sequence[str] | None, k: int
) -> list[int]:
    if match is None:
        return []
    sql = """
        SELECT f.chunk_id AS chunk_id
        FROM chunks_fts f
        JOIN chunks c ON c.id = f.chunk_id
        JOIN files fl ON fl.id = c.file_id
        WHERE chunks_fts MATCH ?
    """
    params: list = [match]
    if sources and _fts_has_source_token(db):
        rows = db.execute(
            f"SELECT id FROM sources WHERE name IN ({','.join('?' * len(sources))})",
            list(sources),
        ).fetchall()
        if not rows:
            return []
        source_clause = " OR ".join(f'source_token:"s{int(row["id"])}"' for row in rows)
        params[0] = f"({source_clause}) AND ({match})"
    if sources:
        sql += f" AND fl.source IN ({','.join('?' * len(sources))})"
        params += list(sources)
    if _fts_has_source_token(db):
        sql += " ORDER BY bm25(chunks_fts, 0.0, 0.0, 1.0, 0.6) LIMIT ?"
    else:
        sql += " ORDER BY bm25(chunks_fts, 1.0, 0.6) LIMIT ?"
    params.append(k)
    return [int(r["chunk_id"]) for r in db.execute(sql, params)]


def _dense(
    db: sqlite3.Connection, query: str, sources: Sequence[str] | None, k: int
) -> list[int]:
    vector = embed.embed_query(query).tobytes()
    # sqlite-vec constrains a partition key with `=`, so multiple sources are
    # separate KNN passes merged by distance.
    partitions: list[str | None] = list(sources) if sources else [None]
    scored: list[tuple[float, int]] = []
    for source in partitions:
        if source is None:
            sql = "SELECT chunk_id, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ?"
            params: list = [vector, k]
        else:
            sql = "SELECT chunk_id, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ? AND source = ?"
            params = [vector, k, source]
        scored += [
            (float(r["distance"]), int(r["chunk_id"])) for r in db.execute(sql, params)
        ]
    scored.sort()
    return [chunk_id for _, chunk_id in scored[:k]]


def _rrf(
    rankings: Sequence[Sequence[int]], k: int, weights: Sequence[float] | None = None
) -> list[tuple[int, float]]:
    """Weighted Reciprocal Rank Fusion.

    Fuses ranks, so BM25's unbounded scale and cosine distance never need to be
    normalised against each other. Weights let a leg whose agreement means more
    (an exact identifier phrase) outvote the fuzzier legs.
    """
    weights = weights or [1.0] * len(rankings)
    scores: dict[int, float] = {}
    for ranking, weight in zip(rankings, weights, strict=True):
        for rank, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def _load(db: sqlite3.Connection, chunk_ids: Sequence[int]) -> dict[int, sqlite3.Row]:
    if not chunk_ids:
        return {}
    marks = ",".join("?" * len(chunk_ids))
    rows = db.execute(
        f"""
        SELECT c.id, c.heading_path, c.text, c.page_start, c.page_end,
               c.content_kinds_json, fl.source, fl.rel_path, fl.title,
               fl.origin_path, fl.origin_media_type, fl.section_id,
               fl.page_labels_json, fl.extraction_methods_json
        FROM chunks c JOIN files fl ON fl.id = c.file_id
        WHERE c.id IN ({marks})
        """,
        list(chunk_ids),
    )
    return {int(r["id"]): r for r in rows}


def search(
    db: sqlite3.Connection,
    query: str,
    *,
    sources: Sequence[str] | None = None,
    limit: int | None = None,
    rerank: bool | None = None,
) -> list[Hit]:
    limit = limit or settings.default_limit
    use_rerank = settings.rerank if rerank is None else rerank

    if not schema_ready(db):
        return []

    # With no searchable token there is nothing to match on, and embedding pure
    # punctuation just returns arbitrary nearest neighbours. Say nothing instead.
    if fts_query(query) is None:
        return []

    # Three legs: exact identifier phrases, individual tokens, and semantics.
    # The phrase leg is empty for ordinary prose queries and costs nothing then.
    phrase_expr = phrase_query(query)
    phrase = _match(db, phrase_expr, sources, settings.candidates)

    # Cross-encoders are trained on natural-language query/passage pairs, so on a
    # bare identifier ("worker_concurrency") they score every candidate as equally
    # irrelevant and their ordering is noise. Measured on this corpus, reranking an
    # identifier query drops MRR from 0.92 to 0.79. Where an exact phrase match is
    # available it is the better evidence, so leave that ordering alone.
    if phrase_expr is not None:
        use_rerank = False

    lexical = _lexical(db, query, sources, settings.candidates)
    dense = _dense(db, query, sources, settings.candidates)
    fused = _rrf(
        [phrase, lexical, dense],
        settings.rrf_k,
        [settings.w_phrase, settings.w_lexical, settings.w_dense],
    )
    if not fused:
        return []

    pool = fused[: max(settings.rerank_pool, limit)] if use_rerank else fused[:limit]
    rows = _load(db, [chunk_id for chunk_id, _ in pool])
    candidates = [
        (chunk_id, score, rows[chunk_id])
        for chunk_id, score in pool
        if chunk_id in rows
    ]

    if use_rerank and candidates:
        texts = [
            f"{r['heading_path']}\n{r['text']}" if r["heading_path"] else r["text"]
            for _, _, r in candidates
        ]
        scores = embed.rerank(query, texts)
        by_rerank = sorted(
            range(len(candidates)), key=lambda i: scores[i], reverse=True
        )

        # Fuse the reranker's ranking with the retrieval ranking rather than letting
        # it replace it. A cross-encoder given a query full of library-specific
        # identifiers scores everything as irrelevant, and its ordering among equally
        # irrelevant passages is noise - which would otherwise bury the one passage
        # that actually contains the term. Fusing keeps strong lexical evidence alive
        # while still letting the reranker promote what it recognises.
        retrieval_order = [chunk_id for chunk_id, _, _ in candidates]
        rerank_order = [candidates[i][0] for i in by_rerank]
        by_id = {chunk_id: row for chunk_id, _, row in candidates}
        candidates = [
            (chunk_id, score, by_id[chunk_id])
            for chunk_id, score in _rrf(
                [retrieval_order, rerank_order],
                settings.rrf_k,
                [settings.w_retrieval, settings.w_rerank],
            )
        ]

    return [
        Hit(
            chunk_id=chunk_id,
            source=row["source"],
            path=row["rel_path"],
            heading_path=row["heading_path"],
            title=row["title"] or row["rel_path"],
            text=row["text"],
            score=round(score, 6),
            origin_path=row["origin_path"],
            origin_media_type=row["origin_media_type"],
            section_id=row["section_id"],
            page_start=row["page_start"],
            page_end=row["page_end"],
            page_labels=tuple(json.loads(row["page_labels_json"])),
            content_kinds=tuple(json.loads(row["content_kinds_json"])),
            extraction_methods=tuple(json.loads(row["extraction_methods_json"])),
        )
        for chunk_id, score, row in candidates[:limit]
    ]


# ---------------------------------------------------------------- reads


def known_sources(db: sqlite3.Connection) -> list[str]:
    """Every indexed source name, for validating a `sources` filter."""
    if not schema_ready(db):
        return []
    return [
        r["source"]
        for r in db.execute("SELECT DISTINCT source FROM files ORDER BY source")
    ]


def list_sources(db: sqlite3.Connection) -> list[dict]:
    if not schema_ready(db):
        return []
    configured = (
        db.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
        if _table_exists(db, "sources")
        else 0
    )
    if configured:
        rows = db.execute(
            """SELECT s.name AS source, s.description, s.sync_status, s.index_status,
                      COUNT(DISTINCT f.id) AS files,
                      COUNT(c.id) AS chunks,
                      COALESCE(s.last_success_at, MAX(f.indexed_at)) AS indexed_at
               FROM sources s LEFT JOIN files f ON f.source_id=s.id
               LEFT JOIN chunks c ON c.file_id=f.id GROUP BY s.id ORDER BY s.name"""
        ).fetchall()
        out = []
        for row in rows:
            titles = [
                r["title"]
                for r in db.execute(
                    "SELECT title FROM files WHERE source_id=(SELECT id FROM sources WHERE name=?) "
                    "AND title IS NOT NULL ORDER BY LENGTH(rel_path), rel_path LIMIT 12",
                    (row["source"],),
                )
            ]
            out.append(
                {
                    "source": row["source"],
                    "files": row["files"],
                    "chunks": row["chunks"],
                    "indexed_at": row["indexed_at"],
                    "sample_titles": titles,
                    "description": row["description"],
                    "sync_status": row["sync_status"],
                    "index_status": row["index_status"],
                }
            )
        return out
    rows = db.execute("""
        SELECT fl.source AS source,
               COUNT(DISTINCT fl.id) AS files,
               COUNT(c.id)           AS chunks,
               MAX(fl.indexed_at)    AS indexed_at
        FROM files fl LEFT JOIN chunks c ON c.file_id = fl.id
        GROUP BY fl.source ORDER BY fl.source
        """).fetchall()
    out = []
    for row in rows:
        titles = [
            r["title"]
            for r in db.execute(
                "SELECT title FROM files WHERE source = ? AND title IS NOT NULL"
                " ORDER BY LENGTH(rel_path), rel_path LIMIT 12",
                (row["source"],),
            )
        ]
        out.append(
            {
                "source": row["source"],
                "files": row["files"],
                "chunks": row["chunks"],
                "indexed_at": row["indexed_at"],
                "sample_titles": titles,
            }
        )
    return out


def get_chunk(
    db: sqlite3.Connection, source: str, chunk_id: int, context: int = 0
) -> list[sqlite3.Row]:
    if not schema_ready(db):
        return []
    row = db.execute(
        """
        SELECT c.id, c.ord, c.file_id, c.heading_path, c.text,
               c.page_start, c.page_end, c.provenance_json, c.content_kinds_json,
               fl.source, fl.rel_path, fl.title, fl.origin_path,
               fl.origin_media_type, fl.section_id, fl.page_labels_json,
               fl.extraction_methods_json, fl.warnings_json
        FROM chunks c JOIN files fl ON fl.id = c.file_id
        WHERE fl.source = ? AND c.id = ?
        """,
        (source, chunk_id),
    ).fetchone()
    if row is None:
        return []
    if context <= 0:
        return [row]
    return db.execute(
        """
        SELECT c.id, c.ord, c.file_id, c.heading_path, c.text,
               c.page_start, c.page_end, c.provenance_json, c.content_kinds_json,
               fl.source, fl.rel_path, fl.title, fl.origin_path,
               fl.origin_media_type, fl.section_id, fl.page_labels_json,
               fl.extraction_methods_json, fl.warnings_json
        FROM chunks c JOIN files fl ON fl.id = c.file_id
        WHERE c.file_id = ? AND c.ord BETWEEN ? AND ? ORDER BY c.ord
        """,
        (row["file_id"], row["ord"] - context, row["ord"] + context),
    ).fetchall()


def get_document(db: sqlite3.Connection, source: str, path: str) -> sqlite3.Row | None:
    if not schema_ready(db):
        return None
    return db.execute(
        "SELECT * FROM files WHERE source = ? AND rel_path = ?", (source, path)
    ).fetchone()


def document_text(db: sqlite3.Connection, file_id: int) -> str:
    rows = db.execute(
        "SELECT text FROM chunks WHERE file_id = ? ORDER BY ord", (file_id,)
    )
    return "\n\n".join(r["text"] for r in rows)


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _fts_has_source_token(db: sqlite3.Connection) -> bool:
    return any(
        row["name"] == "source_token"
        for row in db.execute("PRAGMA table_info(chunks_fts)")
    )


def validate_database(db: sqlite3.Connection) -> None:
    """Reject a rebuild candidate with corruption or index-table orphans."""
    if not schema_ready(db) or get_meta(db, "schema_version") != str(SCHEMA_VERSION):
        raise RuntimeError("rebuilt database schema is incomplete")
    if get_meta(db, "pipeline_fingerprint") != pipeline_fingerprint():
        raise RuntimeError("rebuilt database has the wrong pipeline fingerprint")
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"rebuilt database failed integrity_check: {integrity}")
    for table in ("chunks_fts", "chunks_vec"):
        count = db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE chunk_id NOT IN (SELECT id FROM chunks)"
        ).fetchone()[0]
        if count:
            raise RuntimeError(f"rebuilt database has {count} orphan rows in {table}")
        indexed = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if indexed != chunks:
            raise RuntimeError(
                f"rebuilt database has {indexed} rows in {table} for {chunks} chunks"
            )
    mismatched = db.execute("""SELECT COUNT(*) FROM sources s WHERE
           s.indexed_files != (SELECT COUNT(*) FROM files f WHERE f.source_id=s.id)
           OR s.indexed_chunks != (
               SELECT COUNT(*) FROM chunks c JOIN files f ON f.id=c.file_id
               WHERE f.source_id=s.id
           )""").fetchone()[0]
    if mismatched:
        raise RuntimeError(
            f"rebuilt database has incorrect counts for {mismatched} sources"
        )
