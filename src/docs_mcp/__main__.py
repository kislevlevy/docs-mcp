"""CLI: docs-mcp serve | sync | index | warmup | search"""

from __future__ import annotations

import argparse
import json
import sys

from .config import settings


def _serve() -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    from .access import AccessControl
    from .server import mcp

    app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        json_response=True,  # single JSON body per request; no SSE framing needed for search
        stateless_http=True,  # no session state to keep for a read-only index
        # Origin/Host handling lives in AccessControl so an empty allow-list stays deployable.
        # `transport_security=None` is NOT "disabled" - the SDK auto-enables its own
        # DNS-rebinding Host check (locked to 127.0.0.1/localhost/::1) whenever `host`
        # isn't passed, which it never is here. Must disable explicitly or every
        # non-localhost BIND_ADDR gets 421'd before AccessControl ever runs.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
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


def _search(query: str, limit: int, source: str) -> None:
    """Query the index from the command line - the quickest way to sanity-check retrieval."""
    from . import store

    if not settings.db_path.exists():
        print(f"no index at {settings.db_path} - run: docs-mcp sync")
        return
    db = store.connect(settings.db_path, read_only=True)
    if not store.schema_ready(db):
        print(
            f"no index at {settings.db_path} - build one first:\n  docker compose run --rm indexer"
        )
        return
    available = store.known_sources(db)
    if source not in available:
        print(
            f"unknown source {source!r}; available: {', '.join(available) or '(none)'}"
        )
        return
    hits = store.search(db, query, sources=[source], limit=limit)
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

    sync = sub.add_parser("sync", help="reconcile sources.toml and update the index")
    sync.add_argument("--source", help="limit synchronization to one configured source")
    sync.add_argument(
        "--rebuild", action="store_true", help="re-embed all configured sources"
    )
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="show the plan without changing files or the database",
    )
    sync.add_argument(
        "--quiet", action="store_true", help="only print the final report"
    )

    index = sub.add_parser("index", help="index new and changed docs")
    index.add_argument(
        "--force", action="store_true", help="re-embed everything, ignoring hashes"
    )
    index.add_argument("--source", help="limit to one source")
    index.add_argument("--quiet", action="store_true")

    materialize = sub.add_parser(
        "materialize", help="materialize local PDF/DOCX files without indexing"
    )
    materialize.add_argument("--source", help="limit to one configured source")
    materialize.add_argument("--force", action="store_true", help="rebuild unchanged rich documents")
    materialize.add_argument("--strict", action="store_true", help="treat extraction warnings as failures")

    inspect = sub.add_parser("inspect", help="inspect a rich document or generated topic")
    inspect.add_argument("source")
    inspect.add_argument("path")
    inspect.add_argument("--json", action="store_true", dest="as_json")

    sub.add_parser(
        "warmup", help="download and load the models (used at image build time)"
    )

    search = sub.add_parser("search", help="query the index from the shell")
    search.add_argument("query", nargs="+")
    search.add_argument("-n", "--limit", type=int, default=5)
    search.add_argument("--source", required=True)

    args = parser.parse_args(argv)

    try:
        if args.command == "serve":
            _serve()
        elif args.command == "sync":
            from .sync import format_report
            from .sync import sync as run_sync

            result = run_sync(
                source=args.source,
                rebuild=args.rebuild,
                dry_run=args.dry_run,
                quiet=args.quiet,
            )
            print(format_report(result))
            return result.exit_code
        elif args.command == "index":
            print(
                "Warning: 'index' is a compatibility command; use 'docs-mcp sync'.",
                file=sys.stderr,
            )
            from .indexer import reindex

            stats = reindex(force=args.force, only=args.source, quiet=args.quiet)
            return 1 if stats.failed else 0
        elif args.command == "warmup":
            from .embed import warmup

            warmup()
        elif args.command == "materialize":
            from .materialize import materialize_configured

            results = materialize_configured(
                source=args.source, force=args.force, strict=args.strict
            )
            failed = False
            for name, result in results.items():
                stats = result.stats
                failed = failed or bool(stats.failed)
                print(
                    f"{name}: +{stats.added} ~{stats.changed} -{stats.removed} "
                    f"={stats.unchanged} unchanged; {stats.topics} topics, "
                    f"{stats.pages} pages, {stats.blocks} blocks, "
                    f"{stats.ocr_pages} OCR pages, {stats.warnings} warnings, "
                    f"{stats.elapsed_seconds:.2f}s"
                    + (f", {stats.failed} failed" if stats.failed else "")
                )
                if stats.parser_identities:
                    print(f"  parsers: {', '.join(stats.parser_identities)}")
                for error in stats.errors:
                    print(f"  ! {error}", file=sys.stderr)
            return 1 if failed else 0
        elif args.command == "inspect":
            from .materialize import inspect_materialization

            result = inspect_materialization(args.source, args.path)
            if args.as_json:
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            elif "markdown" in result:
                metadata = {key: value for key, value in result.items() if key != "markdown"}
                print(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True))
                print("\n---\n")
                print(result["markdown"], end="")
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        elif args.command == "search":
            _search(" ".join(args.query), args.limit, args.source)
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
