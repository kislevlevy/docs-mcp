"""MCP server: four tools over the hybrid index, Streamable HTTP, stateless.

Everything is served out of the SQLite index — the server never touches the docs
tree, so there is no path-traversal surface and the container needs no docs mount.
"""

from __future__ import annotations

import sqlite3
import threading

import anyio.to_thread
from mcp.server import MCPServer
from mcp.server.caching import CacheHint
from pydantic import BaseModel, Field

from . import store
from .config import settings

HEALTH_PATH = "/health"

# ---------------------------------------------------------------- db handles

_local = threading.local()


def db() -> sqlite3.Connection:
    """One read-only connection per thread; searches run in a worker thread."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = store.connect(settings.db_path, read_only=True)
        _local.conn = conn
    return conn


EMPTY_INDEX_HINT = "The documentation index is empty. Run the indexer on the server: docker compose run --rm indexer"


def _indexed() -> bool:
    conn = db()
    if not store.schema_ready(conn):
        return False
    return conn.execute("SELECT 1 FROM files LIMIT 1").fetchone() is not None


# ---------------------------------------------------------------- result models


class SourceInfo(BaseModel):
    source: str = Field(description="Identifier to pass to search_docs(sources=[...]).")
    files: int
    chunks: int
    indexed_at: str | None = Field(
        default=None, description="UTC timestamp of the last index run."
    )
    sample_titles: list[str] = Field(
        default_factory=list, description="A few document titles, to convey scope."
    )


class SourceList(BaseModel):
    sources: list[SourceInfo]
    total_files: int
    total_chunks: int


class SearchHit(BaseModel):
    chunk_id: int = Field(
        description="Pass to fetch_chunk to widen context around this hit."
    )
    source: str
    path: str = Field(
        description="Path within the source; pass to fetch_doc for the whole document."
    )
    heading_path: str = Field(
        description="Heading breadcrumb locating this passage in its document."
    )
    score: float
    text: str


class SearchResults(BaseModel):
    query: str
    hits: list[SearchHit]


class ChunkPassage(BaseModel):
    chunk_id: int
    source: str
    path: str
    heading_path: str
    text: str


class ChunkResult(BaseModel):
    passages: list[ChunkPassage]


class DocResult(BaseModel):
    source: str
    path: str
    title: str
    text: str
    offset: int
    total_chars: int
    next_offset: int | None = Field(
        default=None, description="Pass as offset to continue; null when complete."
    )


# ---------------------------------------------------------------- server

mcp = MCPServer(
    "docs",
    title="Documentation Search",
    version="0.1.0",
    instructions=(
        "Searchable third-party documentation. Call list_sources first to see which libraries are "
        "available, then search_docs with a natural-language question (optionally narrowed to a "
        "source). Search is hybrid: exact identifiers like prefetch_count work as well as prose. "
        "Use fetch_chunk to widen context around a hit and fetch_doc to read a whole page."
    ),
    # Content changes only when the indexer runs, so let clients cache aggressively.
    cache_hints={
        "tools/list": CacheHint(ttl_ms=600_000, scope="public"),
        "resources/list": CacheHint(ttl_ms=600_000, scope="public"),
        "resources/templates/list": CacheHint(ttl_ms=600_000, scope="public"),
        "server/discover": CacheHint(ttl_ms=600_000, scope="public"),
        "resources/read": CacheHint(ttl_ms=300_000, scope="public"),
    },
)


@mcp.tool()
async def list_sources() -> SourceList:
    """List the documentation sets available to search.

    Call this first. Each entry's `source` is the filter value for search_docs.
    """
    rows = await anyio.to_thread.run_sync(lambda: store.list_sources(db()))
    if not rows:
        raise ValueError(EMPTY_INDEX_HINT)
    sources = [SourceInfo(**row) for row in rows]
    return SourceList(
        sources=sources,
        total_files=sum(s.files for s in sources),
        total_chunks=sum(s.chunks for s in sources),
    )


@mcp.tool()
async def search_docs(
    query: str,
    sources: list[str] | None = None,
    limit: int = settings.default_limit,
) -> SearchResults:
    """Search the documentation and return the most relevant passages.

    Combines keyword and semantic matching, so both natural-language questions
    ("how do I retry a failed task") and exact identifiers ("acks_late",
    "x-death") work. Narrow with `sources` when you know the library; leave it
    empty to search everything.
    """
    limit = max(1, min(limit, 50))
    if not query.strip():
        return SearchResults(query=query, hits=[])
    hits, ready = await anyio.to_thread.run_sync(
        lambda: (
            store.search(db(), query, sources=sources or None, limit=limit),
            _indexed(),
        )
    )
    if not hits and not ready:
        raise ValueError(EMPTY_INDEX_HINT)
    return SearchResults(
        query=query,
        hits=[
            SearchHit(
                chunk_id=h.chunk_id,
                source=h.source,
                path=h.path,
                heading_path=h.heading_path,
                score=h.score,
                text=h.text,
            )
            for h in hits
        ],
    )


@mcp.tool()
async def fetch_chunk(chunk_id: int, context: int = 1) -> ChunkResult:
    """Read a passage found by search_docs, plus `context` neighbouring passages.

    Cheaper than fetch_doc when a hit is nearly right but cut off mid-explanation.
    """
    context = max(0, min(context, 5))
    rows = await anyio.to_thread.run_sync(
        lambda: store.get_chunk(db(), chunk_id, context)
    )
    if not rows:
        raise ValueError(f"No such chunk_id: {chunk_id}")
    return ChunkResult(
        passages=[
            ChunkPassage(
                chunk_id=int(r["id"]),
                source=r["source"],
                path=r["rel_path"],
                heading_path=r["heading_path"],
                text=r["text"],
            )
            for r in rows
        ]
    )


@mcp.tool()
async def fetch_doc(
    source: str, path: str, offset: int = 0, max_chars: int = 40_000
) -> DocResult:
    """Read a whole documentation page, using `source` and `path` from a search hit.

    Long pages are paginated: when `next_offset` is not null, call again with it
    to continue.
    """
    max_chars = max(1_000, min(max_chars, 200_000))
    offset = max(0, offset)

    def load() -> tuple[sqlite3.Row, str]:
        row = store.get_document(db(), source, path)
        if row is None:
            raise ValueError(
                f"No such document: {source}/{path} (use search_docs to find valid paths)"
            )
        return row, store.document_text(db(), int(row["id"]))

    row, text = await anyio.to_thread.run_sync(load)
    window = text[offset : offset + max_chars]
    end = offset + len(window)
    return DocResult(
        source=source,
        path=path,
        title=row["title"] or path,
        text=window,
        offset=offset,
        total_chars=len(text),
        next_offset=end if end < len(text) else None,
    )


@mcp.resource("docs://{source}/{+path}", mime_type="text/markdown")
async def doc_resource(source: str, path: str) -> str:
    """A documentation page, addressed as docs://<source>/<path>."""

    def load() -> str:
        row = store.get_document(db(), source, path)
        if row is None:
            raise ValueError(f"No such document: {source}/{path}")
        return store.document_text(db(), int(row["id"]))

    return await anyio.to_thread.run_sync(load)


@mcp.custom_route(HEALTH_PATH, methods=["GET"])
async def health(_request):  # noqa: ANN001 - starlette Request
    from starlette.responses import JSONResponse

    ready = await anyio.to_thread.run_sync(_indexed)
    return JSONResponse(
        {"status": "ok" if ready else "empty-index"}, status_code=200 if ready else 503
    )
