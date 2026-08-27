# docs-mcp

Documentation search over MCP for an AI model. Hybrid retrieval (exact phrase + keyword + vector),
one container, no API keys, nothing leaves the machine.

## Commands

```bash
docker compose up -d --build                                # build + start the server on :8765
docker compose run --rm indexer sync                         # reconcile sources.toml
docker compose run --rm indexer sync --rebuild               # re-embed everything from scratch
docker compose run --rm indexer sync --source fastapi        # sync one source only
docker compose run --rm indexer sync --dry-run               # preview changes
docker compose run --rm indexer materialize --source manuals  # rebuild rich docs without indexing
docker compose run --rm indexer inspect manuals manual.pdf --json
docker compose run --rm indexer search --source rabbitmq "per-message ttl" # query from the shell
docker compose logs -f server                               # follow logs
docker compose restart server                               # restart
docker compose down                                         # stop
curl localhost:8765/health                                  # {"status":"ok"} | {"status":"empty-index"}
```

## Configure and sync sources

Copy [sources.toml.example](sources.toml.example) to
`sources.toml`, edit it, and run `docs-mcp sync` (or `docker compose up -d`). The real
`sources.toml` is intentionally local-only and is ignored by Git.

Sources are explicitly `git` or `local`. Markdown, MDX, reStructuredText, and TXT are indexed
directly. PDF and DOCX are accepted only from local sources and are first converted into one
inspectable Markdown file per source section under `STATE_DIR/materialized`; the original rich
document remains the source of truth. Generated paths look like
`manual.pdf/p0012-b0003-deployment.md` and work with the same search/fetch calls as native docs.
Git sources ignore repository PDF/DOCX artifacts. Unsupported files are skipped, hidden paths and
symlinks are never followed, and the configured origin is never modified. A failed rich-document
update retains its last usable topics while other files continue. **No restart is needed** — the
running server picks up a new index immediately.

PDF extraction prefers the embedded text layer and invokes local Tesseract `heb+eng` only for a
page whose native layer is missing or unusable. DOCX extraction walks XML body order so paragraphs
and tables stay interleaved and never fabricates page numbers. The generated manifest records the
original hash, parser identity, page/block ranges, methods, and warnings. Page markers are removed
from searchable prose and exposed as chunk provenance. Encrypted PDFs, macros, external DOCX
relationships, unsafe ZIP paths, and configured resource-limit violations are rejected.

`docs-mcp materialize [--source NAME] [--force] [--strict]` updates generated output without
publishing an index. `docs-mcp inspect SOURCE PATH [--json]` accepts either an original PDF/DOCX
path or a generated logical path and shows its provenance (plus Markdown for a topic). Strict mode
treats extraction warnings as failures.

With the included Compose configuration, relative local paths should live below `docs/` (for
example, `path = "docs/logic"`). That directory is mounted into the one-shot indexer read-only and
is never mounted into the server. Native `docs-mcp sync` may use any local path.

Refresh the configured upstream doc sets:

```bash
docker compose run --rm indexer sync   # fetches configured sources and embeds only what changed
```

## Connect a client

On the VM, set the address to serve on, then restart:

```bash
cp .env.example .env
echo 'BIND_ADDR=100.x.x.x' >> .env     # Tailscale/WireGuard/LAN IP. 0.0.0.0 only if firewalled.
docker compose up -d
```

From your machine:

```bash
claude mcp add --transport http docs http://100.x.x.x:8765/mcp
claude mcp list                        # -> docs: ... ✔ Connected
```

With a token: put `AUTH_TOKEN=…` in `.env`, restart, then add
`--header "Authorization: Bearer …"`.

## Tools the model gets

| tool           | does                                                  |
| -------------- | ----------------------------------------------------- |
| `list_sources` | which doc sets exist, file/chunk counts, last indexed |
| `search_docs`  | hybrid search — required singular `source`, `query`, `limit` |
| `fetch_chunk`  | a source-scoped hit plus its neighbouring passages          |
| `fetch_doc`    | a whole page, paginated                               |

Also exposed as resources: `docs://<source>/<path>`.

## Settings

`.env`, all optional — see `.env.example`:

