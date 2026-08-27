# About docs-mcp

## What it is

A self-hosted search engine for documentation, exposed to an AI model over MCP.

You declare Git repositories or local documentation folders in `sources.toml`. The model can then
ask _which_ doc sets exist, search exactly one of them, and get back the exact passages it
needs instead of whole files. Everything runs in one container on your own machine; no API keys,
no documents leaving the box.

Currently indexed: **celery, rabbitmq, velociraptor, fastapi** — 1809 files, ~4000 passages.

## How it works

```
sources.toml                     desired Git and local sources
        │
        │  docs-mcp sync         acquire → split → embed → store (only new/changed files)
        ▼
   index.db  (SQLite: text + BM25 index + vectors)
        │
        │  docs-mcp serve        reads only; no access to the docs tree
        ▼
   MCP endpoint :8765/mcp  ──►  AI model (Claude Code, etc.)
```

**Indexing.** Every configured entry has a stable source name. Acquired files are split
on their heading structure — never mid-code-block — so each passage is a coherent section carrying
its heading breadcrumb (`Queues > TTL > Per-Message TTL`). Each passage is turned into a 384-number
vector describing its meaning. Markdown, MDX, reStructuredText and Hugo/Docusaurus markup are all
handled; navigation cruft and redirect stubs are dropped.

**Updating.** `sources.toml` is the desired source registry. `docs-mcp sync` hashes every acquired
file and only re-processes what actually changed. Nothing
changed → under a second. One file edited → about a second. A full rebuild of everything → a couple
of minutes. The running server picks up a new index immediately, no restart.

**Searching.** Three independent searches run for every query and their rankings are merged:

| leg            | catches                                                                 |
| -------------- | ----------------------------------------------------------------------- |
| exact phrase   | identifiers — `acks_late`, `x-death`, `worker_concurrency`              |
| keyword (BM25) | literal words anywhere in the docs                                      |
| vector         | meaning, so a paraphrase finds the right page without sharing any words |

Merging rankings (rather than scores) is what lets a keyword score and a vector distance be
compared at all. Any one leg alone measurably fails: keyword-only search buries `acks_late`
because the tokenizer splits it into the common words "acks" and "late"; vector-only search
blurs exact config keys together.

## The tech

|            |                                                                                          |
| ---------- | ---------------------------------------------------------------------------------------- |
| Protocol   | MCP `2026-07-28` (stateless — no session, one HTTP POST per request)                     |
| Language   | Python 3.13, `uv` for dependencies                                                       |
| MCP SDK    | `mcp` 2.0.0 (`MCPServer`, Streamable HTTP)                                               |
| Storage    | SQLite — FTS5 for keyword/phrase, `sqlite-vec` for vectors. One file, no database server |
| Embeddings | `BAAI/bge-small-en-v1.5` via `fastembed` (ONNX on CPU, no PyTorch), baked into the image |
| Serving    | uvicorn + Starlette, non-root container                                                  |
| Ship       | Docker Compose — one image, operational `serve` and `sync` commands                     |

Why SQLite rather than a vector database: at this scale the vector scan is exact and takes
milliseconds, so an approximate index would only lose accuracy. It also means the whole system is
one container and one file.

## What the model can do

| tool           | purpose                                         |
| -------------- | ----------------------------------------------- |
| `list_sources` | what doc sets exist, sizes, when last indexed   |
| `search_docs`  | hybrid search within exactly one named source    |
| `fetch_chunk`  | a source-scoped hit plus neighbouring passages   |
| `fetch_doc`    | a whole page, paginated                         |

## Honest limits

- Search returns _ranked candidates_, not a relevance guarantee — a vector search always has
  nearest neighbours, so a question about something genuinely absent still returns its closest
  passages rather than nothing.
- Cross-encoder reranking is implemented but off by default: measured on this corpus it gave no
  improvement while costing ~120× the latency, and it actively hurt identifier queries.
- English-tuned embedding model.

See `README.md` for commands and `CLAUDE.md` for the internals.
