# Source configuration and sync plan

## Decision

Use:

- one static, versioned `sources.toml` file as the desired source configuration;
- one SQLite database, `index.db`, for source runtime state and all searchable content;
- one public content-management command, `docs-mcp sync`;
- mandatory single-source search; cross-source search is deliberately out of scope.

Do not create one SQLite file per source and do not create a separate metadata database. Logical
source isolation through `source_id`, transactions, and vector partitioning is sufficient until
measurements prove otherwise. Keep storage access behind source-scoped functions so physical
partitioning remains possible later without changing configuration or CLI contracts.

The Docker image must not contain documentation or Git checkouts. Git cache and temporary filtered
snapshots live on the host. The one-shot indexer receives only the snapshot it is currently
processing through a read-only mount; the long-running MCP server receives only `index.db`.

## User experience

Users edit `sources.toml`, then run:

```text
docs-mcp sync
```

Useful scoped and recovery variants are:

```text
docs-mcp sync --source fastapi
docs-mcp sync --rebuild
docs-mcp sync --dry-run
```

`sync` owns the complete workflow: validate configuration, reconcile removed sources, acquire
current content, filter unsupported files, index changes, and report a per-source result. Users do
not run separate add, remove, pull, cleanup, or index commands. Existing `serve`, `search`, and
image-build `warmup` commands remain operational commands; the current public `index` command may
remain temporarily as a deprecated low-level compatibility entry point.

## Source configuration

Use TOML because Python 3.13 can read it without a runtime dependency and it remains easy to review
and version-control.

```toml
version = 1

[[source]]
name = "fastapi"
type = "git"
url = "https://github.com/fastapi/fastapi.git"
ref = "master"
directory = "docs/en/docs"
description = "FastAPI documentation"

[[source]]
name = "company-handbook"
type = "local"
path = "/Users/me/Documents/company-handbook"
description = "Internal company documentation"
```

Rules:

- `name` is the stable public identifier used by MCP search and fetch calls.
- `type` is exactly `git` or `local`; never infer it from `.git` files or URL shape.
- Git requires `url`; `ref` and `directory` are optional. `ref` may be a branch, tag, or commit and
  defaults to the remote default branch. `directory` defaults to the repository root.
- Local requires `path`, resolved to an absolute path during validation.
- `description` is optional and is returned by `list_sources`.
- File extensions are application capabilities and must never be configured per source.
- Credentials and tokens must never be stored in this file. Git uses the host's normal credential
  mechanisms.

`sources.toml` is the source of truth for desired state. The database stores the last observed and
last successfully indexed state. A database row must not silently override configuration.

## Validation before mutation

Parse and validate the entire file before changing cache, staging, or SQLite. Any structural
configuration error stops the complete sync with no mutations.

Validate:

- supported config version and known fields;
- required fields for each source type;
- duplicate source names, including case-folded collisions;
- a conservative safe name such as `[a-z0-9][a-z0-9._-]*`;
- no path separators, `.`/`..`, reserved names, or old `-docs` normalization collisions;
- local path existence and directory type;
- relative Git `directory` without traversal outside the checkout;
- conflicting definitions of the same source name.

Example:

```text
Configuration error: duplicate source name "fastapi"
Defined by entries 1 and 4 in sources.toml.
No sources were changed.
```

Do not choose one duplicate, merge entries, or partially synchronize a configuration that cannot
be interpreted unambiguously.

## One database

Keep the existing `DB_PATH=/data/index.db`. Add a first-class `sources` table and make relational
content refer to `sources.id` rather than repeating a free-form source name.

Suggested control schema:

```sql
CREATE TABLE sources (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT NOT NULL UNIQUE COLLATE NOCASE,
    type                 TEXT NOT NULL CHECK(type IN ('git', 'local')),
    origin               TEXT NOT NULL,
    ref                  TEXT,
    source_directory     TEXT,
    description          TEXT,
    desired_config_hash  TEXT NOT NULL,
    indexed_config_hash  TEXT,
    sync_status          TEXT NOT NULL,
    index_status         TEXT NOT NULL,
    indexed_revision     TEXT,
    last_attempt_at      TEXT,
    last_success_at      TEXT,
    last_error_code      TEXT,
    last_error_message   TEXT,
    indexed_files        INTEGER NOT NULL DEFAULT 0,
    indexed_chunks       INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
```

The important status distinction is:

```text
sync_status = failed
index_status = ready
```

