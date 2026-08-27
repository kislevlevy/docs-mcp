# Rich-document materialization and unified indexing plan

## Status and decision

This plan supersedes the direct PDF/DOCX ingestion architecture in
`dev/pdf-ingest-plan.md`.

The selected architecture is:

```text
PDF / DOCX
  -> local extraction and topic segmentation
  -> one inspectable Markdown document per topic
  -> the existing text-document indexing path
  -> the existing MCP search/fetch workflow
```

The original rich document remains the source of truth. Generated Markdown is a
deterministic, replaceable build artifact stored under the docs-mcp state directory,
not user-authored source content.

The extraction choices measured in `dev/pdf-ingest-spike.md` remain applicable:
Docling base structure is the quality-first candidate, native PDF spans are retained
for formulas and font repair, and Tesseract `heb+eng` is used selectively rather
than over every page.

## Goals

1. Convert each local PDF or DOCX into a directory of Markdown documents, normally
   one document per source topic or structural section.
2. Make generated topic documents behave like regular documentation in
   `search_docs`, `fetch_chunk`, `fetch_doc`, and `docs://` resources.
3. Preserve the original file, page/block provenance, extraction method, warnings,
   and parser identity without placing ranking noise in the Markdown body.
4. Preserve Hebrew, English, mixed-direction text, code, tables, and formulas as
   faithfully as the selected local extractors allow.
5. Keep rich-document parsing out of the long-running MCP server. The server reads
   SQLite only.
6. Make materialization and indexing incremental, inspectable, deterministic,
   retryable, and safe to publish atomically.
7. Preserve all existing behavior for Markdown, MDX, reStructuredText, and TXT.

## Non-goals for the first production slice

- editing or rewriting the original PDF/DOCX;
- returning the original PDF bytes through MCP;
- handwritten-text recognition;
- generated descriptions of unlabeled figures;
- cloud parsing, remote OCR, or runtime model downloads;
- inventing confident topic names when the source has no reliable structure;
- perfect visual reconstruction of arbitrary page layouts;
- treating generated Markdown as user-editable canonical content;
- adding a separate vector database or retrieval implementation.

## Product behavior

### Supported source combinations

| Source type | Native text files | PDF/DOCX | Behavior |
| --- | --- | --- | --- |
| `git` | Index directly | Ignore | Do not materialize repository binary artifacts |
| `local` | Index directly | Materialize, then index generated Markdown | Rich documents never enter the text index directly |

Only the rich-document path needs a physical materialization step. Native text
documents already satisfy the normalized-document contract and do not need to be
copied into the state directory.

### Uniform MCP workflow

For a native Markdown document:

```text
search_docs -> fetch_chunk / fetch_doc
```

For a materialized PDF topic:

```text
search_docs -> fetch_chunk / fetch_doc
```

The tool calls remain the same. Optional provenance fields are additive and null
for native text documents.

`fetch_doc` on a PDF-derived path returns one generated topic document, not the
entire original PDF. Existing character pagination remains available when a topic
document is large.

No new `fetch_pages` tool is required for the MVP. Page ranges and inline source-page
markers provide citation context while keeping the MCP surface uniform. A future
page-range tool can be added without changing the materialization format.

## End-to-end architecture

```text
SourceSpec + acquired source root
  -> eligibility and signature checks
  -> native text candidates -----------------------------+
  -> PDF/DOCX materializer                                |
       -> parser / OCR / normalization                    |
       -> ordered source blocks                           |
       -> topic segmentation                              |
       -> Markdown topic files + manifest                 |
       -> atomic materialized-directory publication       |
                                                         v
  -> unified document candidates
  -> existing section-aware text chunker
       + materialized provenance adapter
  -> FTS5/BM25 + dense vectors + RRF
  -> transactional SQLite publication
  -> existing MCP tools with additive provenance fields
```

The parser and materializer produce Markdown; they do not write directly into the
search tables. The indexer consumes generated Markdown through the same chunking
and embedding abstractions used for native documentation.

## Materialized directory contract

### Root layout

Materialized output lives outside the source tree:

```text
${STATE_DIR}/materialized/
  <source-name>/
    <origin-relative-parent>/
      <original-filename-with-extension>/
        _manifest.json
        p0001-b0000-introduction.md
        p0012-b0003-propositional-semantics.md
        p0132-b0004-skolemization.md
```