```
BIND_ADDR=127.0.0.1     # address the port is published on
PORT=8765
AUTH_TOKEN=             # empty = no auth
ALLOWED_ORIGINS=        # browser origins allowed; requests with no Origin always pass
RERANK=0                # 1 = add a cross-encoder rerank pass (see Notes)
DEFAULT_LIMIT=8         # hits per search
THREADS=                # ONNX threads; blank = all cores
CANDIDATE_MEMORY_MB=64  # candidate data spills to disk above this threshold
SOURCES_CONFIG=sources.toml
STATE_DIR=.docs-mcp     # private Git cache and temporary filtered snapshots
MATERIALIZED_DIR=...    # omit to use STATE_DIR/materialized
MAX_RICH_BYTES=104857600
MAX_PDF_PAGES=2000
MAX_EXTRACTED_CHARS=20000000
MAX_DOCX_ENTRIES=10000
MAX_DOCX_EXPANDED_BYTES=524288000
MAX_RENDERED_PIXELS=50000000
MAX_RICH_PROCESSING_SECONDS=1800
```

## Verify by hand

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

One POST, no `initialize` handshake — MCP `2026-07-28` is stateless. Both `_meta` keys are required.

```bash
uv run pytest -q          # unit, lifecycle, tool, and retrieval quality gates
```

## Numbers

Measured on this corpus (1809 files, 4068 chunks, 4 sources):

|                           |                                                     |
| ------------------------- | --------------------------------------------------- |
| full index                | ~2 min native, ~8 min under Docker Desktop on macOS |
| re-index, nothing changed | <1 s                                                |
| re-index, one file edited | ~1 s                                                |
| search                    | ~6 ms median                                        |
| image                     | 1.13 GB (both ONNX models baked in)                 |
| index file                | `index.db`, 43 MB, in the `docs-mcp_index` volume   |

## Notes

- **Retrieval** is three legs fused with weighted Reciprocal Rank Fusion: an exact-phrase leg for
  identifiers, BM25 for keywords, and vectors for meaning. The phrase leg is why `acks_late` and
  `worker_concurrency` work — SQLite's tokenizer splits them into common words, so keyword-only
  search buries them.
- **`RERANK=0` by default on purpose.** A cross-encoder pass was measured on this corpus and gave
  no improvement on prose queries (fusion already ranks 6 of 7 first) while costing ~740 ms per
  search instead of 6 ms. It also _hurts_ identifier queries — MRR 0.79 vs 0.92 — because
  cross-encoders score bare config keys as uniformly irrelevant. Identifier queries bypass it even
  when enabled. Try `RERANK=1` if your corpus is more prose-heavy.
- **Search returns ranked candidates, not a relevance guarantee.** A vector search always has
  nearest neighbours, and on this corpus the best-match distance for a real paraphrase (0.80)
  overlaps that of an invented word (0.83) — too close to threshold without losing real recall.
  A query with no searchable token at all returns nothing.
- The index is a named volume (`docs-mcp_index`), not a bind mount — the container runs as the
  non-root `app` user (uid 999), and a bind-mounted host directory arrives with the host's
  ownership, so `/data` is unwritable on a fresh Linux clone. Back up with
  `docker compose cp server:/data/index.db ./index.db`; restore with the same in reverse.
  `docker compose down -v` deletes it and starts over.
- The server mounts only the index volume. The docs tree goes to the indexer only, and `fetch_doc`
  serves from the index, so the server has no filesystem path to traverse.
- Rich-document output is deterministic for unchanged input and pipeline versions. Successful
  replacement is atomic and removes stale topics; a failed replacement records an inspectable
  error and leaves the prior materialization and searchable rows intact. The current PDF backend
  reconstructs text structure conservatively and does not generate descriptions for unlabeled
  figures or attempt handwritten recognition.
- Embeddings run on CPU in the container (`bge-small-en-v1.5`, 384-dim), models baked into the
  image, `HF_HUB_OFFLINE=1`. No network at runtime.
- Changing `DENSE_MODEL` forces a full rebuild automatically — vectors from two models aren't
  comparable.
- The SDK also serves pre-2026 MCP revisions, so `GET /mcp` opens a legacy SSE stream instead of
  returning 405, and a request omitting `MCP-Protocol-Version` is treated as `2025-03-26`. Both are
  the spec's backward-compatibility allowances, not strict-2026-only behaviour.
