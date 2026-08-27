# CLAUDE.md

Guidance for AI agents working in this repo. Read `ABOUT.md` first for what the product is.

## Commands

```bash
uv sync                                   # install (Python 3.13+)
uv run pytest -q                          # unit and retrieval quality gates
uv run docs-mcp search --source rabbitmq "per-message ttl"  # query one source
uv run docs-mcp sync                       # reconcile sources.toml
docker compose up -d --build              # start the server on :8765
docker compose run --rm indexer sync      # fetch and index configured sources
docker compose run --rm indexer sync --rebuild
```

Local runs default to `sources.toml` plus `.docs-mcp/index.db`; override paths when needed:

```bash
export DOCS_DIR=$PWD/docs DB_PATH=/tmp/index.db
uv run docs-mcp index --source fastapi
```

## Layout

| file          | role                                                                         |
| ------------- | ---------------------------------------------------------------------------- |
| `config.py`   | every tunable, all env-driven. `settings` is a frozen module-level singleton |
| `sources.py`  | typed `sources.toml` loading, validation, and canonical hashes              |
| `acquire.py`  | origin-safe Git/local snapshots and supported-file filtering                 |
| `sync.py`     | reconciliation, locking, per-source results, and atomic rebuilds             |
| `chunk.py`    | splits md/mdx/rst into passages. Pure functions, no I/O                      |
| `embed.py`    | fastembed wrappers, lazily loaded and cached per process                     |
| `store.py`    | SQLite schema, writes, and the whole search pipeline                         |
| `indexer.py`  | walks `docs/`, hash-diffs, calls chunk → embed → store                       |
| `server.py`   | the 4 MCP tools + `docs://` resource. Thin — logic lives in `store.py`       |
| `access.py`   | pure-ASGI Origin/Host/bearer checks                                          |
| `__main__.py` | CLI: `serve`, `sync`, compatibility `index`, `warmup`, `search`              |

Data flows one way: `chunk.py` → `embed.py` → `store.py`. `server.py` only reads.

## Non-obvious things that will bite you

**`settings` is read at import time.** Tests must set env vars _before_ `docs_mcp.config` is
imported — that is why `tests/conftest.py` sets them at module top level. `dataclasses.replace`
will not propagate, because modules bind the singleton by reference.

**The search pipeline is three legs, then a fusion.** Order in `store.search()` matters:

1. phrase leg (identifiers only), 2. BM25 leg, 3. vector leg → weighted RRF → optional rerank.
   Do not "simplify" this to one leg. Each covers a measured failure of the others; `acks_late` and
   `worker_concurrency` return _nothing_ without the phrase leg.

**FTS5 input must be sanitized.** A raw `:`, `-`, `*` or quote in a `MATCH` expression is a syntax
error, not a no-op. Always build expressions via `fts_query()` / `phrase_query()`.

**BGE embeddings are asymmetric.** Queries get `QUERY_INSTRUCTION` prepended; passages never do.
Using the same function for both silently degrades recall. Guarded by
`test_query_instruction_is_applied_to_queries_only`.

**Reranking is off by default on purpose.** Measured: no gain on prose, MRR 0.92 → 0.79 on
identifier queries, ~740ms vs ~6ms. Identifier queries bypass it even when `RERANK=1`. Do not
re-enable it by default without new measurements.

**Never split inside a fenced code block.** `chunk.py` tracks fence state via `_fence_mask()`;
every text rewrite must respect it, or code samples get mangled.

**`stub_max` (40 chars) is measured, not guessed.** It separates Hugo redirect stubs from real
short docs. It was originally 160 and silently discarded 145 legitimate documents. Do not raise it
without re-measuring the corpus.

**Changing `DENSE_MODEL` forces a full rebuild** — `indexer.reindex()` detects it via the `dense_model`
row in `meta`. Vectors from two models are not comparable.

**The index lives in a named Docker volume**, not a bind mount: the container runs as non-root
`app`, and a bind-mounted host dir arrives with host ownership, leaving `/data` unwritable on a
fresh Linux clone. Copy it out with `docker compose cp server:/data/index.db ./index.db`.

**Read paths must tolerate an unbuilt index.** A fresh deploy has an empty volume; every read goes
through `store.schema_ready()` first, or you get raw `no such table` errors.

**The server never touches the docs tree.** `fetch_doc` reassembles text from the index, so there
is no filesystem path to traverse. Keep it that way — do not add a docs mount to the `server`
service.

## Conventions

- Configured sources live in `sources.toml`; the old direct-child discovery remains only behind the
  deprecated compatibility `index` command.
- Reindexing is a **content hash** diff, never mtime. `touch` must cost nothing.
- Deleting chunks means deleting from all three of `chunks`, `chunks_fts`, `chunks_vec`. Orphans in
  either index table are a bug; `_orphans()` in the tests asserts against them.
- Searches run in `anyio.to_thread.run_sync` — SQLite and ONNX are sync and CPU-bound, and the
  event loop must not block.
- One read-only SQLite connection per thread (`server.db()`), with `PRAGMA query_only=ON`.
- Tool results are Pydantic models so the SDK generates `outputSchema` and clients get
  `structuredContent`. Keep the field descriptions — the model reads them.

## Testing

`tests/test_chunk.py` is pure and fast. `tests/test_retrieval.py` builds a real 16-file index
fixture and is the quality gate — `test_retrieval_quality_floor` asserts MRR ≥ 0.85 over the golden
query set. **If you change retrieval, run it and report the MRR**; do not lower the floor to make a
change pass.

Test fixtures must exceed `stub_max` (40 chars) or documents get dropped as stubs — several early
test failures were just undersized fixtures.

## Verifying protocol changes

The server speaks MCP `2026-07-28`, which is stateless: one POST, no `initialize`. Both `_meta`
keys below are **required** or you get `-32602`.

```bash
curl -s localhost:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/list' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{
       "io.modelcontextprotocol/protocolVersion":"2026-07-28",
       "io.modelcontextprotocol/clientCapabilities":{}}}}'
```

Verify SDK behaviour against the installed package (`.venv/lib/python3.13/site-packages/mcp/`)
rather than blog posts or memory — the v2 API differs from v1 and from most write-ups.
`GET /mcp` returning 200 (not 405) and a missing `MCP-Protocol-Version` being accepted are the
SDK's backward-compatibility allowances, not bugs.
