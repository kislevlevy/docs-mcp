# docs-mcp

Documentation search over MCP for an AI model. Hybrid retrieval (exact phrase + keyword + vector),
one container, no API keys, nothing leaves the machine.

## Commands

```bash
docker compose up -d --build                                # build + start the server on :8765
docker compose run --rm indexer                             # index new/changed docs (run after adding docs)
docker compose run --rm indexer index --force               # re-embed everything from scratch
docker compose run --rm indexer index --source fastapi      # index one source only
docker compose run --rm indexer search "per-message ttl"    # query from the shell, no client needed
docker compose logs -f server                               # follow logs
docker compose restart server                               # restart
docker compose down                                         # stop
curl localhost:8765/health                                  # {"status":"ok"} | {"status":"empty-index"}
```

## Add docs

```bash
cp -r ~/mydocs docs/mydocs-docs        # any folder under docs/ is a source
docker compose run --rm indexer        # only new/changed files get embedded
```

Reads `.md`, `.mdx`, `.rst`, `.txt`. Source name = folder name minus a trailing `-docs`.
Delete a folder and re-run the indexer to drop it. **No restart needed** — the running server picks up
a new index immediately.

Refresh the bundled upstream doc sets:

```bash
python3 docs/get_docs.py               # re-pulls celery, rabbitmq, velociraptor, fastapi from GitHub
docker compose run --rm indexer        # embeds only what changed
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

| tool | does |
|---|---|
| `list_sources` | which doc sets exist, file/chunk counts, last indexed |
| `search_docs` | hybrid search — `query`, optional `sources`, `limit` |
| `fetch_chunk` | a hit plus its neighbouring passages |
| `fetch_doc` | a whole page, paginated |

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
uv run pytest -q          # 40 tests: chunking + retrieval quality gate
```

## Numbers

Measured on this corpus (1809 files, 4068 chunks, 4 sources):

| | |
|---|---|
| full index | ~2 min native, ~8 min under Docker Desktop on macOS |
| re-index, nothing changed | <1 s |
| re-index, one file edited | ~1 s |
| search | ~6 ms median |
| image | 1.13 GB (both ONNX models baked in) |
| index file | `data/index.db`, 43 MB |

## Notes

- **Retrieval** is three legs fused with weighted Reciprocal Rank Fusion: an exact-phrase leg for
  identifiers, BM25 for keywords, and vectors for meaning. The phrase leg is why `acks_late` and
  `worker_concurrency` work — SQLite's tokenizer splits them into common words, so keyword-only
  search buries them.
- **`RERANK=0` by default on purpose.** A cross-encoder pass was measured on this corpus and gave
  no improvement on prose queries (fusion already ranks 6 of 7 first) while costing ~740 ms per
  search instead of 6 ms. It also *hurts* identifier queries — MRR 0.79 vs 0.92 — because
  cross-encoders score bare config keys as uniformly irrelevant. Identifier queries bypass it even
  when enabled. Try `RERANK=1` if your corpus is more prose-heavy.
- **Search returns ranked candidates, not a relevance guarantee.** A vector search always has
  nearest neighbours, and on this corpus the best-match distance for a real paraphrase (0.80)
  overlaps that of an invented word (0.83) — too close to threshold without losing real recall.
  A query with no searchable token at all returns nothing.
- Back up by copying `data/index.db`; delete it to start over.
- The server mounts only `data/`. The docs tree goes to the indexer only, and `fetch_doc` serves
  from the index, so the server has no filesystem path to traverse.
- Embeddings run on CPU in the container (`bge-small-en-v1.5`, 384-dim), models baked into the
  image, `HF_HUB_OFFLINE=1`. No network at runtime.
- Changing `DENSE_MODEL` forces a full rebuild automatically — vectors from two models aren't
  comparable.
- The SDK also serves pre-2026 MCP revisions, so `GET /mcp` opens a legacy SSE stream instead of
  returning 405, and a request omitting `MCP-Protocol-Version` is treated as `2025-03-26`. Both are
  the spec's backward-compatibility allowances, not strict-2026-only behaviour.
