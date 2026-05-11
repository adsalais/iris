# RAG ingestion starter stack — high-level spec

**Status:** design only.
**Date:** 2026-05-11.
**Companions:**
- `2026-05-11-rag-row-policy-acl-design.md` (chunk-level row-policy ACL; defines `auth_id`).
- `2026-05-11-rag-kg-extraction-and-resolution-design.md` (KG extraction; consumes the chunks this pipeline writes).
- `2026-05-11-rag-stix-bootstrap-loader-design.md` (parallel ingestion path for structured STIX content).
- `2026-05-11-rag-stix-vocab-extraction-design.md` (vocabulary used by the KG extractor).
- `2026-05-11-rag-synthesis-prompt-design.md` (synthesis stage that consumes the resulting `rag_embeddings`).

## Goal

A concrete, minimal implementation of the ingestion pipeline that turns
heterogeneous source documents (PDFs, DOCX, PPTX, XLSX, web pages, emails,
Markdown, plain text) into rows in `rag_embeddings` (and contributes new
distinct `auth_id`s to operator-curated `rag_acl` rows) with stable
identifiers and propagated authorization metadata. Roughly 500 LOC of
Python, no exotic dependencies, owned by the operator-side ingestion
service.

## Scope

In scope:
1. The eight-stage pipeline and per-stage tool choices.
2. Chunking strategy and defaults.
3. Metadata schema attached to every chunk.
4. **How `auth_id` enters the pipeline (externally supplied) and how the
   pipeline validates and propagates it.**
5. Error handling for missing or unknown `auth_id`.
6. Audit tables for pipeline runs and per-document failures.

Out of scope:
- KG extraction (the KG extraction spec; runs after this pipeline writes
  each chunk).
- The synthesis stage (downstream).
- Live streaming ingestion — v1 is batch-driven.
- Orchestration framework — call the pipeline from a script, a Prefect
  job, Airflow, cron, or whatever the operator already runs.
- Automatic classification or content-based `auth_id` inference (see
  Authorization stance).

## Authorization stance

**`auth_id` is externally provided, never derived from document content.**
Every document arriving at the pipeline carries an `auth_id` decided
upstream by the operator's organizational classification process — that
decision involves humans, meetings, policy, and is explicitly not iris's
to make. The pipeline does not inspect content to guess `auth_id`: no
TLP-marking detection on the page, no path-based heuristics interpreted
as authoritative, no automatic classifier.

The pipeline's three responsibilities around `auth_id`:

1. **Require it.** A document without an `auth_id` is rejected at the
   acquisition boundary, logged, and surfaced to the operator. No
   fallback default — silence is worse than rejection.
2. **Validate it.** Before any embedding work happens, the pipeline
   checks `auth_id` exists in `rag_acl`. If it doesn't, the document is
   rejected — iris will not silently create ACL rows. Provisioning ACL
   rows is an explicit operator step (per the row-policy spec).
3. **Propagate it.** The `auth_id` is attached to every chunk derived
   from the document and written into `rag_embeddings.auth_id` unchanged.

## Pipeline stages

```
[ acquire ] → [ detect ] → [ parse ] → [ clean ] → [ chunk ]
                                                       ↓
    [ store ] ← [ embed ] ← [ dedup ] ← [ enrich metadata ]
```

Each stage is a function with typed inputs/outputs. Composition is
straightforward; no framework required.

### 1. Acquire

A source-specific connector yields tuples:

```
(source_uri: str, raw_bytes: bytes, auth_id: str, source_metadata: dict)
```

`source_metadata` is the catch-all for fields the connector knows up
front but the pipeline doesn't compute. The pipeline reads:

- `tlp` — optional; one of `'clear' | 'green' | 'amber' | 'amber_strict' | 'red'`.
  Stored on `rag_embeddings.tlp` for every chunk derived from the
  document. Informational only — does NOT affect authorization. If
  absent, the column default (`'clear'`) is used.

**The connector is the authoritative source of `auth_id`.** See the next
section for the three supported mechanisms by which connectors obtain it.
`tlp` follows the same mechanisms (sidecar manifest field / directory
convention / API field).

The connector must guarantee at most one `auth_id` per document. If a
document genuinely needs split authorization (rare), it is split into
multiple logical documents upstream — never silently merged.

The acquire stage records `source_hash = sha256(raw_bytes)` here for
deterministic `doc_id` derivation and exact dedup.

### 2. Detect