Examples:

```text
.docs-mcp/materialized/cse-logic/20466-0021.pdf/
.docs-mcp/materialized/cse-logic/דף נוסחאות המצורף למבחן.pdf/
.docs-mcp/materialized/manuals/reference.docx/
```

The original extension remains in the directory name to avoid collisions such as
`manual.pdf` versus `manual.docx`. Relative parent directories are mirrored so two
files with the same basename in different source directories do not collide.

Hidden paths, symlinks, and the materialized root itself must never be rediscovered
as new source inputs.

### Stable generated names

Generated filenames use a structural locator followed by a readable slug:

```text
p0132-b0004-skolemization.md
```

- `p0132`: one-based physical PDF page where the topic begins;
- `b0004`: stable block ordinal on that page for the current materialization;
- `skolemization`: readable, sanitized heading slug.

DOCX has no canonical page number, so it uses a document block/paragraph locator:

```text
d000127-b0002-deployment-options.md
```

The locator is the identity component. A heading-text change may change the slug but
must not affect `section_id`. For unchanged source bytes and pipeline fingerprint,
the complete output tree must be byte-for-byte deterministic.

MVP stability is guaranteed for unchanged source bytes and pipeline versions. A
source reflow that moves section boundaries may legitimately replace generated
paths; atomic stale-output cleanup prevents duplicates.

### Markdown body contract

Generated documents use Markdown rather than TXT because Markdown can represent
headings, lists, tables, fenced code, formulas, and links.

````markdown
# Skolemization

<!-- docs-mcp-source-page: 132; label: 130 -->

Normalized source prose...

```python
def example():
    return True
```

$$
\forall y\,\exists x\,R(x,y)
$$

<!-- docs-mcp-source-page: 133; label: 131 -->

Continuation...
````

Rules:

- the first heading is the source heading, not a generated summary;
- physical PDF page markers are deterministic machine comments and are not
  embedded or indexed as prose;
- optional printed page labels are retained separately from physical page numbers;
- body text is Unicode NFC and remains in the source language;
- code is fenced with a language only when the source provides reliable evidence;
- rectangular tables use Markdown tables; complex tables use labelled row/cell
  text rather than visually plausible but wrong Markdown;
- formulas use source-native Unicode or conservative LaTeX when available;
- formula/OCR aliases used only for retrieval are metadata, not presented as
  source-native body text;
- repeated headers, footers, page numbers, and personalized watermarks are removed
  only after cross-page repetition establishes them as boilerplate;
- no parser warning, confidence score, filename, or page number is inserted into
  searchable prose.

### Manifest contract

`_manifest.json` is the authoritative mapping from generated Markdown to the
original rich document.

```json
{
  "manifest_version": 1,
  "source": "cse-logic",
  "origin_path": "20466-0021.pdf",
  "origin_media_type": "application/pdf",
  "origin_sha256": "...",
  "page_count": 259,
  "parser": {"name": "hybrid-docling-native", "version": "..."},
  "pipeline_fingerprint": "...",
  "warnings": [],
  "documents": [
    {
      "path": "p0132-b0004-skolemization.md",
      "section_id": "p0132-b0004",
      "title": "סקולמיזציה",
      "heading_path": ["יחידה 8", "סקולמיזציה"],
      "page_start": 132,
      "page_end": 136,
      "page_labels": ["130", "131", "132", "133", "134"],
      "block_start": {"page": 132, "index": 4},
      "block_end": {"page": 136, "index": 18},
      "section_confidence": 0.97,
      "content_kinds": ["paragraph", "formula", "list"],
      "extraction_methods": ["native", "font-map"],
      "search_aliases": [],
      "warnings": []
    }
  ]
}
```

Requirements:

- validate against a versioned schema before publication or indexing;
- reject absolute paths, `..`, duplicate paths, duplicate section IDs, missing
  generated files, and unlisted Markdown files;
- cap every string/list/payload and verify that all page/block ranges are ordered
  and within the source limits;
- metadata is trusted only after local validation; extracted body text remains
  untrusted documentation content;
- `search_aliases` may contain deterministic formula/font normalizations, never
  free-form summaries or translations in the MVP.

## Topic segmentation

Topic boundaries are chosen from the strongest available source evidence:

