"""CLI: docs-mcp serve | index | warmup | search"""

from __future__ import annotations

import argparse
import sys

from .config import settings


def _serve() -> None:
    import uvicorn

    from .access import AccessControl
    from .server import mcp

    app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        json_response=True,  # single JSON body per request; no SSE framing needed for search
        stateless_http=True,  # no session state to keep for a read-only index
        # Origin/Host handling lives in AccessControl so an empty allow-list stays deployable.
        transport_security=None,
    )
    guarded = AccessControl(
        app,
        token=settings.auth_token,
        allowed_origins=settings.allowed_origins,
        allowed_hosts=settings.allowed_hosts,
    )
    print(
        f"docs-mcp serving http://{settings.host}:{settings.port}{settings.mcp_path}  "
        f"(db={settings.db_path}, rerank={'on' if settings.rerank else 'off'}, "
        f"auth={'bearer' if settings.auth_token else 'none'})",
        flush=True,
    )
    uvicorn.run(
        guarded,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


def _search(query: str, limit: int, source: str | None) -> None:
    """Query the index from the command line - the quickest way to sanity-check retrieval."""
    from . import store

    db = store.connect(settings.db_path, read_only=True)
    if not store.schema_ready(db):
        print(
            f"no index at {settings.db_path} - build one first:\n  docker compose run --rm indexer"
        )
        return
    hits = store.search(db, query, sources=[source] if source else None, limit=limit)
    if not hits:
        print("no results")
        return
    for rank, hit in enumerate(hits, 1):
        snippet = " ".join(hit.text.split())[:160]
        print(f"{rank:2}. [{hit.score:.4f}] {hit.source}/{hit.path}")
        print(f"    {hit.heading_path or '(document root)'}")
        print(f"    {snippet}...\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="docs-mcp", description="Hybrid documentation search over MCP"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the MCP server (Streamable HTTP)")

    index = sub.add_parser("index", help="index new and changed docs")
    index.add_argument(
        "--force", action="store_true", help="re-embed everything, ignoring hashes"
    )
    index.add_argument("--source", help="limit to one source")
    index.add_argument("--quiet", action="store_true")

    sub.add_parser(
        "warmup", help="download and load the models (used at image build time)"
    )

    search = sub.add_parser("search", help="query the index from the shell")
    search.add_argument("query", nargs="+")
    search.add_argument("-n", "--limit", type=int, default=5)
    search.add_argument("--source")

    args = parser.parse_args(argv)

    if args.command == "serve":
        _serve()
    elif args.command == "index":
        from .indexer import reindex

        reindex(force=args.force, only=args.source, quiet=args.quiet)
    elif args.command == "warmup":
        from .embed import warmup

        warmup()
    elif args.command == "search":
        _search(" ".join(args.query), args.limit, args.source)
    return 0


if __name__ == "__main__":
    sys.exit(main())