This means the latest refresh failed but the previous successfully indexed content is still
searchable. A newly configured source whose first acquisition fails has `index_status = absent`.

Add `source_id INTEGER NOT NULL REFERENCES sources(id)` to `files` and use
`UNIQUE(source_id, rel_path)`. Keep a small `sync_runs` table only if historical diagnostics are
needed; it is not required for the first release. Last-attempt fields on `sources` are enough for
the normal CLI and `list_sources` response.

The database remains recoverable from `sources.toml` and source content. Schema, parser,
normalization, chunking, embedding model, and pipeline versions must remain recorded in the
database metadata so `sync --rebuild` can make correct invalidation decisions.

## Format policy

Maintain one parser registry as the source of truth:

```python
TEXT_PARSERS = {".md": ..., ".mdx": ..., ".rst": ..., ".txt": ...}
RICH_DOCUMENT_PARSERS = {".pdf": ..., ".docx": ...}
```

| Input | Git source | Local source |
| --- | --- | --- |
| Registered text format | index | index |
| PDF | skip | index |
| DOCX | skip | index |
| Image/archive/executable/unknown | skip | skip |
| Binary content disguised as text | fail visibly | fail visibly |
| Symlink | skip with warning | skip with warning |

Extension matching is case-insensitive. An extension selects a candidate parser; a content probe
must still reject binary or mismatched content. Unsupported files are normal and summarized as
`skipped`, not reported as parsing failures.

Skip hidden files and files below hidden directories at every depth. Do not follow symlinks. Do
not allow a configured Git subdirectory or archive member to escape its root.

## Host storage and Docker boundary

Use a host-managed state directory, default `.docs-mcp/` and configurable with `STATE_DIR`:

```text
.docs-mcp/
  git-cache/<source-name>/
  staging/<source-name>/
  tmp/
  locks/
```

This directory is gitignored. It is not copied into the Docker image.

For Git sources, keep a private shallow/bare fetch cache and export only the configured
documentation directory into a new filtered snapshot. For local sources, create a filtered
snapshot of supported files to guarantee a consistent input if the user edits the origin during
indexing. The snapshot may be temporary and removed after successful indexing; no permanent
second copy is required.

The friendly host command orchestrates the current Compose deployment:

1. acquire and filter content on the host;
2. mount the completed snapshot read-only into the one-shot indexer;
3. mount the persistent data volume containing `index.db`;
4. stream concise progress and return the aggregate exit status.

The long-running server never receives Git cache, staging, or origin mounts. It continues serving
documents reconstructed from SQLite only.

## Sync algorithm

### 1. Validate and plan

1. Acquire a global reconciliation lock.
2. Parse and fully validate `sources.toml`.
3. Read the `sources` table if the schema exists.
4. Calculate configured additions, changes, removals, and unchanged sources.
5. Print the plan and exit without mutation for `--dry-run`.

Configuration comparison uses a canonical `desired_config_hash`. A description-only change can
update metadata without reacquiring or reindexing content. Changes to type, origin, ref, or source
directory require acquisition. A name is stable identity in the first release; renaming is treated
as remove plus add.

### 2. Reconcile removals

A database source missing from the validated configuration is removed source-atomically:

1. collect all of its chunk IDs;
2. delete its rows from `chunks_fts` and `chunks_vec`;
3. delete relational chunks and files;
4. delete the `sources` row;
5. commit once;
6. remove only that source's managed cache/staging after verified containment.

Any failure rolls back the database removal and leaves unrelated sources untouched. Never delete
or modify the former origin. The final report lists every removed source and counts its deleted
files/chunks.

### 3. Acquire each configured source

Process sources independently after global validation. Start with serial processing for clear
logs and predictable CPU/RAM; per-source concurrency can be added later behind bounded workers.

Git acquisition:

- fetch the configured ref into the private cache;
- resolve and record the exact commit;
- verify the configured directory exists at that commit;
- export into a new temporary snapshot;
- filter to registered text formats only;
- atomically publish the completed snapshot.

Local acquisition:

- verify the origin remains a directory;
- copy supported text, PDF, and DOCX into a new temporary snapshot;
- filter hidden, symlinked, and unsupported entries;
- atomically publish the completed snapshot.

If acquisition or filtering fails, retain the previous snapshot and index. Record the failed
attempt in `sources`, continue with other valid sources, and return a non-zero aggregate status.

### 4. Build a candidate source update

Hash the published snapshot and compare it with `files` for that `source_id`. Determine added,
changed, removed, and unchanged files. Parse, normalize, chunk, and embed all added/changed files
before deleting their last-known-good rows.