1. PDF bookmarks/outline or DOCX heading hierarchy.
2. Table-of-contents entries reconciled with physical pages.
3. Parser-detected headings using typography, numbering, whitespace, and layout.
4. Stable structural boundaries such as chapters, units, numbered sections, or
   major list/table transitions.
5. Conservative untitled sections when no reliable heading exists.

Do not use a language model to invent topic names or rewrite headings in the MVP.
Semantic similarity may later help decide ambiguous boundaries, but it must not
override explicit source structure.

Each section records `section_confidence`. Low-confidence sections remain usable
and emit `section_boundary_uncertain`.

Fallback behavior when no reliable topics exist:

- preserve any reliable heading path;
- otherwise create deterministic page-window documents with neutral names such as
  `p0001-p0010-untitled-section.md`;
- do not claim that a page window is a source-authored topic;
- avoid tiny files by merging undersized adjacent blocks when their structure does
  not conflict;
- keep a very large genuine topic as one generated document; `fetch_doc` already
  paginates it, while the chunker still produces smaller retrieval units.

A topic may begin or end in the middle of a page. Manifest block locators define
the exact boundary; page ranges exist for citation and navigation.

## Extraction and normalization

### PDF page classification

Classify every page independently:

- `native`: usable embedded text and plausible reading order;
- `scanned`: no useful text plus page-sized imagery;
- `hybrid`: useful text plus image regions containing additional text;
- `broken_text_layer`: text exists but fails script/font/order quality checks.

Processing order:

1. Extract native spans with font, bbox, direction, and physical page.
2. Run deterministic quality checks.
3. Use Docling base structure for reading order, headings, lists, and tables.
4. Reconcile Docling blocks with native spans by location.
5. Repair legacy Symbol-font private-use glyphs using the originating font mapping.
6. Preserve native formula spans when Docling omits formula contents.
7. OCR only scanned/broken pages or missing regions with local Tesseract `heb+eng`.
8. Deduplicate overlapping native/OCR text.
9. Remove established repeated boilerplate.
10. Segment normalized blocks into topic documents and render Markdown.

Docling formula enrichment is not part of the default path: the measured CPU cost
and formula-order errors are unacceptable for this corpus.

### Hebrew and mixed direction

- preserve logical Unicode order, not visual glyph order;
- reconstruct words from coordinates and segment directional runs;
- never apply whole-line bidi reversal to formulas or code;
- retain English identifiers inside Hebrew prose;
- normalize to NFC without stripping meaningful Hebrew combining marks;
- emit `rtl_order_uncertain` when deterministic reconstruction is ambiguous.

### Code

- preserve indentation, spaces, and line breaks;
- identify code only from reliable style/layout evidence;
- never run prose dehyphenation, bidi reordering, or sentence joining over code;
- fenced code is kept intact by the existing chunker;
- split oversized code by line only as a final fallback.

### Formulas

- prefer source-native Unicode when the PDF font map is reliable;
- use font-aware deterministic mappings for legacy Symbol spans;
- retain conservative LaTeX exposed by a trustworthy parser;
- preserve formula blocks separately from prose;
- add deterministic aliases only for lexical retrieval;
- mark OCR-derived formula text as OCR and warn when confidence is low;
- do not use dense embeddings as the only retrieval path for formula identifiers.

### DOCX

Materialize DOCX into the same directory/manifest contract, but use structural
locators instead of page numbers. Walk XML body order so paragraphs and tables stay
interleaved. Do not fabricate pagination. The detailed security and feature limits
from `dev/pdf-ingest-plan.md` continue to apply.

## Unified indexing contract

### Candidate discovery

The indexer receives two candidate streams:

1. eligible native text files from the acquired source root;
2. validated generated Markdown entries from rich-document manifests.

Both become a common `IndexDocument` value containing:

```python
class IndexDocument:
    source: str
    logical_path: str
    filesystem_path: Path
    title: str | None
    origin_path: str
    origin_media_type: str
    section_id: str | None
    page_start: int | None
    page_end: int | None
    metadata: dict[str, JSONValue]
```

For native Markdown, `logical_path` and `origin_path` are the same, `section_id` is
null, and page fields are null.

For a generated topic:

```text
logical_path = 20466-0021.pdf/p0132-b0004-skolemization.md
origin_path  = 20466-0021.pdf
```

