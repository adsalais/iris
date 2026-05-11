# RAG phase 2 — data ingestion pipeline — high-level spec

**Status:** design only.
**Date:** 2026-05-11.
**Series:**
- Phase 1 (`2026-05-11-rag-phase-1-vector-rag-with-acl-design.md`) — vector RAG with row-policy ACL.
- **Phase 2 (this spec)** — data ingestion pipeline.
- Phase 3 (`2026-05-11-rag-phase-3-knowledge-graph-design.md`) — knowledge graph extension.
- Phase 4 (`2026-05-11-rag-phase-4-stix-vocab-and-bootstrap-design.md`) — STIX vocabulary + connector.

## Goal

A two-stage ingestion pipeline that turns heterogeneous source
documents (PDFs, DOCX, PPTX, XLSX, web pages, emails, Markdown, plain
text) into rows in `rag_embeddings` with stable identifiers and
propagated authorization metadata (`auth_id`). Roughly 500 LOC of
Python, no exotic dependencies, owned by the operator-side ingestion
service.

**Two stages decouple intake from embedding.** The operator submits
a document; the intake stage parses, chunks, and writes the chunks to
a buffer table (`rag_ingestion_buffer`) — fast and synchronous; the
operator's call returns once the chunks are buffered. A separate
async worker pool reads from the buffer, runs the slow stages
(embed, store in `rag_embeddings`, KG handoff), then **deletes the
processed rows from the buffer** via ClickHouse's lightweight delete.

Why two stages: embedding is the slowest stage (network round-trips
to the embedding API, often rate-limited). Without decoupling, every
ingest call's latency is bounded by the embedding service's response
time, and an outage of that service blocks all uploads. With the
buffer, intake survives embedding-service outages — documents queue
up and the worker drains the buffer when the service is healthy
again.

Builds on phase-1's `rag_embeddings` schema. Also writes **extraction
tasks** to `kg_extraction_queue` so the phase-3 extraction worker
can asynchronously do per-chunk KG extraction; the queue table is
provisioned in phase 2 because its writer is the ingestion worker.
The actual phase-3 worker that consumes the queue is out of scope
here.

A connector may yield documents whose entities/relations are
**already structured** (e.g., the phase-4 STIX connector). For those,
the worker writes the pre-extracted KG rows synchronously after the
chunk lands in `rag_embeddings`, and **does not** enqueue an
extraction task — no LLM extraction is needed.

## Scope

In scope:
1. Twelve-stage two-phase pipeline (7 sync intake, 5 async processing) and per-stage tool choices.
2. Chunking strategy and defaults.
3. Metadata schema attached to every chunk.
4. **How `auth_id` enters the pipeline (externally supplied) and how
   the pipeline validates and propagates it.**
5. The `kg_extraction_queue` table — written by phase 2, consumed by
   phase 3's extraction worker.