Default publication is source-atomic:

- spool candidate chunks/vectors to bounded temporary storage when they do not fit comfortably in
  memory;
- if any supported file fails, record the error and do not apply that source's candidate update;
- when all candidates are ready, apply added/changed/removed files and source status in one SQLite
  transaction;
- explicitly delete matching rows from relational, FTS5, and sqlite-vec tables so no orphans
  remain.

This makes one source failure clean without requiring a separate database per source. Other
sources may still publish successfully in the same `sync` run.

### 5. Full rebuild

`docs-mcp sync --rebuild` reconstructs the complete database from the validated configuration and
freshly prepared snapshots:

1. acquire every configured source successfully;
2. build `index.next.db` from zero;
3. validate schema, integrity, orphan counts, source/file/chunk counts, and smoke retrieval;
4. close and checkpoint the candidate database;
5. atomically replace `index.db`;
6. retain the old file as a bounded rollback artifact until the new server read succeeds.

If any required source cannot be acquired or indexed, do not publish the rebuilt database. The
current `index.db` remains active. `--source NAME` may scope normal incremental sync, but the first
release should keep `--rebuild` database-wide rather than implement a second shadow-publication
mechanism for one source.

## Search behavior and scale

Make `source` mandatory and singular:

```python
search_docs(query: str, source: str, limit: int = ...)
fetch_chunk(source: str, chunk_id: int, context: int = 1)
```

The existing sqlite-vec `source` partition key already restricts KNN work to one source. Preserve
that physical partition even after relational tables move to `source_id`.

The current FTS query matches the global FTS index and filters by source through a later join. Add
an indexed source key to `chunks_fts` and include it in the FTS expression so FTS intersects source
and content postings before ranking. Keep user text sanitized through the existing query builders;
the internal source token must be generated from numeric `source_id`, not interpolated from user
input.

Benchmark instead of assuming this remains fast forever. Generate corpora with approximately 10,
100, and 1,000 sources and increasing total chunk counts. Measure phrase, BM25, vector, fused p50
and p95 latency, database size, source deletion time, and retrieval MRR within a fixed source as
unrelated sources are added.

Do not move to per-source database files unless measurements show that source-filtered FTS,
maintenance, or writer contention violates an agreed target. All store entry points must still be
source-scoped so such a migration remains internal.

## Failure semantics

Every error must identify the source and stage while preserving the last usable index:

```text
SOURCE       SYNC       INDEX   RESULT
fastapi      success    ready   updated to commit 1a2b3c4
celery       failed     ready   Git ref "v6-docs" was not found; previous index retained
handbook     failed     absent  policies/leave.docx could not be parsed
old-docs     removed    absent  removed 83 files and 191 chunks
```

Required cases:

- duplicate/invalid configuration: abort before all mutations;
- Git network/auth/ref/subdirectory failure: retain previous snapshot and index;
- local origin missing/unreadable: retain previous index;
- unsupported file: skip and count, not failure;
- supported-looking invalid file: fail that source update;
- parser/embedding failure: roll back that source update;
- SQLite transaction failure: roll back and keep previous status/content;
- interrupted sync: temporary snapshots/candidates are ignored and later cleaned safely;
- one source failure: continue other sources and return non-zero after the summary.

Store stable short error codes plus sanitized messages. Never store document bodies, credentials,
or full authenticated Git URLs in error records.

## Origin safety

The contract must be explicit and tested:

> docs-mcp never writes to or deletes from a configured Git or local origin. It only removes data
> from its verified private cache, staging directory, and SQLite index.

Enforce this structurally:

- never run `clean`, `reset`, or checkout operations inside a user directory;
- Git operations run only in the managed cache;
- copy supported files to new inodes; do not use hard links;
- do not follow symlinks;
- ensure state/cache/staging paths neither contain nor are contained by a local origin;
- cleanup functions accept only a validated managed-root handle and re-check containment before
  recursive deletion;
- the indexer receives snapshots read-only.

## Implementation map

New modules:

- `src/docs_mcp/sources.py`: typed `SourceSpec`, TOML loading, complete validation, config hashing.
- `src/docs_mcp/acquire.py`: Git cache/fetch/export and local snapshot acquisition.
- `src/docs_mcp/formats.py`: parser registry and source-type eligibility policy.
- `src/docs_mcp/staging.py`: temporary snapshots, atomic publication, containment-safe cleanup.
- `src/docs_mcp/sync.py`: reconciliation state machine, locking, reporting, and exit status.