This logical path is what MCP clients pass back to `fetch_doc`.

### Chunking

Generated Markdown uses the existing heading/fenced-code-aware chunker with one
additive provenance adapter:

- source-page comments update the current page locator but are excluded from text;
- every chunk receives ordered block provenance;
- `page_start` and `page_end` derive from included source blocks;
- no chunk crosses a generated topic-file boundary;
- neighboring chunks used by `fetch_chunk(context=N)` remain within one topic file;
- title and heading path prefix embedding text as they do for native docs;
- source path, page number, parser, and warnings do not enter embedding text.

### SQLite changes

Bump the schema version when implementation begins. Because the index is
reproducible, use a full candidate rebuild rather than an in-place migration.

Add to `files`:

- `origin_path TEXT NOT NULL`;
- `origin_media_type TEXT NOT NULL`;
- `section_id TEXT NULL`;
- `page_start INTEGER NULL`;
- `page_end INTEGER NULL`;
- `page_labels_json TEXT NOT NULL DEFAULT '[]'`;
- `extraction_methods_json TEXT NOT NULL DEFAULT '[]'`;
- `warnings_json TEXT NOT NULL DEFAULT '[]'`;
- `materialization_fingerprint TEXT NULL`.

Add to `chunks`:

- `page_start INTEGER NULL`;
- `page_end INTEGER NULL`;
- `provenance_json TEXT NOT NULL DEFAULT '[]'`;
- `content_kinds_json TEXT NOT NULL DEFAULT '[]'`.

Generated Markdown contents remain stored in the existing `chunks.text`, so the
MCP server does not need access to `${STATE_DIR}/materialized`.

The pipeline fingerprint must cover:

- manifest and materialization format versions;
- parser/backend versions;
- OCR engine/languages/DPI and quality thresholds;
- Unicode/font normalization version;
- topic-segmentation version;
- Markdown renderer version;
- chunking settings/version;
- embedding model and query/passage formatting.

## MCP/API behavior

Keep all existing tools and argument signatures.

Add optional fields to `SearchHit` and `ChunkPassage`:

- `origin_path`;
- `origin_media_type`;
- `section_id`;
- `page_start`;
- `page_end`;
- `page_labels`;
- `content_kinds`;
- `extraction_methods`.

Add optional fields to `DocResult`:

- `origin_path`;
- `origin_media_type`;
- `section_id`;
- `page_start`;
- `page_end`;
- `page_labels`;
- extraction status/warning summary.

All fields are optional/defaulted so native documentation and existing clients keep
working unchanged.

Example PDF-derived result:

```json
{
  "source": "cse-logic",
  "path": "20466-0021.pdf/p0132-b0004-skolemization.md",
  "origin_path": "20466-0021.pdf",
  "origin_media_type": "application/pdf",
  "section_id": "p0132-b0004",
  "page_start": 132,
  "page_end": 133,
  "chunk_id": 1842,
  "heading_path": "יחידה 8 > סקולמיזציה",
  "text": "..."
}
```

`fetch_doc` returns the complete logical topic document, with existing `offset` and
`next_offset` pagination. The `docs://<source>/<path>` resource behaves the same way.

## Materialization and indexing lifecycle

### Incremental behavior

For each rich source file:

1. stream-hash the original bytes;
2. compare input hash and materialization fingerprint with the published manifest;
3. skip extraction when both match and every manifest file validates;
4. otherwise build a complete candidate directory in a private temporary path;
5. validate manifest, generated Markdown, limits, and deterministic ordering;
6. publish the directory atomically;
7. expose its generated documents to the index candidate stream;
8. chunk/embed only logical documents whose content or searchable metadata changed.

An unchanged rich file must cause zero parser, OCR, and embedding work.

### Failures and last-known-good behavior

- a failed rich-file update retains its previous materialized directory and previous
  searchable database rows;
- record the new input hash, stable error code, message, and attempt time;
- continue materializing and indexing other files;
- retry the failed input on the next run even when its bytes are unchanged;
- a warning may publish usable output; a fatal parse/validation error may not;
- removal of an original rich file removes its materialized directory and all
  logical topic documents from the next published index;
- stale generated topic files must never survive a successful directory replacement.