6. The `pre_extracted` optional field on the connector's
   `AcquiredDocument`, used by structured-data connectors (e.g. phase
   4's STIX connector) to bypass the LLM extractor.
7. Error handling for missing or unknown `auth_id`.
8. Audit tables for pipeline runs and per-document failures.

Out of scope:
- The phase-3 extraction worker itself (it consumes
  `kg_extraction_queue`; lives in phase 3).
- Live streaming ingestion — phase-2 is batch-driven.
- Orchestration framework — call the pipeline from a script, Prefect
  job, Airflow, cron, etc.
- Automatic classification / content-based `auth_id` inference.

## Authorization stance

**`auth_id` is externally provided, never derived from document
content.** Every document arriving at the pipeline carries an `auth_id`
decided upstream by the operator's organizational classification
process — that decision involves humans, meetings, policy, and is
explicitly not iris's to make.

The pipeline's three responsibilities around `auth_id`:

1. **Require it.** A document without an `auth_id` is rejected at
   acquisition, logged, and surfaced to the operator. No fallback
   default.
2. **Validate it.** Before any embedding work happens, the pipeline
   checks `auth_id` exists in `rag_acl`. Missing → reject the document.
3. **Propagate it.** Attached to every chunk derived from the document
   and written into `rag_embeddings.auth_id` unchanged.

## Pipeline stages

Two stages, separated by `rag_ingestion_buffer`.

```
─── INTAKE (sync) ──────────────────────────────────────
[ acquire ] → [ detect ] → [ parse ] → [ clean ] → [ chunk ]
                                                       ↓
                              [ buffer-write ] ← [ enrich ]
                                       ↓
                            rag_ingestion_buffer

─── PROCESSING (async worker) ──────────────────────────
              rag_ingestion_buffer
                       ↓
              [ claim batch ]
                       ↓
              [ dedup ] → [ embed ] → [ store-rag_embeddings ]
                                                       ↓
              [ lightweight-delete from buffer ] ← [ kg handoff ]
```

Each stage is a function with typed inputs/outputs. Intake stages are
synchronous on the operator's submit call — they finish in the time
budget of "parse a doc + compute a few hashes". The processing
stages run on a separate worker pool that pulls from the buffer.

**Structured-data connectors short-circuit.** If the connector
yielded `pre_extracted` + empty `raw_bytes` (e.g. a STIX SRO with no
description), intake skips parse → clean → chunk and writes a single
buffer row with `content` empty + `pre_extracted_json` populated. The
worker likewise skips dedup → embed and goes straight to KG handoff.

## Intake (synchronous)

These stages run on the operator's submit call. They finish in
parse-a-doc + hash time. The call returns once the chunks are in
`rag_ingestion_buffer`; from the operator's perspective, the document
is "received" but not yet queryable.

### 1. Acquire

A source-specific connector implements `IngestionConnector`:

```python
from typing import Iterator, Protocol
from dataclasses import dataclass, field

@dataclass(frozen=True)
class PreExtractedMention:
    local_id: int
    entity_type: str
    name_surface: str
    aliases: list[str]
    # All non-graph entity data: external identifiers, source
    # provenance, status flags. JSON-serialized into
    # kg_entities.metadata by the resolver. No fixed schema -- pick
    # any keys the application needs.
    metadata: dict[str, object] = field(default_factory=dict)
    # If the connector knows the canonical entity already (e.g., STIX
    # provides a stable per-object UUID), populate this. Otherwise
    # NULL and the resolver does Stage 1.5 lookup.
    canonical_entity_id: str | None = None

@dataclass(frozen=True)
class PreExtractedRelation:
    source_local_id: int  # refers to a local mention in this doc
    target_local_id: int  # may also refer to a mention in ANOTHER doc
                          # the same connector emitted -- the loader
                          # resolves cross-doc local_ids by name.
    relation_type: str
    evidence: str

@dataclass(frozen=True)
class PreExtractedKG:
    """Filled in by structured-data connectors (e.g. phase-4 STIX) to
    bypass the LLM extractor. Optional."""
    mentions: list[PreExtractedMention] = field(default_factory=list)
    relations: list[PreExtractedRelation] = field(default_factory=list)

@dataclass(frozen=True)
class AcquiredDocument:
    source_uri: str
    raw_bytes: bytes  # may be empty for relation-only documents
    auth_id: str
    source_metadata: dict[str, str]
    pre_extracted: PreExtractedKG | None = None

class IngestionConnector(Protocol):
    name: str
    def acquire(self) -> Iterator[AcquiredDocument]: ...
```

Built-in connectors (filesystem walker, S3 lister, IMAP fetcher, web
crawler, API endpoint, phase-4 STIX connector) all implement this
single Protocol. The optional `pre_extracted` field is what
distinguishes a structured-data connector from an unstructured one;
both go through the same pipeline.

`source_metadata` is the catch-all for fields the connector knows up
front. Used today for audit breadcrumbs (`classified_by`,
`classified_at`); future hot keys (sensitivity labels, retention
class) can be threaded through it without schema changes.

**The connector is the authoritative source of `auth_id`.** See "How
auth_id reaches the pipeline" below for the three supported delivery
mechanisms.

The connector must guarantee at most one `auth_id` per document. If a
document genuinely needs split authorization (rare), split it into
multiple logical documents upstream.

Records `source_hash = sha256(raw_bytes)` for deterministic `doc_id`
derivation and exact dedup.

### 2. Detect

`python-magic` or `filetype` to sniff MIME. File extensions are a hint,
not authoritative. Output: a `mime_type` string.

### 3. Parse

| MIME family | Parser |
|---|---|
| `application/pdf` (text-based or scanned) | **Docling** (IBM) for layout + tables. PyMuPDF as fallback. |
| `application/vnd.openxmlformats-*` (DOCX/PPTX/XLSX), `text/html`, `message/rfc822` (EML), `text/markdown`, `text/plain` | **Unstructured.io** |
| Scanned PDFs (no text layer) | `OCRmyPDF` (Tesseract) → Docling. Config flag swaps in commercial OCR (Textract / Azure Document Intelligence). |

Both Docling and Unstructured emit a **structured element list** (Title,
NarrativeText, ListItem, Table, Code, etc.) and a canonical Markdown
rendering. The pipeline uses the Markdown for chunk text and the
element list for section-aware chunk boundaries.

### 4. Clean

- **Whitespace**: collapse runs, rejoin PDF hyphenation across lines.
- **Encoding**: NFC normalization; ligature substitution (`ﬁ`→`fi`).
- **Headers/footers**: detect runs repeating across PDF pages and strip.
- **Web boilerplate**: `trafilatura.extract()` on raw HTML before
  passing to Unstructured.
- **IOC preservation**: skip aggressive normalization inside table
  cells and code blocks — hashes, IPs, registry paths, command lines
  must survive byte-exact.

### 5. Chunk

**Strategy: section-aware over the Markdown structure, with a recursive
fallback within over-sized sections.**

1. Walk the parser's element list. Each leaf section is a candidate
   chunk.
2. If a section fits the budget (≤512 tokens), emit it as one chunk.
3. If it exceeds the budget, split with
   `RecursiveCharacterTextSplitter(chunk_size=512, chunk_overlap=64,
   separators=["\n\n", "\n", ". ", " ", ""])`.
4. **Tables** → one chunk each, rendered as Markdown, prepended with
   the table caption (or the surrounding paragraph if no caption).
5. **Code blocks** → one chunk each, never split.
6. **Heading chain** → prepend `# H1 > ## H2 > ### H3\n\n` to every
   chunk in that section. Cheap context boost.

Hard limits: minimum 32 tokens (smaller fragments merge); maximum 1024
(forces a hard split).

### 6. Enrich metadata

Every chunk receives:

| Field | Source |
|---|---|
| `doc_id` | `uuid5(NS_DOC, f"{source_uri}::{source_hash}")` — see Phase 1 "UUID derivation" for the namespace definition. |
| `chunk_id` | `uuid5(doc_id, f"{ordinal}::{content_hash}")` — `doc_id` itself is the namespace; chunks are naturally parented to their document. |
| `auth_id` | from stage 1, **unchanged** |
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
once per distinct `auth_id` per batch, caching the result. A missing
`auth_id` causes the whole document to be rejected.

**Re-chunking cost.** Because `chunk_id` derivation includes `ordinal`,
changing the chunking strategy (size, overlap, boundaries) renumbers
chunks within a document → every chunk gets a new `chunk_id` → any
downstream rows keyed by the old `chunk_id` (the future phase-3 KG
tables) are orphaned. Bump `pipeline_version` on every chunking-strategy
change so provenance is visible.

The pipeline provides a `redocument(doc_id)` helper that the operator
calls when re-chunking. ClickHouse has no multi-statement transactions,
so the helper issues each step sequentially; the steps are idempotent
and re-running after a partial failure is safe. All deletes are
**lightweight deletes** (CH 23.3+):

1. `DELETE FROM rag_embeddings WHERE doc_id = :doc_id` — purge the
   document's chunks. Lightweight; no part rewrite.
2. `DELETE FROM kg_mentions_raw WHERE doc_id = :doc_id` (phase 3
   table; skipped if phase 3 isn't yet deployed).
3. `DELETE FROM kg_relations_raw WHERE doc_id = :doc_id` (phase 3
   table; skipped if phase 3 isn't yet deployed).
4. `DELETE FROM rag_ingestion_buffer WHERE doc_id = :doc_id` — in
   case any chunks of the old document are still buffered, drop them
   so the new ingest doesn't race with the old one.
5. Re-ingest the document from its source.
6. Operator runs the phase-3 resolution job at next cadence — it
   re-derives `kg_entities` / `kg_edges`, naturally dropping orphan
   contributions from the deleted mentions/relations.

The destructive `DELETE`s are scoped by `doc_id` (a single document's
worth of rows). They run as part of the ingestion identity's
permissions, which already has `ALTER` on these tables for normal
operations. This is **the** supported way to re-chunk a document.
Operators who want to keep old chunks alongside new ones (for
provenance audit) snapshot the tables before `redocument()` rather
than trying to coexist two `pipeline_version`s of the same `doc_id`.

### 7. Buffer-write

The last intake stage. Writes one row per chunk into
`rag_ingestion_buffer`, then returns to the operator. All chunks of a
single document are buffered atomically (via a batched INSERT) — the
operator's call doesn't return half-buffered.

For documents with `pre_extracted` (structured connectors), the
buffer row stores the `PreExtractedKG` payload serialized as JSON
in `pre_extracted_json`. For documents with empty `raw_bytes`
(relation-only docs from structured connectors), the buffer row's
`content` is empty and `content_hash` is the hash of the empty
string; the processing worker recognizes this shape and skips the
embed step.

## Processing (async worker)

These stages run on a separate worker pool, decoupled from the
operator's submit call. Workers can be scaled independently of intake
based on embedding-API throughput.

**Worker access model.** Same as the phase-3 workers — runs under
`query_as_user(worker_session, ...)` with a regular iris user
(`rag-ingest-worker`), NOT a tier admin. The worker's groups must be
listed in `rag_acl.allowed_roles` for every `auth_id` it should
process. Row policies apply normally; `GRANT INSERT` on
`rag_embeddings` is granted explicitly to the worker.

### 8. Claim batch

The worker reads a batch of unclaimed rows from
`rag_ingestion_buffer` and claims them atomically via a
**ClickHouse lightweight update**:

```sql
-- 1. Read candidate task ids
SELECT chunk_id
FROM rag_ingestion_buffer
WHERE claimed_by IS NULL
   OR claimed_at < now() - INTERVAL 10 MINUTE  -- re-claim stale
ORDER BY buffered_at
LIMIT 64
SETTINGS apply_mutations_on_fly = 1

-- 2. Claim them
ALTER TABLE rag_ingestion_buffer
UPDATE claimed_by = '<worker_id>', claimed_at = now()
WHERE chunk_id IN (...)
  AND (claimed_by IS NULL OR claimed_at < now() - INTERVAL 10 MINUTE)
SETTINGS materialize_ttl_after_modify = 0
```

The `UPDATE` is a **lightweight update** (CH 24.3+) — it writes the
change to a separate part-level structure instead of rewriting the
underlying part. Reads with `apply_mutations_on_fly = 1` see the
updated values immediately. This is what makes a CH-backed buffer
viable as a claim queue.

Stale-claim recovery (`claimed_at < now() - 10 minutes`) handles
worker crashes: another worker re-claims the row and re-processes
it. Re-processing is safe because chunk_ids are deterministic and the
final INSERT into `rag_embeddings` produces an identical row.

### 9. Dedup

Two distinct concerns, often conflated:

- **Same-document re-ingestion** (what this stage actually does): skip
  a chunk if `(doc_id, content_hash)` already exists. Catches re-runs
  on an unchanged document.
- **Cross-document duplicate content** (the same vendor PDF appearing
  under two `source_uri`s → two different `doc_id`s, identical chunks):
  **not** deduplicated in v1. The same content can exist under multiple
  `(doc_id, auth_id)` pairs. Two reasons: per-document audit trails;
  avoiding cross-tenant content correlation through dedup.
- **Near-duplicate** (optional, off by default): MinHash via
  `datasketch` over **word-level 5-shingles** (`shingle(text) = {tuple(words[i:i+5]) for i in ...}`),
  num_perm=128, Jaccard threshold 0.85. Scoped strictly within the
  same `auth_id` to avoid cross-tenant content leakage. The token
  unit and shingle width are fixed in the spec so two implementations
  produce the same dedup decisions; changing either is a
  `pipeline_version` bump.

### 10. Embed + store

- Batched API calls (16–64 chunks per request).
- Local cache by `(embedding_model_id, content_hash)` in SQLite.
- `rag_embeddings` inserts in batches of ~1000, ordered by
  `(doc_id, chunk_id)`.
- The pipeline **reads** `rag_acl` for validation but never writes it.
  New `auth_id`s must exist in `rag_acl` before documents carrying
  them arrive.

### 11. KG handoff

The last stage decides how each document's KG side gets populated.
There are two branches:

**Branch A — `pre_extracted` is set** (structured-data connector;
e.g. phase-4 STIX). Write the KG rows synchronously:

1. For each `PreExtractedMention`, compute `mention_id` (phase-1 UUID
   derivation: `uuid5(chunk_id, <mention_identifier>)` for content-bearing
   docs; the connector supplies a stable identifier for content-less
   relation-only docs). Insert into `kg_mentions_raw` with the
   document's `auth_id`.
2. If the mention carries `canonical_entity_id`, insert a
   `kg_alias_map` row tying the synthetic mention to the canonical
   directly (`resolution_method = 'exact'`, `confidence = 1.0`). If
   not, leave it to the next phase-3 resolution run.
3. For each `PreExtractedRelation`, insert a `kg_relations_raw` row
   referencing the resolved source/target `mention_id`s.
4. **No extraction-queue task is enqueued.** Pre-extracted content
   bypasses the LLM extractor entirely.

**Branch B — `pre_extracted` is unset** (standard text-document
connector). Enqueue extraction tasks, one per chunk just written:

```sql
INSERT INTO kg_extraction_queue (
    task_id, chunk_id, doc_id, auth_id,
    enqueued_at, status, claimed_by, claimed_at, completed_at, error
)
VALUES (...)
```

The phase-3 extraction worker (described in the phase-3 spec) consumes
this queue, calls the LLM extractor on the chunk's content, and
writes `kg_mentions_raw` / `kg_relations_raw`. Until the worker
processes the task, the chunk is queryable via the phase-1 vector
path but invisible to the phase-3 graph path.

### 12. Lightweight-delete from buffer

Last processing stage. Once the chunk is in `rag_embeddings` and the
KG handoff has run (synchronous write of pre-extracted rows, or task
enqueued in `kg_extraction_queue`), the worker deletes the row from
`rag_ingestion_buffer`:

```sql
DELETE FROM rag_ingestion_buffer
WHERE chunk_id IN (...)
```

This is a **ClickHouse lightweight delete** (default behaviour for
`DELETE FROM` on MergeTree since CH 23.3) — it marks rows as deleted
via the `_row_exists` virtual column without rewriting the
underlying part. Reads filter out deleted rows immediately; the
physical purge happens at the next merge.

Atomicity matters here: a worker crash between "store to
rag_embeddings" and "delete from buffer" leaves the buffer row in
place, and stale-claim recovery (stage 8) will eventually re-claim
it. The duplicate INSERT into `rag_embeddings` produces an identical
row (deterministic chunk_id), so no harm. Same for the KG handoff:
the queue task is keyed by `task_id = uuid5(chunk_id, "extract")`,
so re-enqueue is idempotent.

### Buffer-table failure modes

If a buffer row fails repeatedly (parse OK at intake but embed fails
permanently — chunk too large, embedding model unavailable for too
long, content corruption), it accumulates retry attempts. After N
failed claims (default `max_attempts = 5`), the worker moves the row
to a sibling `ingest_failures` audit table (see below) and
lightweight-deletes from the buffer. The operator sees the failure
on the audit dashboard.

The buffer table also has a TTL safety net: rows older than 7 days
get TTL-deleted regardless of state, on the theory that anything
that hasn't completed in a week is stuck for non-recoverable
reasons.

## `rag_ingestion_buffer` table

| Column | Type | Notes |
|---|---|---|
| `chunk_id` | `UUID` | Deterministic per Phase 1's UUID derivation. |
| `doc_id` | `UUID` | |
| `auth_id` | `String` | Validated at intake against `rag_acl`. |
| `content` | `String` | Chunk text. Empty for relation-only docs (structured connectors). |
| `content_hash` | `FixedString(64)` | `sha256(content)`. |
| `source_uri` | `String` | |
| `page` | `Nullable(UInt32)` | |
| `section_path` | `Array(String)` | |
| `language` | `LowCardinality(Nullable(String))` | |
| `mime_type` | `LowCardinality(Nullable(String))` | |
| `ordinal` | `UInt32` | Position within the document; needed for the chunk_id derivation reproducibility. |
| `pipeline_version` | `LowCardinality(String)` | |
| `pre_extracted_json` | `Nullable(String)` | Serialized `PreExtractedKG` if the connector supplied it; else `NULL`. |
| `buffered_at` | `DateTime` | |
| `claimed_by` | `LowCardinality(Nullable(String))` | Worker id; `NULL` when unclaimed. |
| `claimed_at` | `Nullable(DateTime)` | |
| `attempts` | `UInt8` DEFAULT 0 | Incremented on each claim; gates the max_attempts cutoff. |

Engine:
```sql
ENGINE = MergeTree
PARTITION BY toYYYYMM(buffered_at)
ORDER BY (buffered_at, chunk_id)
TTL buffered_at + INTERVAL 7 DAY DELETE
SETTINGS
    apply_mutations_on_fly = 1,         -- lightweight UPDATE visible to subsequent reads
    materialize_ttl_after_modify = 0    -- don't re-materialize TTL on each mutation
```

The TTL is a safety net — under normal operation, the worker
lightweight-deletes rows long before TTL kicks in. Rows that hit the
TTL are stuck-and-unrecovered; their absence from the audit table is
itself a signal the operator should investigate.

### Backpressure on the buffer

If the worker pool can't keep up with intake (sustained input >
sustained embedding throughput), the buffer grows unboundedly. v1 has
no explicit backpressure mechanism — operators monitor buffer depth
via `SELECT count() FROM rag_ingestion_buffer WHERE claimed_by IS NULL`
and provision more workers when needed. v1.1 may add an intake-side
rate limit triggered by buffer depth.

### `kg_extraction_queue` table

Provisioned alongside `rag_embeddings` (iris's create-database flow
does both).

| Column | Type | Notes |
|---|---|---|
| `task_id` | `UUID` | `uuid5(chunk_id, "extract")`. Deterministic; the same chunk re-enqueued for any reason produces the same task_id. |
| `chunk_id` | `UUID` | |
| `doc_id` | `UUID` | |
| `auth_id` | `String` | Inherited from the chunk; lets per-tenant workers filter by their granted auth_ids. |
| `enqueued_at` | `DateTime` | |
| `status` | `Enum8('pending' = 1, 'claimed' = 2, 'completed' = 3, 'failed' = 4)` | |
| `claimed_by` | `LowCardinality(Nullable(String))` | Worker identifier. |
| `claimed_at` | `Nullable(DateTime)` | |
| `completed_at` | `Nullable(DateTime)` | |
| `error` | `Nullable(String)` | Last error message on failed tasks. |

Engine:
```sql
ENGINE = ReplacingMergeTree(enqueued_at)
PARTITION BY toYYYYMM(enqueued_at)
ORDER BY task_id
TTL completed_at + INTERVAL 30 DAY DELETE WHERE status = 'completed'
```

The worker claims tasks with a **lightweight update** (`ALTER TABLE
kg_extraction_queue UPDATE status='claimed', claimed_by=...,
claimed_at=now() WHERE task_id IN (...) AND status='pending' SETTINGS
materialize_ttl_after_modify = 0`), re-claims stuck tasks
(`status='claimed' AND claimed_at < now() - 10 minutes`), and marks
done or failed. The lightweight-update mechanism (CH 24.3+) avoids
part rewrites; reads with `apply_mutations_on_fly = 1` see the
claim immediately. Same pattern as the ingestion-buffer claim.

Re-running the ingestion pipeline on an already-extracted chunk:
deterministic `task_id` lets `ReplacingMergeTree(enqueued_at)` dedupe
naturally; the worker sees one row and re-processes only if `status` is
`pending` or stale-`claimed`.

## How `auth_id` reaches the pipeline

Three operator-supportable mechanisms. The pipeline accepts any via
pluggable connectors; the connector produces a clean
`(source_uri, raw_bytes, auth_id, source_metadata, pre_extracted)`
tuple.

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

Strongest audit trail.

### Mechanism B — directory convention

```
/ingest/customer:acme/incidents/2026-Q1.pdf
/ingest/internal:eng/runbooks/sso-recovery.md
/ingest/public/handbook-2026.pdf
```

Connector parses the first path segment as `auth_id`. Simplest to
wire up; weakest audit trail (no record of who classified).

### Mechanism C — API ingest

`POST /ingest/document` (`multipart/form-data`):

```
file: <bytes>
auth_id: customer:acme
source_uri: https://internal/case/2026-Q1
classified_by: alice@org.example
```

Best when classification is part of an upstream system (case
management, DLP gateway, sharing-group portal).

The operator picks one or supports several through distinct connectors.

## Error handling

| Condition | Behaviour |
|---|---|
| Document without `auth_id` | Reject at acquisition; do not parse. `error_kind = 'missing_auth_id'`. |
| `auth_id` not present in `rag_acl` | Reject at metadata enrichment; no chunks embedded. `error_kind = 'unknown_auth_id'`. |
| Parse failure | Mark document failed; do **not** partial-ingest a subset of chunks. `error_kind = 'parse_failure'`. |
| Empty document (parse succeeded but yielded zero chunks; e.g., image-only PDF with no OCR layer) | Reject with `error_kind = 'no_extractable_text'`. The operator's response is re-OCR + re-ingest. Counted in `documents_failed_parse`. |
| Embed-API failure (transient) | Retry with exponential backoff. After N retries, mark failed. |
| Embed-API failure (permanent on isolated chunk) | Mark chunk failed but continue the document; if >X% chunks fail, fail the whole document. |
| Dedup hit (exact) | Skip silently; increment counter. |

**The "reject the whole document" rule on `auth_id` failure is
deliberate.** Partial ingestion — some chunks tagged, some not — is a
silent authorization bug waiting to happen.

## Audit tables (iris provides; pipeline writes)

### `ingest_runs`

| Column | Type |
|---|---|
| `run_id` | `UUID` |
| `started_at` / `finished_at` | `DateTime` / `Nullable(DateTime)` |
| `documents_seen` / `documents_ingested` | `UInt32` |
| `documents_rejected_no_auth_id` | `UInt32` |
| `documents_rejected_unknown_auth_id` | `UInt32` |
| `documents_failed_parse` | `UInt32` |
| `chunks_written` / `chunks_dedup_skipped` | `UInt32` |
| `pipeline_version` / `embedding_model_id` | `LowCardinality(String)` |
| `notes` | `String` |

### `ingest_failures`

| Column | Type |
|---|---|
| `run_id` | `UUID` |
| `source_uri` | `String` |
| `stage` | `Enum8('acquire', 'detect', 'parse', 'clean', 'chunk', 'enrich', 'buffer_write', 'claim', 'dedup', 'embed', 'store', 'kg_handoff')` |
| `error_kind` | `LowCardinality(String)` |
| `error_message` | `String` |
| `failed_at` | `DateTime` |

Engines, per table:

```sql
-- ingest_runs
ENGINE = MergeTree
PARTITION BY toYYYYMM(started_at)
ORDER BY (run_id, source_uri)
TTL started_at + INTERVAL 365 DAY DELETE

-- ingest_failures
ENGINE = MergeTree
PARTITION BY toYYYYMM(failed_at)
ORDER BY (run_id, source_uri)
TTL failed_at + INTERVAL 365 DAY DELETE
```

Audit retention is operator-tunable but 365 days is the safe default —
long enough to debug last quarter's incidents, short enough that the
audit tables don't dominate storage.

## Concrete dependency list

```
docling          # PDF + layout/tables
unstructured     # everything else
trafilatura      # HTML boilerplate stripping
ocrmypdf         # OCR for scanned PDFs
python-magic     # MIME sniffing
langid           # language detection
datasketch       # MinHash (optional)
langchain-text-splitters  # RecursiveCharacterTextSplitter
clickhouse-connect  # write rag_embeddings, read rag_acl
httpx            # web fetching
playwright       # JS-rendered web pages (optional)
```

Plus iris's single HTTP-client wrapper around the embedding endpoint
configured in `.rag_env` (phase-1 — no per-vendor SDK).

## What iris owns vs. what the operator owns

| Concern | Owner |
|---|---|
| Implementing the pipeline (~500 LOC Python) | Iris |
| Built-in connectors (FS walker, S3 lister, IMAP fetcher, web crawler, API endpoint) | Iris (pluggable) |
| Audit tables (`ingest_runs`, `ingest_failures`) | Iris |
| **Assigning `auth_id` to each document** | Operator's classification process |
| Wiring `auth_id` into manifests / dirs / API calls | Operator |
| Provisioning `rag_acl` rows before documents arrive | Operator |
| Choosing and configuring the embedding model (`.rag_env`) | Operator |
| Scheduling pipeline runs | Operator |
| Reviewing `ingest_failures` and acting on rejected documents | Operator |
| OCR vendor selection (Tesseract default, commercial swap) | Operator |

## Tests

Phase-2 tests share the phase-1 `rag_env` fixture (skip-on-missing).
Additional tests:

- **No external resources needed**: parser-stage unit tests on fixture
  PDFs/DOCX (decoupled from CH and the embedding model).
- **Requires `.rag_env`**: end-to-end ingest of a fixture document into
  a test database; verify chunks land with correct `auth_id` and the
  row policy filters as expected when read under different sessions.

The "external resources" tests reuse the phase-1 `rag_env` fixture; no
new test infrastructure.

## Non-goals

- No content-based `auth_id` derivation (deliberately).
- No automatic creation of `rag_acl` rows by the pipeline.
- No live streaming ingestion in v1.
- No in-pipeline KG extraction.
- No quality scoring of OCR output.
- No partial-document ingestion.
- No multi-`auth_id` documents — splitting is the operator's job.

## Open questions

1. **Cross-`auth_id` dedup policy.** v1: dedup only within `doc_id`;
   cross-doc duplicates preserved. Revisit if storage cost becomes
   uncomfortable.
2. **Re-classification of an already-ingested document.** If a document
   previously ingested under one `auth_id` arrives later under a
   different one, warn (likely) or silently create the second copy?
   Defer to v1.1.
3. **OCR cache.** Commercial OCR is expensive enough that re-runs
   should be free. A dedicated `ocr_cache` table keyed by `source_hash`
   is the obvious shape. v1.1.
4. **Classification-service connector.** Some organizations keep
   classification in a single source of truth. A connector that calls
   out to that service per document is accommodated by the pluggable
   pattern; out of v1 scope.
5. **Embedding-model migration.** Switching embedding models requires
   re-embedding every chunk. Lives outside this pipeline but worth a
   migration spec before the first ingest happens (a separate job that
   reads `rag_embeddings`, re-embeds with the new model, writes to a
   parallel table, atomically swaps via a view).