Existing modules:

- `config.py`: add `SOURCES_CONFIG`, `STATE_DIR`, parser/limit settings; keep `DB_PATH`.
- `__main__.py`: add the public `sync` command and flags; deprecate direct content-management use
  of `index`.
- `indexer.py`: accept resolved `SourceSpec` plus snapshot, produce candidate source updates, and
  stop discovering arbitrary direct children as the primary configured path.
- `store.py`: add `sources`, schema migration, source-atomic publication, source deletion, FTS
  source token, and source-scoped APIs.
- `server.py`: require one source for search, expose description and independent sync/index status,
  and keep the server filesystem-isolated.
- `docs/get_docs.py`: replace hard-coded source definitions with checked-in `sources.toml`; keep a
  one-release deprecation wrapper if needed.
- `docker-compose.yml`: host-managed staging read-only for the one-shot indexer; database volume
  only for the server.
- `README.md`, `ABOUT.md`, `CLAUDE.md`: document the one-file database, static configuration,
  single sync workflow, safety rules, and failure semantics.

## Migration

Provide one compatibility release:

1. If `sources.toml` is absent, retain current direct-child discovery and `-docs` name stripping.
2. Print one concise migration notice after indexing, not per file.
3. Ship a sample/checked-in `sources.toml` for the repository's bundled Git documentation.
4. On first configured sync, create/migrate the `sources` table and associate existing `files`
   rows with validated source names without re-embedding unchanged content.
5. Detect collisions before migration and require the user to resolve them.
6. Deprecate `docs/get_docs.py` and the direct `index` workflow.
7. Remove implicit folder discovery only after the compatibility window.

Because FTS and sqlite-vec virtual tables require coordinated deletes and schema creation with
`IF NOT EXISTS` does not perform migrations, use an explicit schema version. A migration that
changes virtual-table shape should rebuild `index.next.db`, validate it, and atomically replace the
old database.

## Test plan

Configuration and planning:

- valid Git/local round-trip and canonical config hashing;
- duplicate, case-colliding, unsafe, ambiguous, and version-invalid configuration;
- `--dry-run` performs no filesystem or database writes;
- description-only versus acquisition-affecting changes;
- added, changed, removed, and unchanged reconciliation.

Acquisition and filtering:

- Git default branch, branch/tag/commit ref, documentation subdirectory, and changed revision;
- Git auth/network/missing-ref/missing-directory failures preserve prior content;
- local read-only origin, concurrent origin edits, missing origin, and unreadable files;
- complete format-policy matrix, mixed-case extensions, hidden paths, symlinks, suspicious text,
  and unsupported binaries;
- no operation changes origin hashes, paths, modes, or timestamps.

Database lifecycle:

- source rows and `(source_id, rel_path)` identity;
- source-atomic incremental publication and rollback;
- source removal leaves no relational, FTS5, or vector orphans;
- failed refresh records `sync_status=failed` while retaining `index_status=ready`;
- full rebuild publishes only after all validation and retains the old DB on failure;
- parser/model/pipeline fingerprint changes trigger the correct rebuild behavior.

Search and scale:

- source is mandatory and unknown sources fail clearly;
- FTS expressions constrain source inside FTS, not only through a post-match join;
- vector queries preserve the source partition constraint;
- existing identifier phrase, BM25, vector, RRF, and MRR quality gates remain intact;
- synthetic 10/100/1,000-source benchmarks report p50/p95 and ranking stability.

Docker and operations:

- image contains no documentation or Git checkout;
- server has only the database mount;
- indexer receives only a read-only prepared snapshot;
- interrupted sync and stale temporary directories are safely recoverable;
- concise summaries and stable non-zero exit behavior for partial failure.

## Acceptance criteria

The redesign is complete when:

- adding, changing, or removing a source requires editing `sources.toml` and running one command;
- `docs-mcp sync` cleanly reconciles desired and observed state;
- exactly one `index.db` contains source state, files, chunks, FTS, and vectors;
- Git sources index registered text only, while local sources also accept PDF and DOCX;
- no user configures extension allow-lists;
- no origin is ever modified or deleted;
- a source failure preserves its last known-good searchable content and does not block successful
  sources;
- duplicate or invalid configuration causes zero mutations and an actionable error;
- full rebuild is atomic at the database-file level;
- search always targets exactly one source;
- source-filtered lexical and vector performance is measured at scale before considering physical
  per-source databases.