`python-magic` (libmagic bindings) or `filetype` to sniff MIME. File
extensions are a hint, not authoritative. Output: a `mime_type` string
that routes to the parser.

### 3. Parse

| MIME family | Parser |
|---|---|
| `application/pdf` (text-based or scanned) | **Docling** (IBM) for layout + tables. PyMuPDF as a fallback if Docling errors. |
| `application/vnd.openxmlformats-*` (DOCX, PPTX, XLSX), `text/html`, `message/rfc822` (EML), `text/markdown`, `text/plain` | **Unstructured.io** |
| Scanned PDFs (no text layer) | `OCRmyPDF` (Tesseract) → Docling. A config flag swaps in commercial OCR (AWS Textract / Azure Document Intelligence) when scan quality demands it. |

Both Docling and Unstructured emit a **structured element list** (Title,
NarrativeText, ListItem, Table, Code, etc.) plus a canonical Markdown
rendering. The pipeline uses the Markdown for chunk text and the element
list for section-aware chunk boundaries.

### 4. Clean

- **Whitespace**: collapse runs, rejoin PDF hyphenation across lines.
- **Encoding**: NFC normalization; ligature substitution (`ﬁ`→`fi`,
  `ﬂ`→`fl`).
- **Headers/footers**: detect runs repeating across PDF pages and strip.
- **Web boilerplate**: `trafilatura.extract()` on raw HTML before passing
  to Unstructured, or use Unstructured's `partition_html` with
  `skip_headers_and_footers=True`.
- **IOC preservation**: skip aggressive normalization inside table cells
  and code blocks. Hashes, IP addresses, registry paths, and command
  lines must survive byte-exact for forensic value.

### 5. Chunk

**Strategy: section-aware over the Markdown structure, with a recursive
fallback within over-sized sections.**

1. Walk the parser's element list. Each leaf section is a candidate
   chunk.
2. If a section fits the budget (≤512 tokens), emit it as one chunk.
3. If it exceeds the budget, split with
   `RecursiveCharacterTextSplitter(chunk_size=512, chunk_overlap=64,
   separators=["\n\n", "\n", ". ", " ", ""])`.
4. **Tables** → one chunk each, rendered as Markdown, prepended with the
   table caption (or the immediately surrounding paragraph if there's no
   caption).
5. **Code blocks** (YARA rules, Sigma rules, PowerShell, shell scripts,
   etc.) → one chunk each, never split.
6. **Heading chain** → prepend `# H1 > ## H2 > ### H3\n\n` to every chunk
   within that section. Cheap context boost that survives retrieval.

Hard limits: minimum chunk size 32 tokens (smaller fragments are merged
with their neighbor); maximum 1024 tokens (oversized blocks force a hard
character split).

### 6. Enrich metadata

Every chunk receives:

| Field | Source |
|---|---|
| `doc_id` | `uuid5(NS, source_uri \|\| source_hash)` |
| `chunk_id` | `uuid5(doc_id, ordinal \|\| content_hash)` |
| `auth_id` | from stage 1, **unchanged** |
| `tlp` | from stage 1's `source_metadata` if supplied; otherwise `'clear'` (the column default). Informational only — never used for authorization. |
| `source_uri` | from stage 1 |
| `source_hash` | from stage 1 |
| `page` | (PDF) page number from Docling |
| `section_path` | `["H1 title", "H2 title", ...]` |
| `heading_chain` | rendered chain string |
| `content_hash` | `sha256(chunk_text)` |
| `language` | detected via `langid` or `fasttext` LID |
| `mime_type` | from stage 2 |
| `ingested_at` | wall-clock UTC |
| `pipeline_version` | static string from this codebase |

**`auth_id` validation happens here.** The pipeline queries `rag_acl`
once per distinct `auth_id` per batch, caching the result for the run. A
missing `auth_id` causes the whole document to be rejected (not just the
chunk) — see "Error handling".

**Re-chunking cost.** Because `chunk_id` derivation includes `ordinal`,
changing the chunking strategy (size, overlap, section boundaries)
renumbers chunks within a document → every chunk gets a new `chunk_id`
→ every downstream KG row keyed by the old `chunk_id`
(`kg_mentions_raw`, `kg_relations_raw`, `kg_alias_map`, and the
`evidence_chunks` arrays in `kg_edges`) is orphaned. A chunking-strategy
change therefore requires re-running KG extraction + resolution for
every affected document, not just re-embedding. Operators should change
chunking strategy deliberately and rarely; bump `pipeline_version` on
every change so old vs. new chunk_id provenance is visible.

