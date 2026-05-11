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
operator's call returns once the chunks are buffered. A **single
async worker process** reads from the buffer, runs the slow stages
(embed, store in `rag_embeddings`, KG handoff), then **deletes the
processed rows from the buffer** via ClickHouse's lightweight delete.

Why two stages: embedding is the slowest stage (network round-trips
to the embedding API, often rate-limited). Without decoupling, every
ingest call's latency is bounded by the embedding service's response
time, and an outage of that service blocks all uploads. With the
buffer, intake survives embedding-service outages — documents queue
up and the worker drains the buffer when the service is healthy
again.

Why one worker (not a pool): a single-writer model removes claim
races on the buffer table outright. ClickHouse is not a queue and
its `ALTER TABLE UPDATE` mutations are not transactional locks;
implementing a correct multi-writer claim protocol on top of CH
would require either an external coordinator (Postgres advisory
lock, Consul session) or accepting that two workers occasionally
do the same work. Throughput is bounded by the embedding API
rate-limit and batch parallelism *within* the worker, not by the
worker count — the embedding API call is the bottleneck, and a
single worker batching 16–64 chunks per request saturates that
without contention.

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
[ admit (buffer-cap check) ]
            ↓
[ acquire ] → [ detect ] → [ parse ] → [ clean ] → [ chunk ]
                                                       ↓
                              [ buffer-write ] ← [ enrich ]
                                       ↓
                            rag_ingestion_buffer

─── PROCESSING (single async worker) ───────────────────
              rag_ingestion_buffer
                       ↓
              [ select batch ]
                       ↓
              [ dedup ] → [ embed ] → [ store-rag_embeddings ]
                                                       ↓
              [ lightweight-delete from buffer ] ← [ kg handoff ]