The SQLite server remains protected by the existing candidate-database validation
and atomic publication. It never observes a half-built materialized tree.

## CLI behavior

`docs-mcp sync` remains the normal command and performs acquisition,
materialization, and indexing in order.

Add a diagnostic command:

```text
docs-mcp materialize [--source NAME] [--force] [--strict]
```

It writes/updates the materialized tree without publishing a new index and reports:

- rich files added/changed/removed/unchanged/failed;
- generated topic count;
- page/block counts;
- OCR page/region count;
- warning count and codes;
- parser identity and elapsed time.

Add inspection support:

```text
docs-mcp inspect SOURCE PATH [--json]
```

For an original PDF/DOCX path it reports the manifest and generated topic list. For
a logical generated path it reports origin provenance, page/block range, extraction
methods, warnings, and rendered Markdown without mutating state.

`--strict` fails on warnings; normal mode publishes usable warning-bearing output
and fails only on fatal errors.

## Resource limits and security

The original binary-document controls remain mandatory:

- maximum input bytes, PDF pages, rendered pixels, extracted characters, images,
  processing time, DOCX ZIP entries, and expanded ZIP bytes;
- bounded OCR workers and subprocess timeouts;
- signature/MIME verification in addition to suffix;
- reject encrypted, malformed, oversized, or out-of-root inputs according to the
  centralized fatal/warning policy;
- never execute macros, JavaScript, embedded files, or external relationships;
- never follow remote images or download parser/OCR models at runtime;
- reject symlinks escaping the registered source root;
- validate every generated relative path before filesystem operations;
- do not expose source trees or materialized trees to the MCP server container;
- treat generated body text as untrusted documentation content.

Container builds must pre-bake selected parser models, PDF rendering tools,
Tesseract, and only the required `eng`/`heb` language data. Prove materialization
works with networking disabled.

## Implementation map

### New modules

- `src/docs_mcp/document.py` — blocks, provenance, warnings, limits, versions.
- `src/docs_mcp/materialize.py` — rich-file lifecycle and atomic publication.
- `src/docs_mcp/materialized.py` — manifest models, validation, candidate discovery.
- `src/docs_mcp/parsers/__init__.py` — parser registry/signature dispatch.
- `src/docs_mcp/parsers/pdf.py` — PDF classification and hybrid extraction.
- `src/docs_mcp/parsers/docx.py` — secure DOCX structural extraction.
- `src/docs_mcp/normalize.py` — Unicode/font/layout normalization.
- `src/docs_mcp/segment.py` — deterministic topic segmentation.
- `src/docs_mcp/render_markdown.py` — Markdown and page-marker rendering.
- `src/docs_mcp/quality.py` — page/block/output quality and fatal policy.

### Existing modules

- `src/docs_mcp/config.py` — materialized root, OCR settings, limits, versions.
- `src/docs_mcp/formats.py` — separate native candidates from materializable inputs.
- `src/docs_mcp/acquire.py` — retain original rich files for local sources only.
- `src/docs_mcp/sync.py` — orchestrate acquire -> materialize -> index and reports.
- `src/docs_mcp/indexer.py` — consume unified `IndexDocument` candidates; remove
  direct `_pdf_text`/`_docx_text` indexing.
- `src/docs_mcp/chunk.py` — page-marker/provenance adapter while preserving native
  Markdown behavior.
- `src/docs_mcp/store.py` — schema/fingerprint/provenance fields and failure rows.
- `src/docs_mcp/server.py` — additive provenance result fields only.
- `src/docs_mcp/__main__.py` — materialize/inspect commands and strict reporting.
- `src/docs_mcp/embed.py` — multilingual model candidate and explicit formatting.
- `Dockerfile` / `docker-compose.yml` — offline parser/OCR runtime and writable
  materialized state for the one-shot indexer only.
- `.env.example` / `README.md` — format support, lifecycle, limits, inspection, and
  known fidelity constraints.

### Tests and fixtures

- `tests/test_materialized_manifest.py`;
- `tests/test_materialization_lifecycle.py`;
- `tests/test_parsers_pdf.py`;
- `tests/test_parsers_docx.py`;
- `tests/test_topic_segmentation.py`;
- `tests/test_document_security.py`;
- `tests/fixtures/documents/` with redistributable native/scanned/mixed/two-column/
  Hebrew/formula/code/table/corrupt/encrypted cases;