### 7. Dedup

Two distinct concerns, often conflated:

- **Same-document re-ingestion** (the dedup this stage actually does):
  skip a chunk if `(doc_id, content_hash)` already exists. Catches
  re-running the pipeline on an unchanged document.
- **Cross-document duplicate content** (e.g., the same vendor PDF
  appearing under two `source_uri`s, producing two different `doc_id`s
  but identical chunks): **not** deduplicated in v1. The same content
  can exist under multiple `(doc_id, auth_id)` pairs. Two reasons:
  preserving per-document audit trails, and avoiding cross-tenant
  content correlation through dedup. See Open Question 1.
- **Near-duplicate** (optional, off by default): MinHash via
  `datasketch`, Jaccard threshold ~0.85, scoped strictly within the same
  `auth_id` to avoid leaking content existence across tenants.

### 8. Embed + store

- Batched API calls (16–64 chunks per request).
- Local cache by `(embedding_model_id, content_hash)` in SQLite —
  re-runs of the same content are free; model upgrades trigger a
  re-embed.
- `rag_embeddings` inserts batched in groups of ~1000, ordered by
  `(doc_id, chunk_id)` to keep MergeTree parts tidy.
- The pipeline **reads** `rag_acl` to validate `auth_id` but never
  writes it. New `auth_id`s must exist in `rag_acl` before documents
  carrying them arrive.

## How `auth_id` reaches the pipeline

Three operator-supportable mechanisms. The pipeline accepts any of them
through pluggable connectors; the connector's job is to produce a clean
`(source_uri, raw_bytes, auth_id, source_metadata)` tuple.

### Mechanism A — sidecar manifest

```
/ingest/incidents/2026-Q1-acme.pdf
/ingest/incidents/2026-Q1-acme.pdf.meta.json
```

```json
{
  "auth_id": "customer:acme",
  "source_uri": "internal://cases/2026-Q1-acme",
  "classified_by": "alice@org.example",
  "classified_at": "2026-05-08T10:14:00Z"
}
```

Strongest audit trail; works well when the classification process emits a
structured record alongside the file.

### Mechanism B — directory convention

```
/ingest/customer:acme/incidents/2026-Q1.pdf
/ingest/tlp:white/public-cti/vendor-report.pdf
/ingest/tlp:amber/sharing-group-x/report-2025-12.pdf
```

Connector parses the first path segment as `auth_id`. Simplest to wire
up; weakest audit trail (no record of who classified).

### Mechanism C — API ingest

`POST /ingest/document` (`multipart/form-data`):

```
file: <bytes>
auth_id: customer:acme
source_uri: https://internal/case/2026-Q1
classified_by: alice@org.example
```

Best when classification is part of an upstream system (case management,
DLP gateway, sharing-group portal) that can call iris directly.

The operator picks one or supports several through distinct connectors.

## Error handling

| Condition | Behaviour |
|---|---|
| Document without `auth_id` | Reject at acquisition; do not parse. Record in `ingest_failures` with `stage = 'acquire'`, `error_kind = 'missing_auth_id'`. |
| `auth_id` not present in `rag_acl` | Reject at metadata enrichment; do not embed any chunk from the document. `error_kind = 'unknown_auth_id'`. |
| Parse failure | Mark the document failed in `ingest_failures`. Do **not** partial-ingest a subset of chunks. |
| Embed-API failure (transient) | Retry with exponential backoff. After N retries, mark document failed. |
| Embed-API failure (permanent, e.g., chunk too large) | Mark chunk failed but continue the document if the failure is isolated; if more than X% of chunks fail, fail the whole document. |
| Dedup hit (exact) | Skip silently; increment `chunks_dedup_skipped`. |

**The "reject the whole document" rule on `auth_id` failure is
deliberate.** Partial ingestion — some chunks tagged, some not — is a
silent authorization bug waiting to happen. Fail loudly instead.

## Audit tables (iris provides; pipeline writes)

### `ingest_runs`

| Column | Type |
|---|---|
| `run_id` | `UUID` |
| `started_at` | `DateTime` |
| `finished_at` | `Nullable(DateTime)` |
| `documents_seen` | `UInt32` |
| `documents_ingested` | `UInt32` |
| `documents_rejected_no_auth_id` | `UInt32` |
| `documents_rejected_unknown_auth_id` | `UInt32` |
| `documents_failed_parse` | `UInt32` |
| `chunks_written` | `UInt32` |
| `chunks_dedup_skipped` | `UInt32` |
| `pipeline_version` | `LowCardinality(String)` |
| `embedding_model_id` | `LowCardinality(String)` |
| `notes` | `String` |

