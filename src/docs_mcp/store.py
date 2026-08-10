"""SQLite index: FTS5 for lexical, sqlite-vec for dense, RRF to fuse them.

One file, no services. WAL mode lets the indexer write while the server keeps
serving reads.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import sqlite_vec

from . import embed
from .config import settings

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Hit:
    chunk_id: int
    source: str
    path: str
    heading_path: str
    title: str
    text: str
    score: float


def connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA synchronous=NORMAL")
    if read_only:
        # Read-only without the read-only-WAL file-permission trap.
        db.execute("PRAGMA query_only=ON")
    return db


def create_schema(db: sqlite3.Connection, dim: int) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS files (
            id         INTEGER PRIMARY KEY,
            source     TEXT NOT NULL,
            rel_path   TEXT NOT NULL,
            sha256     TEXT NOT NULL,
            title      TEXT,
            bytes      INTEGER NOT NULL,
            indexed_at TEXT NOT NULL,
            UNIQUE(source, rel_path)
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id           INTEGER PRIMARY KEY,
            file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            ord          INTEGER NOT NULL,
            heading_path TEXT NOT NULL DEFAULT '',
            text         TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS chunks_file_ord ON chunks(file_id, ord);

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            chunk_id UNINDEXED,
            text,
            heading_path,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    db.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
            chunk_id INTEGER PRIMARY KEY,
            source TEXT PARTITION KEY,
            embedding FLOAT[{dim}]
        )
        """
    )
    db.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    db.execute(
        "INSERT INTO meta(key, value) VALUES('dim', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(dim),),
    )
    db.execute(
        "INSERT INTO meta(key, value) VALUES('dense_model', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (settings.dense_model,),
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


# ---------------------------------------------------------------- writes

def delete_file(db: sqlite3.Connection, file_id: int) -> None:
    ids = [r["id"] for r in db.execute("SELECT id FROM chunks WHERE file_id = ?", (file_id,))]
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
    rel_path: str,
    sha256: str,
    title: str | None,
    size: int,
    chunks: Sequence,
    vectors: Sequence[np.ndarray],
) -> int:
    row = db.execute("SELECT id FROM files WHERE source = ? AND rel_path = ?", (source, rel_path)).fetchone()
    if row:
        delete_file(db, row["id"])

    cursor = db.execute(
        "INSERT INTO files(source, rel_path, sha256, title, bytes, indexed_at) VALUES(?,?,?,?,?,?)",
        (source, rel_path, sha256, title, size, datetime.now(UTC).isoformat(timespec="seconds")),
    )
    file_id = int(cursor.lastrowid)

    for chunk, vector in zip(chunks, vectors, strict=True):
        chunk_id = int(
            db.execute(
                "INSERT INTO chunks(file_id, ord, heading_path, text) VALUES(?,?,?,?)",
                (file_id, chunk.ord, chunk.heading_path, chunk.text),
            ).lastrowid
        )
        db.execute(
            "INSERT INTO chunks_fts(chunk_id, text, heading_path) VALUES(?,?,?)",
            (chunk_id, chunk.text, chunk.heading_path),
        )
        db.execute(
            "INSERT INTO chunks_vec(chunk_id, source, embedding) VALUES(?,?,?)",
            (chunk_id, source, vector.astype(np.float32).tobytes()),
        )
    return file_id


# ---------------------------------------------------------------- search

_TOKEN_RE = re.compile(r"[0-9A-Za-z]+")
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


def _lexical(db: sqlite3.Connection, query: str, sources: Sequence[str] | None, k: int) -> list[int]:
    return _match(db, fts_query(query), sources, k)


def _match(db: sqlite3.Connection, match: str | None, sources: Sequence[str] | None, k: int) -> list[int]:
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
    if sources:
        sql += f" AND fl.source IN ({','.join('?' * len(sources))})"
        params += list(sources)
    sql += " ORDER BY bm25(chunks_fts, 1.0, 0.6) LIMIT ?"
    params.append(k)
    return [int(r["chunk_id"]) for r in db.execute(sql, params)]


def _dense(db: sqlite3.Connection, query: str, sources: Sequence[str] | None, k: int) -> list[int]:
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
        scored += [(float(r["distance"]), int(r["chunk_id"])) for r in db.execute(sql, params)]
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
        SELECT c.id, c.heading_path, c.text, fl.source, fl.rel_path, fl.title
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
    candidates = [(chunk_id, score, rows[chunk_id]) for chunk_id, score in pool if chunk_id in rows]

    if use_rerank and candidates:
        texts = [
            f"{r['heading_path']}\n{r['text']}" if r["heading_path"] else r["text"] for _, _, r in candidates
        ]
        scores = embed.rerank(query, texts)
        by_rerank = sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)

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
                [retrieval_order, rerank_order], settings.rrf_k, [settings.w_retrieval, settings.w_rerank]
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
        )
        for chunk_id, score, row in candidates[:limit]
    ]


# ---------------------------------------------------------------- reads

def list_sources(db: sqlite3.Connection) -> list[dict]:
    if not schema_ready(db):
        return []
    rows = db.execute(
        """
        SELECT fl.source AS source,
               COUNT(DISTINCT fl.id) AS files,
               COUNT(c.id)           AS chunks,
               MAX(fl.indexed_at)    AS indexed_at
        FROM files fl LEFT JOIN chunks c ON c.file_id = fl.id
        GROUP BY fl.source ORDER BY fl.source
        """
    ).fetchall()
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


def get_chunk(db: sqlite3.Connection, chunk_id: int, context: int = 0) -> list[sqlite3.Row]:
    if not schema_ready(db):
        return []
    row = db.execute(
        """
        SELECT c.id, c.ord, c.file_id, c.heading_path, c.text, fl.source, fl.rel_path, fl.title
        FROM chunks c JOIN files fl ON fl.id = c.file_id WHERE c.id = ?
        """,
        (chunk_id,),
    ).fetchone()
    if row is None:
        return []
    if context <= 0:
        return [row]
    return db.execute(
        """
        SELECT c.id, c.ord, c.file_id, c.heading_path, c.text, fl.source, fl.rel_path, fl.title
        FROM chunks c JOIN files fl ON fl.id = c.file_id
        WHERE c.file_id = ? AND c.ord BETWEEN ? AND ? ORDER BY c.ord
        """,
        (row["file_id"], row["ord"] - context, row["ord"] + context),
    ).fetchall()


def get_document(db: sqlite3.Connection, source: str, path: str) -> sqlite3.Row | None:
    if not schema_ready(db):
        return None
    return db.execute("SELECT * FROM files WHERE source = ? AND rel_path = ?", (source, path)).fetchone()


def document_text(db: sqlite3.Connection, file_id: int) -> str:
    rows = db.execute("SELECT text FROM chunks WHERE file_id = ? ORDER BY ord", (file_id,))
    return "\n\n".join(r["text"] for r in rows)