```

Each stage is a function with typed inputs/outputs. Intake stages are
synchronous on the operator's submit call — they finish in the time
budget of "parse a doc + compute a few hashes". A **single ingestion
worker process** drains the buffer; this constraint is operational
(see "Worker concurrency model" below) and is what lets the buffer
table get away without a multi-writer claim protocol.

**Structured-data connectors short-circuit.** If the connector
yielded `pre_extracted` + empty `raw_bytes` (e.g. a STIX SRO with no
description), intake skips parse → clean → chunk and writes a single
buffer row with `content` empty + `pre_extracted_json` populated. The
worker likewise skips dedup → embed and goes straight to KG handoff.

## Worker concurrency model

**Exactly one ingestion worker process runs at any time, system-wide.**
This is an operational constraint enforced by the operator's
deployment (single Kubernetes Deployment with `replicas: 1` and a
strict update strategy, a systemd unit with `Restart=on-failure`, or
equivalent). Iris does not provide a distributed lock or leader
election; "single worker" is the entire claim-collision-avoidance
strategy.

**Why this works.** The buffer table records pending chunks; the
worker selects a batch, processes it, deletes the rows on success.
With no concurrent writers there is no claim race. ALTER UPDATEs on
the buffer (to increment `attempts` after a failure) happen one at a
time and don't need lightweight-update semantics for correctness.

**Failure mode: worker crash mid-batch.** Rows the worker had already
embedded but not yet deleted remain in the buffer. The deterministic
`chunk_id` derivation means a re-run produces an identical
`rag_embeddings` row, so the duplicate insert is a no-op
(`ReplacingMergeTree` on `extracted_at` collapses it, or — since
`rag_embeddings` is plain `MergeTree` — the row exists once after the
first run and once more after the second; an idempotent INSERT
pattern in the worker, `INSERT ... SELECT WHERE NOT EXISTS`, avoids
the duplicate). On restart the worker resumes from the head of the
buffer.

**Operational consequence: ingestion stalls when the worker is
down.** Intake continues to buffer documents (subject to the
back-pressure cap below), but no chunks become queryable until the
worker is restarted. This is acceptable for the design's batch
nature; deployments that need HA should run a hot/standby pair under
external lease management (Kubernetes `leaderElection`, Consul
session, Postgres advisory lock) and treat the lease holder as the
"single worker" — but that is out of v1 scope.

## Back-pressure on intake

The intake API checks
`SELECT count() FROM rag_ingestion_buffer` against
`RAG_INGESTION_BUFFER_MAX_ROWS` (from `.rag_env`, default `100000`)
before accepting any new document. If the buffer is at-or-above the
cap, the API returns `HTTP 429 Too Many Requests` with a
`Retry-After` header derived from a rolling-window estimate of the
worker's drain rate (`buffer_depth / chunks_per_second_last_5_min`,
clamped to `[30, 3600]` seconds; falls back to a fixed 5-minute
suggestion when no recent throughput data is available).

The count query is cheap (`count()` on a MergeTree without a WHERE
hits the per-part counts) and runs once per ingest call. The cap is
the only protection against unbounded buffer growth when the
embedding API degrades or the single worker is offline — without it,
intake will OOM the CH server before the operator notices.

Filesystem / S3 / IMAP connectors that invoke ingest directly (not
through the API) honor the same cap by querying it themselves before
each batch. The connector's submit helper raises `BufferFullError`
which the operator's scheduling layer interprets as "pause this
batch, retry on the next tick."

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
    # provenance, status flags. Written as a JSON object into
    # kg_entities.metadata (CH native JSON type). No fixed schema --
    # pick any keys the application needs; CH infers new paths
    # automatically.
    metadata: dict[str, object] = field(default_factory=dict)
    # If the connector knows the canonical entity already (e.g., STIX
    # provides a stable per-object UUID), populate this. Otherwise
    # NULL and the resolver does Stage 1.5 lookup.
    canonical_entity_id: str | None = None

@dataclass(frozen=True)
class PreExtractedRelation:
    """Endpoint references can be in-document (local_id) or cross-document
    (mention_id, pre-resolved by the connector). Exactly one of
    {source_local_id, source_mention_id} must be set; same for target.
    Mixing per-endpoint is fine — e.g., a STIX SRO with no description
    sets both endpoints' mention_id, while an LLM extractor uses
    local_id for both."""
    relation_type: str
    evidence: str
    # In-document references — integer keys into the same PreExtractedKG's
    # `mentions` list. The loader resolves these to mention_ids after
    # writing the mentions for this document.
    source_local_id: int | None = None
    target_local_id: int | None = None
    # Cross-document references — fully-resolved mention_ids that the
    # connector computed deterministically from another document the
    # same run emitted. The loader uses these verbatim, bypassing
    # local_id resolution. Used by phase-4 STIX SROs whose source/target
    # mentions live in the SDO documents.
    source_mention_id: str | None = None  # UUID
    target_mention_id: str | None = None  # UUID

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

## Processing (single async worker)

These stages run in a single worker process, decoupled from the
operator's submit call. The single-worker constraint (see "Worker
concurrency model" above) is what lets the buffer table get away
without a multi-writer claim protocol — there's no concurrent reader
to race with.

**Worker access model.** Runs as the dedicated ClickHouse user
`<RAG_WORKER_USER>` configured in `.rag_env` (see phase-1 "Worker
account"). The worker connects to ClickHouse directly with the
`<RAG_WORKER_PASSWORD>` from `.rag_env`; it does **not** go through
iris's `query_as_user` / session machinery, and it does **not** hold
any iris-managed tier role.

Iris's RAG-database-enable path grants the worker
`SELECT, INSERT, ALTER, DELETE` on every RAG table and installs a
wildcard `USING 1` row policy on every row-policied RAG table
(including `rag_ingestion_buffer` and `kg_extraction_queue`). The
worker therefore reads/writes every row regardless of `auth_id` —
necessary for cross-tenant centroid computation and KG aggregation,
and consistent with phase 3's resolution workflow.

The worker reads `rag_acl` directly (operator-curated table; the
worker has `SELECT rag_acl`) to validate incoming `auth_id`s at
intake.

### 8. Select batch

The single worker selects a batch of pending rows from
`rag_ingestion_buffer`:

```sql
SELECT *
FROM rag_ingestion_buffer
WHERE attempts < {max_attempts:UInt8}
ORDER BY buffered_at, chunk_id
LIMIT {batch_size:UInt32}
```

No claim metadata, no atomic update, no stale-claim recovery — the
single-worker constraint makes those unnecessary. `attempts` is
incremented (via `ALTER TABLE … UPDATE attempts = attempts + 1`,
plain mutation — order doesn't matter because no concurrent writer)
only after a failed processing attempt, so the next iteration of the
batch picks up the row again, with `attempts` reflecting the prior
failure. After `max_attempts` (default 5) the worker writes the row
into `ingest_failures` and lightweight-deletes it from the buffer.

On worker restart, rows that were mid-process (embedded but not
deleted) get re-selected; the deterministic `chunk_id` makes the
re-INSERT into `rag_embeddings` produce an identical row, so the
re-process is idempotent at the storage layer. The worker uses
`INSERT INTO rag_embeddings SELECT ... FROM input WHERE NOT EXISTS
(SELECT 1 FROM rag_embeddings WHERE chunk_id = input.chunk_id)` to
short-circuit re-embedding on resume; the embedding API cost of a
re-run is non-trivial.

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

1. For each `PreExtractedMention`, compute `mention_id` per phase-1
   UUID derivation: `uuid5(chunk_id, <mention_identifier>)`. The
   `<mention_identifier>` value is connector-specific and must be
   deterministic — phase-4 STIX SDOs pin it to `"stix:synthetic"`,
   yielding one synthetic mention per SDO chunk. Insert into
   `kg_mentions_raw` with the document's `auth_id`.
2. If the mention carries `canonical_entity_id`, insert a
   `kg_alias_map` row tying the synthetic mention to the canonical
   directly (`resolution_method = 'exact'`, `confidence = 1.0`). If
   not, leave it to the next phase-3 resolution run.
3. For each `PreExtractedRelation`, resolve the source / target
   mention_ids and insert into `kg_relations_raw`:
   - If `source_local_id` is set: look up the corresponding mention
     in the same `PreExtractedKG.mentions` list (by `local_id`),
     compute its `mention_id`, use that as `source_mention_id`.
   - If `source_mention_id` is set: use it verbatim (cross-doc
     reference, fully resolved by the connector).
   - Same logic for target. The connector guarantees exactly one of
     `{local_id, mention_id}` per endpoint; the loader raises on
     ambiguous input.
4. **No extraction-queue task is enqueued.** Pre-extracted content
   bypasses the LLM extractor entirely.

**Branch B — `pre_extracted` is unset** (standard text-document
connector). Enqueue extraction tasks, one per chunk just written:

```sql
INSERT INTO kg_extraction_queue (
    task_id, chunk_id, doc_id, auth_id,
    enqueued_at, status, completed_at, error, attempts
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

A worker crash between "store to rag_embeddings" and "delete from
buffer" leaves the buffer row in place. On restart, the worker
re-selects the row at the head of the buffer; the `WHERE NOT EXISTS`
guard on the `rag_embeddings` INSERT short-circuits the embedding
API call, and the worker proceeds to the KG-handoff step. The KG
handoff is idempotent: pre-extracted mention/relation rows use
deterministic UUIDs (per phase 1's derivation table), and queue
tasks are keyed by `task_id = uuid5(chunk_id, "extract")`, so a
re-enqueue is a no-op under `ReplacingMergeTree`.

### Buffer-table failure modes

If a buffer row fails repeatedly (parse OK at intake but embed fails
permanently — chunk too large, embedding model unavailable for too
long, content corruption), the worker increments `attempts` on each
attempt. After `max_attempts` (default 5), the worker copies the row
into the sibling `ingest_failures` audit table and
lightweight-deletes it from the buffer. The operator sees the
failure on the audit dashboard and decides whether to fix the source
material + re-ingest, drop it, or change the embedding config.

The buffer table also has a TTL safety net: rows older than 7 days
get TTL-deleted regardless of state, on the theory that anything
that hasn't completed in a week is stuck for non-recoverable
reasons. With a single worker, TTL-hit rows are an "operator
investigate" signal: either the worker has been offline for a week
or the row is poison.

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
| `attempts` | `UInt8` DEFAULT 0 | Incremented after each failed processing attempt; gates the `max_attempts` cutoff. |
| `last_error` | `Nullable(String)` | Last error message; cleared on successful processing (but the row is deleted on success anyway). |

There are **no claim columns** (`claimed_by`, `claimed_at`). The
single-worker constraint makes them unnecessary; introducing them
would suggest a multi-writer model the design deliberately rejects.

Engine:
```sql
ENGINE = MergeTree
PARTITION BY toYYYYMM(buffered_at)
ORDER BY (buffered_at, chunk_id)
TTL buffered_at + INTERVAL 7 DAY DELETE
```

The TTL is a safety net — under normal operation, the worker
lightweight-deletes rows long before TTL kicks in. Rows that hit the
TTL with the worker running mean repeated failures: either the
operator's intake threw bad data past `max_attempts` and somehow
the failure-table write didn't land (audit it), or the worker has
been offline long enough to matter.

### Row policies on `rag_ingestion_buffer`

Same per-role-policy + worker-wildcard pattern as `rag_embeddings`
(phase 1):

- **Per user-facing tier role** (`*_USER`, `*_GRP`): one PERMISSIVE
  policy per role via `add_row_dict_policy(database=<rag>,
  table='rag_ingestion_buffer', auth_id='auth_id',
  dictionary='rag_acl_dict', authorisations='allowed_roles',
  role=R, value=R)`. Defense in depth — end users typically don't
  query the buffer, but a stray SELECT shouldn't leak chunks
  awaiting embed.
- **Wildcard for the worker**: `CREATE ROW POLICY ... USING 1 TO
  <RAG_WORKER_USER>` so the worker can read every row regardless
  of `auth_id`.
- **Wildcards for `iris_global_admin` and `<database>_DBADMIN`**:
  installed automatically by `add_row_dict_policy`.

The dict-keyed policy for user-facing roles requires `GRANT dictGet
ON rag_docs.rag_acl_dict` on those roles — already granted as part
of the phase-1 row-policy install.

### `kg_extraction_queue` table

Provisioned alongside `rag_embeddings` (iris's create-database flow
does both).

| Column | Type | Notes |
|---|---|---|
| `task_id` | `UUID` | `uuid5(chunk_id, "extract")`. Deterministic; the same chunk re-enqueued for any reason produces the same task_id. |
| `chunk_id` | `UUID` | |
| `doc_id` | `UUID` | |
| `auth_id` | `String` | Inherited from the chunk. Gates per-row visibility for the user-facing policies; the worker's wildcard policy ignores it. |
| `enqueued_at` | `DateTime` | |
| `status` | `Enum8('pending' = 1, 'completed' = 2, 'failed' = 3)` | |
| `completed_at` | `Nullable(DateTime)` | |
| `error` | `Nullable(String)` | Last error message on failed tasks. |
| `attempts` | `UInt8` DEFAULT 0 | Incremented by the phase-3 extraction worker on each failure; gates the worker's max-attempts cutoff. |

There are **no claim columns** — same rationale as
`rag_ingestion_buffer`. The phase-3 extraction worker is also a
single process (see phase 3), so the queue uses `status` for state
transitions without needing a claim protocol.

Engine:
```sql
ENGINE = ReplacingMergeTree(enqueued_at)
PARTITION BY toYYYYMM(enqueued_at)
ORDER BY task_id
TTL completed_at + INTERVAL 30 DAY DELETE WHERE status = 'completed'
```

The phase-3 extraction worker selects pending tasks
(`WHERE status = 'pending' AND attempts < max_attempts ORDER BY
enqueued_at LIMIT N`), processes them, and transitions `status` to
`'completed'` or `'failed'` via plain `ALTER TABLE UPDATE`
(non-lightweight; correctness doesn't depend on mutation latency
because no concurrent writer exists). Re-running the ingestion
pipeline on an already-extracted chunk produces an identical
`task_id`; `ReplacingMergeTree(enqueued_at)` collapses to one row,
and the worker re-processes only if that row's `status` is
`'pending'`.

### Row policies on `kg_extraction_queue`

Same shape as `rag_ingestion_buffer`:

- **Per user-facing tier role**: one PERMISSIVE policy per
  `*_USER` / `*_GRP` via `add_row_dict_policy(table='kg_extraction_queue',
  auth_id='auth_id', dictionary='rag_acl_dict',
  authorisations='allowed_roles', role=R, value=R)`. End users don't
  typically query the queue, but the policy keeps queue contents
  partitioned along the same auth_id boundary as the chunks.
- **Wildcard for `<RAG_WORKER_USER>`**: `USING 1` so the worker can
  see every task.
- **Wildcards for `iris_global_admin` and `<database>_DBADMIN`**:
  installed automatically by `add_row_dict_policy`.

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
| `stage` | `Enum8('acquire', 'detect', 'parse', 'clean', 'chunk', 'enrich', 'buffer_write', 'select', 'dedup', 'embed', 'store', 'kg_handoff')` |
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
| `rag_ingestion_buffer` + `kg_extraction_queue` schema, row policies, and worker wildcards | Iris |
| API-side back-pressure check against `RAG_INGESTION_BUFFER_MAX_ROWS` | Iris |
| Running the single ingestion worker process (the worker binary itself) | Iris |
| **Provisioning the worker CH account** (`CREATE USER`, password, network reachability) | Operator |
| **Enforcing the single-worker constraint** (one Deployment replica, lease, systemd unit, etc.) | Operator |
| **Assigning `auth_id` to each document** | Operator's classification process |
| Wiring `auth_id` into manifests / dirs / API calls | Operator |
| Provisioning `rag_acl` rows before documents arrive | Operator |
| Choosing and configuring the embedding model (`.rag_env`) | Operator |
| Setting `RAG_INGESTION_BUFFER_MAX_ROWS` (`.rag_env`) | Operator |
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