- expected manifests, Markdown topics, provenance, warnings, and retrieval queries.

## Staged delivery

### Stage 0 — freeze contracts and fixtures

1. Check in manifest schema and representative expected materialized trees.
2. Establish deterministic topic IDs, page markers, and Markdown rendering.
3. Define fatal/warning policy and resource limits.
4. Record baseline extraction and retrieval metrics.

### Stage 1 — materialization framework

1. Implement manifest models/validation and atomic directory publication.
2. Implement unified native/materialized candidate discovery.
3. Add `materialize` and `inspect` diagnostics.
4. Use a simple native PDF/DOCX adapter only to prove lifecycle behavior.
5. Prove unchanged, changed, failed, and removed rich-file semantics.

### Stage 2 — topic documents and unified MCP behavior

1. Implement block normalization, deterministic segmentation, and Markdown output.
2. Remove direct PDF/DOCX-to-chunks indexing.
3. Add manifest provenance to files/chunks and bump/rebuild the schema.
4. Add optional provenance fields to search/fetch results.
5. Prove native Markdown results remain unchanged.

### Stage 3 — quality extraction

1. Integrate Docling base structure and native span reconciliation.
2. Add font-aware Symbol repair and RTL reconstruction.
3. Preserve formulas, tables, and code blocks.
4. Add selective local Tesseract `eng+heb`.
5. Prove offline container materialization.

### Stage 4 — retrieval quality

1. Build Hebrew, English, mixed, code, identifier, table, and formula golden groups.
2. Select a multilingual FastEmbed-compatible model.
3. Tune chunking only from per-group `Recall@3` and `MRR@10`.
4. Retain the existing English-text MRR floor and exact-identifier lexical access.

### Stage 5 — hardening and operations

1. Add corruption, timeout, symlink, ZIP-bomb, and resource-limit tests.
2. Benchmark full/incremental time, peak RAM, database size, materialized size, and
   Docker image delta.
3. Validate deterministic rebuilds and last-known-good recovery.
4. Complete user/operations documentation.

## Acceptance criteria

The feature is complete when:

1. Local PDF/DOCX inputs are never indexed directly; they first produce validated
   Markdown topic documents and a manifest.
2. Native text documents keep their current paths, chunks, and MCP behavior.
3. `search_docs`, `fetch_chunk`, `fetch_doc`, and `docs://` work identically for
   native and materialized logical paths.
4. `fetch_doc` for a PDF-derived hit returns one topic document rather than the
   complete original PDF.
5. Every generated topic retains its original file plus correct physical page/block
   provenance; DOCX never receives fabricated page numbers.
6. Every PDF-derived chunk exposes correct `page_start`/`page_end`, and neighboring
   chunk fetches never cross into another topic file.
7. Generated Markdown preserves tested Hebrew/English order, fenced code, tables,
   and formula text at the benchmark thresholds.
8. Whole-page OCR is not run for good native pages; OCR/native overlap is not
   duplicated.
9. Unchanged rich files cause zero parse/OCR/embed work; changed bytes or pipeline
   fingerprints rebuild the affected materialization and index entries.
10. Failed updates retain last-known-good materialized topics and searchable rows,
    record an inspectable error, and do not block unrelated files.
11. Successful replacement removes every stale generated topic and orphaned
    FTS/vector row.
12. Materialization and indexing work in the shipped container with networking
    disabled and all models/languages pre-baked.
13. Hebrew and English retrieval groups meet recorded `Recall@3`/`MRR@10` floors,
    and the existing English-text MRR remains at least `0.85`.
14. Runtime/image/materialized-storage costs and known fidelity limits are measured
    and documented before backend dependencies are made mandatory.

## Open decisions resolved by measurement

- Exact Docling versus lightweight fallback operating mode after full-corpus
  memory/image benchmarks.
- Multilingual embedding model and query/passage prefixes.
- OCR DPI and page-quality thresholds.
- Maximum acceptable low-confidence section size before neutral page-window
  fallback.
- Whether formula lexical aliases materially improve retrieval without increasing
  false positives.
- Whether a future `fetch_pages` tool adds value beyond topic fetch plus page
  provenance.

These remain measurement decisions, not user-facing parser flags in the normal
workflow.