### `ingest_failures`

| Column | Type |
|---|---|
| `run_id` | `UUID` |
| `source_uri` | `String` |
| `stage` | `Enum8('acquire', 'detect', 'parse', 'clean', 'chunk', 'enrich', 'embed', 'store')` |
| `error_kind` | `LowCardinality(String)` |
| `error_message` | `String` |
| `failed_at` | `DateTime` |

Engine: `MergeTree ORDER BY (run_id, source_uri)` for both. Trivial to
query for operator dashboards.

## Concrete dependency list

```
docling          # PDF + layout/tables
unstructured     # everything else (DOCX/PPTX/XLSX/HTML/EML/MD/TXT)
trafilatura      # HTML boilerplate stripping
ocrmypdf         # OCR for scanned PDFs (wraps Tesseract)
python-magic     # MIME sniffing
langid           # language detection
datasketch       # MinHash (optional)
langchain-text-splitters  # RecursiveCharacterTextSplitter
clickhouse-connect  # write rag_embeddings, read rag_acl
httpx            # web fetching
playwright       # JS-rendered web pages (optional)
```

Plus the embedding-provider SDK (OpenAI / Voyage / etc.) the operator
selects.

## What iris owns vs. what the operator owns

| Concern | Owner |
|---|---|
| Implementing the pipeline (~500 LOC Python) | Iris (in the ingestion module) |
| Built-in connectors (filesystem walker, S3 lister, IMAP fetcher, web crawler, API endpoint) | Iris (pluggable) |
| Audit tables `ingest_runs` and `ingest_failures` | Iris (provisioned alongside `rag_embeddings`) |
| **Assigning `auth_id` to each document** | Operator's classification process (the meeting-driven decision) |
| Wiring `auth_id` into sidecar manifests / dir layout / API calls | Operator |
| Provisioning `rag_acl` rows before documents arrive | Operator |
| Choosing and configuring the embedding model | Operator |
| Scheduling pipeline runs | Operator |
| Reviewing `ingest_failures` and acting on rejected documents | Operator |
| OCR vendor selection (Tesseract default, commercial swap) | Operator |

## Non-goals

- No content-based `auth_id` derivation (deliberately — see Authorization
  stance).
- No automatic creation of `rag_acl` rows by the pipeline.
- No live streaming ingestion in v1.
- No in-pipeline KG extraction — KG extraction runs as a separate stage
  consuming the chunks this pipeline writes.
- No quality scoring of OCR output ("is this text good enough?"). If
  needed, the operator re-OCRs and re-ingests.
- No partial-document ingestion: a document either ingests fully or
  fails fully, with one narrow exception for isolated embed failures
  (see Error handling).
- No multi-`auth_id` documents — splitting into separate logical
  documents is the operator's responsibility upstream.

## Open questions

1. **Cross-`auth_id` dedup policy.** A vendor whitepaper might be tagged
   `tlp:white` in one ingest and `customer:acme` in another (attached to
   a case file). v1: dedup only within `doc_id`; cross-doc duplicates
   are preserved, and the same content can exist under multiple
   `(doc_id, auth_id)` pairs. Revisit if storage cost becomes
   uncomfortable; the trade-off is auditability vs. storage efficiency.
2. **Re-classification of an already-ingested document.** If a document
   already ingested as `tlp:white` arrives later with `auth_id =
   customer:acme`, should the pipeline warn ("previously classified
   tlp:white — confirm intent") or silently create the second copy?
   Likely warn; defer to v1.1.
3. **OCR cache.** Commercial OCR is expensive enough that re-runs should
   be free. A dedicated `ocr_cache` table keyed by `source_hash` is the
   obvious shape. v1.1.
4. **Classification-service connector.** Some organizations keep
   classification decisions in a single source of truth (a
   classification service). A connector can call out to that service to
   resolve `auth_id` per document. Out of v1 scope but the pluggable
   connector pattern already accommodates it.
5. **Embedding model migration.** Switching embedding models requires
   re-embedding every chunk. A migration job (read `rag_embeddings`,
   re-embed with the new model, write to a parallel table, atomically
   swap via a view) lives outside this pipeline but is worth specifying
   before the first ingest happens.
