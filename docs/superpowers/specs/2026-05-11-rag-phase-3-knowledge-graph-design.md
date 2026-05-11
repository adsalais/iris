# RAG phase 3 — knowledge graph extension — high-level spec

**Status:** design only.
**Date:** 2026-05-11.
**Series:**
- Phase 1 (`2026-05-11-rag-phase-1-vector-rag-with-acl-design.md`) — vector RAG with row-policy ACL.
- Phase 2 (`2026-05-11-rag-phase-2-ingestion-design.md`) — data ingestion pipeline.
- **Phase 3 (this spec)** — knowledge graph extension.
- Phase 4 (`2026-05-11-rag-phase-4-stix-vocab-and-bootstrap-design.md`) — STIX vocabulary + connector.

## Goal

Augment phase 1's vector RAG with a knowledge graph:

- Extract typed entities and relationships from each ingested chunk via
  an LLM with a fixed schema.
- Resolve entity mentions into canonical nodes with a hybrid strategy
  (deterministic → embedding cluster → LLM pairwise).
- Optionally resolve cross-document coreference (pronouns, definite
  descriptions) into canonical entities — off by default; enable
  when corpus quality demands it.
- Expose the artefacts (`kg_entities`, `kg_edges`) to the phase-1
  synthesis stage as a `STRUCTURAL CONTEXT` block that lets the LLM
  ground answers in graph structure as well as raw chunks.
- All KG tables sit inside the same authorization boundary as
  `rag_embeddings` — the row-dict-policy substrate is extended to gate
  every read of mentions, relations, entities, edges, and alias
  mappings.

## Scope

In scope:
1. KG schema (entity types + relation types).
2. ClickHouse storage layout — 5 KG tables, all row-policied.
3. Extraction worker (LLM-driven, per chunk; async consumer of
   `kg_extraction_queue` written by phase 2).
4. Hybrid entity-resolution pipeline (deterministic → embedding cluster
   → LLM pairwise) + Stage 1.5 merge into pre-existing canonicals.
5. Cross-document coreference (in-document during extraction;
   cross-document as a post-resolution pass).
6. Graph-path query alongside phase 1's vector path.
7. Synthesis-stage upgrade: `STRUCTURAL CONTEXT` block fed by `kg_edges`.

Out of scope:
- **Community detection / hierarchical summarization** (GraphRAG-style
  global-question routing) — deferred. See "Deferred to a later phase"
  below.
- STIX ingestion (phase 4).
- Streaming / incremental resolution.
- Cross-language entity resolution beyond what the embedding model
  gives for free.

## Deferred to a later phase

**Community detection + per-community LLM summarization** is
intentionally out. For the kinds of queries this system targets
(entity-anchored: "what does this technique do?", "which documents
mention this person?", "which threads cite this customer?"), the
regular entity-match + 1–2-hop traversal handles them. Global /
aggregative questions ("what are the dominant themes in this
corpus?") are rare and not the bread-and-butter; they don't justify
the cost (Leiden + a worker + question classifier + new table +
partition coordination).

Communities sit *on top of* `kg_edges` without changing it, so adding
them later is a clean follow-on. Trigger metric: count of questions
classified as "global" or that fail to find a satisfying answer via the
local path.

## Authorization stance

**All KG tables sit inside iris's auth boundary, using the same
`rag_acl_dict` substrate as `rag_embeddings`.** No structural metadata
about entities or edges leaks to a user who has no authorized evidence
for them.

Two flavors of row policy are needed:

1. **Per-row tables** (`kg_mentions_raw`, `kg_relations_raw`,
   `kg_alias_map`) carry a single `auth_id String` column, inherited
   from the source chunk. Same policy expression as `rag_embeddings`.
2. **Aggregated tables** (`kg_entities`, `kg_edges`) carry an
   `auth_ids Array(String)` column — the union of `auth_id`s of all
   contributing mentions / relations. Row policy uses ANY-match
   semantics.

**Visibility semantics:**

- A user can see an entity iff they can read at least one chunk that
  mentions it.
- A user can see an edge iff they can read at least one chunk that
  evidences it.
- Entity/edge names are never exposed to a user who has no authorized
  evidence for them.
- A user reading an authorized `kg_edges` row sees all `chunk_id`s in
  `evidence_chunks` — including any whose underlying `rag_embeddings`
  row they cannot read. CH row policies are row-level, not
  column-level; the synthesis stage's structural-block filter masks
  the unauthorized `chunk_id`s before they reach the LLM prompt.

**Performance note.** The aggregated-table policy does `arrayExists`
over `auth_ids × currentRoles()` with a `dictGet` per pair. Cost
scales with `|auth_ids| × |currentRoles|`. Worth monitoring if popular
entities accumulate hundreds of `auth_ids`.

## Schema (starter, operator-editable)

Loaded as config by both extractor and resolver. Operators tune for
their corpus; phase 4 provides a tool to derive this from STIX bundles
for deployments that share canonical entities across tenants (e.g.,
a public threat-intel bundle referenced from private case files; a
shared company-glossary referenced from per-team documentation).

**Entity types:** `Person | Organization | Location | Concept | Document | Event | Product`

**Relation types:** `AUTHORED | AFFILIATED_WITH | LOCATED_IN | MENTIONS | CITES | INTRODUCED | RELATED_TO | PART_OF | OCCURRED_AT | REFERS_TO`

Each entity type may declare typed properties; anything else goes into
a free-form `properties Map(String, String)`.

## Storage layout

Five tables, colocated with phase-1's `rag_embeddings`.

### `kg_mentions_raw` — extractor output, one row per mention

| Column | Type | Notes |
|---|---|---|
| `mention_id` | `UUID` | `uuid5(chunk_id, f"{span_start}::{span_end}")` — `chunk_id` is the namespace (see Phase 1 "UUID derivation"). |
| `chunk_id` | `UUID` | Joins to `rag_embeddings`. UUID-typed everywhere; see Phase 1's UUID derivation table for the formula. |
| `doc_id` | `UUID` | Copied for convenience. |
| `auth_id` | `String` | Inherited from the source chunk. Gates row visibility. |
| `entity_type` | `LowCardinality(String)` | From the schema. |
| `name_surface` | `String` | Verbatim surface form (or referring expression for coreference). |
| `aliases` | `Array(String)` | Other surface forms. |
| `mention_kind` | `Enum8('direct' = 1, 'coreference_in_doc' = 2, 'coreference_cross_doc' = 3)` | |
| `properties` | `Map(String, String)` | Free-form. |
| `mention_embedding` | `Array(Float32)` | Embedding of `name + type + context`. HNSW ANN index for Stage 2 nearest-neighbour lookups. |
| `extractor_version` / `prompt_version` | `LowCardinality(String)` | |
| `extracted_at` | `DateTime` | |

Engine:
```sql
ENGINE = ReplacingMergeTree(extracted_at)
PARTITION BY toYYYYMM(extracted_at)
ORDER BY (chunk_id, mention_id)
```

Re-running extraction over the same chunk (STIX bundle refresh,
new `prompt_version`) keeps the newest row by `extracted_at`. Read
via a view that applies `FINAL`. ANN index:

```sql
-- iris's DDL helper substitutes <dim> from RAG_EMBEDDING_VECTOR_SIZE
ALTER TABLE kg_mentions_raw ADD INDEX mention_embedding_hnsw
mention_embedding
TYPE vector_similarity('hnsw', 'cosineDistance', <dim>)
GRANULARITY 1
```

The dimension reuses `RAG_EMBEDDING_VECTOR_SIZE` on the assumption
that mentions and chunks share an embedding model (v1 default). If
the mention-embedding model differs (see Open Question 1), introduce
a separate `RAG_MENTION_EMBEDDING_VECTOR_SIZE` and reference it here.
CH DDL doesn't expand env vars; iris's helper formats the integer
into the DDL string before issuing the statement (same mechanism as
Phase 1's `rag_embeddings` vector index).

### `kg_relations_raw` — extractor output, one row per relation

| Column | Type |
|---|---|
| `relation_id` | `UUID` — `uuid5(chunk_id, f"{source_mention_id}::{target_mention_id}::{relation_type}")` (chunk_id as namespace). |
| `chunk_id` | `UUID` (inherited; same unified type as `kg_mentions_raw.chunk_id`) |
| `doc_id` | `UUID` |
| `auth_id` | `String` |
| `source_mention_id` / `target_mention_id` | `UUID` |
| `relation_type` | `LowCardinality(String)` |
| `evidence` | `String` (verbatim quote) |
| `extractor_version` / `prompt_version` | `LowCardinality(String)` |
| `extracted_at` | `DateTime` |

Engine:
```sql
ENGINE = ReplacingMergeTree(extracted_at)
PARTITION BY toYYYYMM(extracted_at)
ORDER BY (chunk_id, relation_id)
```

Same refresh semantics as `kg_mentions_raw`.

### `kg_entities` — canonical entities after resolution

| Column | Type |
|---|---|
| `entity_id` | `UUID` — `uuid5(NS_ENTITY, f"{entity_type}::{canonical_name_normalized}")` for LLM-extracted; STIX-native UUIDs for phase-4-bootstrapped entries. (`NS_ENTITY` defined in Phase 1's UUID-derivation section.) |
| `entity_type` | `LowCardinality(String)` |
| `canonical_name` | `String` |
| `canonical_name_normalized` | `String` | Normalized form (the input to `entity_id`'s uuid5). Stored explicitly so Stage 1.5 can do a primary-key lookup on the table's ORDER BY tuple `(entity_type, canonical_name_normalized)` instead of recomputing per query. |
| `aliases` | `Array(String)` |
| `metadata` | `String` (JSON-encoded) — single generic blob for all non-graph entity data: external identifiers, source provenance, status flags, anything connector-specific. Read primarily by the application layer at synthesis time (rare manual SELECT on this column); operators don't routinely filter by its contents. Examples: STIX connector writes `{"mitre_attack": "T1059", "cve": null, "stix": "attack-pattern--abc", "stix_revoked": false, "kill_chain_phases": [...]}`. JIRA connector writes `{"jira": "PROJ-123", "status": "open"}`. Email connector writes `{"message_id": "<...>", "thread_id": "..."}`. No schema migration when a new connector starts emitting new keys. |
| `representative_embedding` | `Array(Float32)` — centroid of contributing direct-mention `mention_embedding`s, computed at Stage 5. Used by Stage 2's pre-existing-canonical lookup AND by the graph-path query's question-entity matching. For STIX-bootstrapped entities with one synthetic mention, the centroid equals that single mention's embedding. HNSW ANN index. |
| `auth_ids` | `Array(String)` — union over contributing mentions. ANY-match policy. |
| `normalization_rules_hash` | `LowCardinality(String)` — hash of the normalization rules used to compute this entity's `canonical_name`. Lets drift be detected: when the rules-hash changes, the operator knows entity_ids must be re-derived. |
| `resolution_version` | `LowCardinality(String)` |
| `first_seen` / `last_seen` | `DateTime` |

Engine:
```sql
ENGINE = ReplacingMergeTree(resolution_version)
ORDER BY (entity_type, canonical_name_normalized)
```

The `(entity_type, canonical_name_normalized)` tuple is the entity's
logical primary key — `entity_id` is
`uuid5(NS_ENTITY, f"{entity_type}::{canonical_name_normalized}")` for
LLM-extracted entries, and STIX-bootstrapped entries get distinct
normalized names by virtue of distinct STIX content. ReplacingMergeTree dedup by this tuple matches the semantic
invariant ("one canonical entity per type + normalized name") and
makes Stage 1.5's lookup a direct primary-key match without an
auxiliary projection.

Direct point-lookups by `entity_id` (`WHERE entity_id = ?`) scan, but
all entity_id access in practice is JOIN-shaped (`kg_alias_map ⨝
kg_entities`, `kg_edges ⨝ kg_entities`) which CH executes as a hash
JOIN; ORDER BY doesn't affect hash JOIN performance. So the
reordering is a free win for Stage 1.5 with no impact on the
downstream graph-path JOINs.

ANN index (iris's DDL helper substitutes `<dim>` from
`RAG_EMBEDDING_VECTOR_SIZE` before issuing — CH DDL doesn't expand env
vars):
```sql
ALTER TABLE kg_entities ADD INDEX repr_embedding_hnsw
representative_embedding
TYPE vector_similarity('hnsw', 'cosineDistance', <dim>)
GRANULARITY 1
```

If the mention-embedding model differs from the chunk-embedding
model (see Open Question 1), introduce a separate
`RAG_MENTION_EMBEDDING_VECTOR_SIZE` and use it here.

The `metadata` column has **no index** by design. Queries that need
to filter by JSON contents (e.g., `JSONExtractBool(metadata,
'stix_revoked') = true`) full-scan the surviving granules after the
ANN / primary-key prefilter. That's acceptable because the
manual-SELECT-on-metadata workload is rare; the synthesis stage's hot
path reads `metadata` per-row only for chunks already narrowed by
the row-policied retrieval.

### `kg_entity_aliases_mv` — alias → entity lookup (materialized view)

Stage 1.5's "OR `normalized_name` appears in `aliases`" clause cannot
use the main table's `(entity_type, canonical_name_normalized)`
primary key because aliases is an Array. A small materialized view
unnests the array and indexes by it:

```sql
CREATE MATERIALIZED VIEW kg_entity_aliases_mv
ENGINE = MergeTree
ORDER BY (entity_type, alias_normalized)
POPULATE
AS SELECT
    entity_id,
    entity_type,
    arrayJoin(aliases) AS alias_raw,
    <normalize_fn>(alias_raw) AS alias_normalized,
    auth_ids
FROM kg_entities
```

`<normalize_fn>` applies the same normalization as `canonical_name_normalized`.
Stage 1.5's alias-match path becomes a point lookup. The MV carries
`auth_ids` so the same ANY-match row-policy expression as `kg_entities`
applies.

### `kg_alias_map` — mention → canonical entity

| Column | Type |
|---|---|
| `mention_id` | `UUID` |
| `entity_id` | `UUID` |
| `auth_id` | `String` (inherited) |
| `resolution_method` | `Enum8('exact' = 1, 'embedding_cluster' = 2, 'llm_judged' = 3, 'coreference' = 4)` |
| `confidence` | `Float32` |
| `resolution_version` | `LowCardinality(String)` |

Engine:
```sql
ENGINE = ReplacingMergeTree(resolution_version)
ORDER BY mention_id
-- projection for graph-path entity -> mentions traversal
PROJECTION by_entity (
    SELECT mention_id, entity_id, auth_id, resolution_method,
           confidence, resolution_version
    ORDER BY (entity_id, mention_id)
)
```

The primary ORDER BY (`mention_id`) is the natural dedup key — one
canonical resolution per mention. The `by_entity` projection makes the
graph-path query (*"for entities X, give me their mentions"*) a
primary-key-style scan instead of a full-table read.

### `kg_edges` — canonical edges, derived

| Column | Type |
|---|---|
| `edge_id` | `UUID` — `uuid5(NS_EDGE, f"{source_entity_id}::{relation_type}::{target_entity_id}")` (`NS_EDGE` defined in Phase 1's UUID-derivation section). |
| `source_entity_id` / `target_entity_id` | `UUID` |
| `relation_type` | `LowCardinality(String)` |
| `evidence_chunks` | `Array(UUID)` (chunk_id type is unified across the schema) |
| `auth_ids` | `Array(String)` — union over contributing relations. ANY-match policy. |
| `support_count` | `UInt32` |
| `resolution_version` | `LowCardinality(String)` |

Engine:
```sql
ENGINE = ReplacingMergeTree(resolution_version)
ORDER BY (source_entity_id, relation_type, target_entity_id)
-- inverse-traversal projection: incoming-edges-for-target queries
PROJECTION by_target (
    SELECT source_entity_id, target_entity_id, relation_type,
           evidence_chunks, auth_ids, support_count, edge_id
    ORDER BY (target_entity_id, relation_type, source_entity_id)
)
```

Primary ORDER BY `(source_entity_id, relation_type, target_entity_id)`
makes the dominant graph-traversal query (*"outgoing edges from these
entities"* — `WHERE source_entity_id IN (...)`) a primary-key scan.
The tuple is unique (it's what `edge_id` is derived from), so
ReplacingMergeTree dedup semantics are preserved without a separate
`edge_id` dedup key. The `by_target` projection covers the inverse
*"incoming edges to entity X"* pattern (e.g. *"who cited this paper?"*,
*"what TTPs target this asset?"*).

### Row policies on KG tables

Installed by iris's Authorization feature alongside the
`rag_embeddings` policy.

**Per-row tables**:

```sql
USING arrayExists(r -> has(
  dictGet('rag_docs.rag_acl_dict', 'allowed_roles', auth_id),
  r
), currentRoles())
```

**Aggregated tables**, ANY-match:

```sql
USING arrayExists(a -> arrayExists(r -> has(
  dictGet('rag_docs.rag_acl_dict', 'allowed_roles', a),
  r
), currentRoles()), auth_ids)
```

Required grants: every tier role attached needs
`GRANT dictGet ON rag_docs.rag_acl_dict`. Single grant covers
phase-1's `rag_embeddings` and all five phase-3 KG tables.

## Extraction worker (async, queue-driven)

**Extraction runs asynchronously**, not in-band with phase-2 ingest.
The phase-2 pipeline writes a row into `kg_extraction_queue` (defined
in phase 2) for every chunk that landed via a non-`pre_extracted`
connector; a separate worker pool consumes the queue and writes
`kg_mentions_raw` / `kg_relations_raw`.

**Consistency window.** The chunk is queryable via the phase-1 vector
path immediately after ingest. It becomes queryable via the phase-3
graph path only after the extraction worker processes it — typically
within minutes, but the SLA is operator-tunable based on worker pool
size and LLM rate limits. There is no time-based ordering guarantee:
older chunks may finish extracting after newer ones if the older one
hit a transient retry.

**Worker access model.** Runs under `query_as_user(worker_session,
...)` where `worker_session` belongs to a regular iris user (e.g.,
`kg-extractor`), NOT a tier admin. The worker's groups must be
explicitly listed in `rag_acl.allowed_roles` for every `auth_id` the
worker should extract from. Row policies apply normally on reads;
INSERTs are gated by table-level `GRANT INSERT` on the KG tables.
**Same access pattern as the phase-2 ingest worker and the
resolution worker below** — three iris workers, three explicit user
identities, all granted via the same `rag_acl`-row mechanism. The
extraction worker may be the same iris user as the resolution worker
or a separate one with a smaller scope.

### Per-task workflow

For each task claimed from `kg_extraction_queue`:

1. **Fetch chunk content** from `rag_embeddings`. Row policy filters
   apply; if the worker can't see the chunk, mark the task `failed`
   with `error = 'unauthorized'` (a misconfiguration signal).
2. **Call the schema-guided LLM extractor** — fixed vocabulary, JSON
   output, small context window of neighboring chunks. Extractor
   emits direct mentions + (optionally) in-document coreference
   mentions. Validate against a Pydantic schema; reject and retry on
   violations.
3. **Compute mention embeddings** — embed
   `f"{entity_type}: {name_surface} | {context_snippet}"`.
4. **Compute deterministic IDs** — `mention_id` as defined above.
5. **Insert** into `kg_mentions_raw` and `kg_relations_raw`,
   propagating `auth_id` onto every row and setting `mention_kind`
   per emission.
6. **Mark the task** `completed` (or `failed` with retry budget).

### Task claim semantics

Workers claim a batch of pending tasks via a **ClickHouse lightweight
update** (CH 24.3+): `ALTER TABLE kg_extraction_queue UPDATE status =
'claimed', claimed_by = <worker_id>, claimed_at = now() WHERE
task_id IN (<read-then-claim batch>) AND status = 'pending'`.
Lightweight updates avoid part rewrites; reads with
`apply_mutations_on_fly = 1` see the claim immediately, so two
workers don't double-claim the same task. Stale claims
(`status = 'claimed' AND claimed_at < now() - 10 minutes`) get
re-claimed by another worker, since the deterministic
`task_id = uuid5(chunk_id, "extract")` makes re-processing
idempotent at the chunk level — if both workers happen to finish,
`ReplacingMergeTree(extracted_at)` on `kg_mentions_raw` keeps the
newest row.

Same `ALTER … UPDATE` mechanism the phase-2 ingest worker uses on
`rag_ingestion_buffer` (stage 8). Both queues rely on CH's
lightweight-mutation guarantees.

Extractor prompt shape:

```
You are an entity-relation extractor. Output JSON of this shape:
{
  "entities": [
    {"local_id": <int>, "type": <EntityType>, "name": <str>,
     "kind": "direct" | "coreference_in_doc",
     "aliases": [<str>...], "properties": {<str>: <str>},
     "refers_to_local_id": <int|null>}
  ],
  "relations": [
    {"source": <local_id>, "target": <local_id>,
     "type": <RelationType>, "evidence": <verbatim quote>}
  ]
}

Constraints:
- `type` values must come from the supplied vocabulary.
- `evidence` must be a verbatim quote from the input text.
- For `kind = "coreference_in_doc"`, `refers_to_local_id` must point
  to a `direct` entity in this same response.
- If uncertain, omit rather than guess.
```

## Hybrid resolution pipeline

Batch job at operator-controlled cadence (e.g. nightly). Each run
bumps `resolution_version`.

**Worker access model.** Same as the extraction worker, with a
different iris user (e.g., `kg-resolver`) whose groups are granted
in `rag_acl.allowed_roles` for every `auth_id` the operator wants
the worker to aggregate over.

Two common shapes:

1. **Single global-ish worker** — granted via every `rag_acl` row.
   Cross-`auth_id` canonical entities (e.g., a public STIX
   `AttackPattern` referenced in a customer chunk resolves to the same
   canonical).
2. **Per-tenant workers** — strict tenant isolation; no cross-tenant
   canonicals.

Deployments sharing canonical entities across tenants (e.g.
public-STIX bundles, company-wide glossaries) typically want shape (1).

### Stage 1 — deterministic normalization + exact-match merge

1. Normalize each direct mention name per entity type: lowercase, strip
   diacritics, collapse whitespace, strip suffixes (`Inc.`, `Ltd.`,
   `Dr.`).
2. Group by `(entity_type, normalized_name)`.
3. Assign `entity_id` to unambiguous groups; write `kg_alias_map` rows
   with `resolution_method = 'exact'`.

Captures 60–80% of direct mentions cheaply.

### Stage 1.5 — merge into pre-existing canonicals

**Critical for phase 4's STIX bootstrap to work.** Before creating a
*new* canonical from a Stage 1 group, look up whether an existing
`kg_entities` row already covers this canonical_name + entity_type:

1. For each Stage 1 group `(entity_type, normalized_name)`, query
   `kg_entities` for any row whose `entity_type` matches AND
   (`canonical_name` normalizes the same OR `normalized_name` appears
   in `aliases`).
2. Exactly one match → assign that pre-existing `entity_id` to every
   mention in the group; write `kg_alias_map` with `method = 'exact'`.
   Do NOT create a new canonical.
3. Multiple matches → defer to Stage 3.
4. No match → create new canonical in Stage 2 / 5 with `entity_id =
   uuid5(NS_ENTITY, f"{entity_type}::{canonical_name_normalized}")`.

This is what lets LLM-extracted mentions ("T1059", "Mimikatz", "APT29")
from later corpus ingests resolve into the STIX-sourced canonicals
(whose `entity_id` is a STIX-native UUID, not the `uuid5` form).
Without Stage 1.5, the resolver would silently fork.

### Stage 2 — embedding clustering, within entity_type

For direct mentions not assigned in Stage 1:

1. Per `entity_type`, run agglomerative clustering on
   `mention_embedding` with a tight cosine threshold (start: 0.90).
2. For each cluster, **first check overlap with pre-existing
   `kg_entities`** by computing the cluster centroid's nearest
   neighbours; if the nearest exceeds the threshold, merge into it.
3. Otherwise fall back to Stage 1 / 1.5 canonicals; otherwise create
   new.
4. Write `kg_alias_map` with `method = 'embedding_cluster'`.

### Stage 3 — LLM pairwise judge on the ambiguous tail

For direct-mention pairs in the 0.75–0.90 embedding-similarity band
with differing normalized names:

1. Block by `(entity_type, prefix)` to bound the comparison set.
2. Prompt:
   ```
   Are these the same entity?
   A: {name_a, context_a, properties_a}
   B: {name_b, context_b, properties_b}
   Answer JSON: {same: bool, confidence: float, reason: str}
   ```
3. Apply a confidence cutoff and write `kg_alias_map` with
   `method = 'llm_judged'`.

Blocking keeps the workload to ~thousands of pairs per batch.

### Stage 4 — in-document coreference write-through

Coreference mentions emitted with `refers_to_local_id` already carry
the resolved target. The resolver writes `kg_alias_map` rows with
`method = 'coreference'`.

### Stage 5 — derive `kg_entities` and `kg_edges`

1. Build `kg_entities` by aggregating per `entity_id`:
   - merge `aliases` (`groupUniqArray`),
   - merge `metadata` — JSON-object merge across contributing
     mentions, last-write-wins per top-level key. Conflicts on stable
     identifier keys (`mitre_attack`, `cve`, etc.) are logged as
     upstream data inconsistency.
   - pick the most frequent surface form (among direct mentions
     only) as `canonical_name`,
   - **compute `representative_embedding` as the L2-normalized
     centroid of contributing direct-mention `mention_embedding`s**;
     coreference mentions are excluded from the centroid to avoid
     biasing it with anaphora context,
   - set `auth_ids = groupUniqArray(auth_id)` over ALL contributing
     mentions (direct + coreference) so the visibility policy
     reflects everywhere the entity is referenced,
   - set `normalization_rules_hash` to the hash of the active
     normalization-rules config (Stage 1 / Stage 1.5 inputs).
2. Derive `kg_edges` by joining `kg_relations_raw` to `kg_alias_map`
   on both endpoints, aggregating to
   `(source_entity_id, relation_type, target_entity_id)` with
   `groupArray(chunk_id)` as `evidence_chunks`, `count()` as
   `support_count`, `groupUniqArray(auth_id)` as `auth_ids`.

## Cross-document coreference (post-resolution pass)

Resolves referring expressions ("it", "they", "the company", "this
technique") that the in-document extractor couldn't link because the
referent lives in another chunk or document.

**Triggering signal.** The extractor flags chunks with unresolved
references via a JSON field: `unresolved_references: [{span, hint}]`.

**Pass workflow** (after Stage 5):

1. For each chunk with unresolved references:
   - Assemble candidates capped at **K=10 per pool, 30 total**:
     - The chunk's own direct-mention entities (all, up to 10).
     - The parent document's top-10 most-mentioned entities (by
       count of direct mentions in the doc).
     - The neighboring chunks' (window: ±2 chunks by ordinal) top-10
       most-mentioned entities, excluding any already covered above.
   - Deduplicate by `entity_id` so the same entity doesn't appear in
     two pools.
   - Prompt the LLM:
     ```
     In this chunk, does any unresolved referring expression refer to
     one of these candidate entities?
     Chunk: <text>
     Unresolved spans: <list>
     Candidates: [{entity_id, canonical_name, type, summary_snippet}, ...]
     Answer JSON: [{span, entity_id|null, confidence}]
     ```
   - For each non-null answer above the cutoff:
     - Synthesize a `kg_mentions_raw` row. `mention_id` is derived as
       `uuid5(chunk_id, f"coref::{entity_id}::{resolution_version}")`
       (chunk_id as namespace, like direct mentions; the name encodes
       coref-specific disambiguation since synthetic mentions have no
       span).
     - `mention_kind = 'coreference_cross_doc'`, `auth_id` from the
       chunk, `name_surface` = the referring expression text.
     - Write `kg_alias_map` row pointing at the resolved `entity_id`
       with `method = 'coreference'`.

**Cost.** One LLM call per chunk that flagged unresolved references.
Off by default; enable when corpus quality demands it.

**Re-derivation.** Re-run Stage 5's `kg_entities` / `kg_edges`
derivation so coreference mentions contribute to `auth_ids` and edge
`support_count`.

## Query path alongside vector RAG

Both paths run on the user's `DatabaseSession`; every read traverses a
row-policied table.

**Vector path** (phase-1, unchanged): top-K from `rag_embeddings`.

**Graph path:**
1. Lightweight extractor on the question → question entities +
   optional relation hints.
2. Match to `kg_entities` by name-embedding similarity with exact-alias
   fallback. Row-policy filtered.
3. Traverse `kg_edges` 1–2 hops from matched entities, optionally
   filtering by relation type. Row-policy filtered.
4. Follow `kg_alias_map` → `kg_mentions_raw.chunk_id` to assemble
   candidate chunks. Row-policy filtered.
5. Fetch from `rag_embeddings` — same row policy as final defense.

Merge vector + graph chunks, dedup by `(doc_id, chunk_id)`, rerank,
synthesize.

## Synthesis upgrade (extends phase 1)

Phase 1 ships a single `SOURCES`-only prompt. Phase 3 adds a
`STRUCTURAL CONTEXT` block between SYSTEM and SOURCES:

```
[STRUCTURAL CONTEXT]
The following relationships are present in the sources:
- <entity_name_A> (<entity_type_A>) --[<relation_type>]--> <entity_name_B> (<entity_type_B>)
  evidence: [C3, C7]
...
```

System prompt gains:

> When a STRUCTURAL CONTEXT block is provided, treat it as a summary
> of known relationships extracted from the same sources. You may use
> it to plan your answer, but every claim in your final answer must
> still cite a specific [C<n>] source — not the structural context
> itself.

### Pre-synthesis pipeline (phase-3 form)

```
[vector chunks]   [graph chunks]
       \             /
        dedup by chunk_id  (mark dual-source "high-confidence")
                |
                v
       authorized_chunk_ids = {c.chunk_id for c in unioned set}
       (the BROAD set, before rerank/truncate)
                |
                v
       cross-encoder rerank vs question
                |
                v
       truncate to top-N  -> selected_chunks
                |
                v
       build structural block from kg_edges
       constraint: every included edge has at least one
       chunk_id in evidence_chunks ∩ authorized_chunk_ids
                |
                v
       construct prompt (now with STRUCTURAL CONTEXT)
                |
                v
       LLM → parse citations → answer
```

The structural-block authorization filter is **the row-level → cell-
level masking step**: `kg_edges`' row policy already enforced "user can
see this edge"; this step masks individual `chunk_id`s in the edge's
`evidence_chunks` array down to those the user can fetch from
`rag_embeddings`.

### Caller shape (extends phase 1)

```python
async def synthesize(
    session: DatabaseSession,
    question: str,
    *,
    vector_k: int = 24,
    graph_hops: int = 2,
    final_n: int = 12,
    structural_block_max_tokens: int = 1024,
) -> SynthesisResult: ...
```

The structural block is bounded by **token budget**, not edge count.
Edges are added in order of `relevance_to_question_entities × support_count`
until the rendered block (entity names + types + relation type +
evidence list) reaches `structural_block_max_tokens`. This keeps a
predictable prompt size regardless of how verbose canonical names get
in a given corpus (a `course-of-action` entity with a long mitigation
title eats far more tokens than a `Person` entity).

Phase-3 internal steps that differ from phase 1:

1. Embed the question.
2. In parallel: vector-path query; **graph-path query** (both on
   `session`).
3. Union, dedup → `retrieval_set`.
4. `authorized_chunk_ids = {c.chunk_id for c in retrieval_set}`
   (BROAD, pre-rerank).
5. Rerank + truncate → `selected_chunks`.
6. Build structural block from `kg_edges`, filtered against
   `authorized_chunk_ids` (not `selected_chunks`).
7. Render prompt (with STRUCTURAL CONTEXT block); call LLM.
8. Parse citations → `SynthesisResult`.

## What iris owns vs. what the operator owns

| Concern | Owner |
|---|---|
| Maintaining schema config (entity types, relation types, normalization rules) | Operator |
| Provisioning the extraction-worker user (`kg-extractor`) and granting its groups in `rag_acl.allowed_roles` | Operator |
| Provisioning the resolution-worker user (`kg-resolver`) and granting its groups in `rag_acl.allowed_roles` | Operator |
| Running the extraction worker pool (consumes `kg_extraction_queue`, calls the LLM extractor, writes `kg_mentions_raw` / `kg_relations_raw`) | Iris (worker process; operator scales it) |
| Running the resolution batch job (Stages 1–5) as `query_as_user(kg-resolver, ...)` on the operator's cron | Iris (worker process; operator schedules and scales) |
| Running the cross-document coreference pass (optional) | Iris (worker process; operator schedules and scales) |
| Propagating `auth_id` from `rag_embeddings` onto KG rows | Extraction worker (queue-driven) / phase-2 pipeline (for `pre_extracted` connectors) |
| Provisioning the 5 KG tables + `kg_entity_aliases_mv` with the right engines | Iris (extend Authorization feature's create-database flow) |
| Attaching row policies to the 5 KG tables + MV | Iris |
| Graph-path execution + structural-block build | Iris RAG feature module |
| Updated synthesis prompt template | Iris |

## Tests

Phase-3 tests use the phase-1 `rag_env` fixture (skip-on-missing).
Additional test surface:

- Resolution-pipeline unit tests on synthetic mentions (no LLM, no CH).
- End-to-end (requires `.rag_env`): ingest fixture chunks, run
  extraction, run resolution, query via graph-path, verify row-policy
  filtering against `auth_id`-restricted users.
- Stage 1.5 merge test: pre-create a canonical entity (simulating
  phase-4 STIX bootstrap), then ingest a chunk mentioning it; verify
  the LLM-extracted mention resolves to the pre-existing canonical
  rather than creating a duplicate.

## Non-goals

- No community detection / hierarchical summarization in v1 (see
  Deferred section).
- No automatic schema-drift detection — schema is operator-curated.
- No cross-language entity resolution beyond what the embedding model
  gives for free.
- No column-level masking of `evidence_chunks` arrays (the synthesis
  filter handles that at prompt-render time).
- No reified coreference chains (single-step link only).

## Open questions

1. **Embedding model for mentions vs. chunks.** Same = simpler; cheaper
   for short-string tasks usually fine. Pick after benchmark.
2. **Stage 2 / Stage 3 thresholds.** Tune on real data. Start 0.90 /
   0.75–0.90.
3. **`uuid5` namespace AND normalization-rules stability.** Both are
   load-bearing inputs to `entity_id` derivation. Fix the namespace
   UUID once at deployment and never rotate. Treat the normalization-
   rules config (suffix-strip lists, diacritic handling, etc.) as
   equally immutable; any change requires a full re-resolution that
   rewrites `entity_id`s and rebuilds the alias map. The
   `kg_entities.normalization_rules_hash` column lets drift be
   detected automatically — a resolution-job preflight refuses to
   run if the active rules hash diverges from the most recent
   `kg_entities` row, unless the operator passes
   `--rewrite-entity-ids` (a deliberate destructive flag).
4. **Aggregated-table policy cost at scale.** `arrayExists × arrayExists`
   with a `dictGet` inside is O(|auth_ids| × |currentRoles|) per row.
   Fine for small arrays; measure if popular entities accumulate
   hundreds of `auth_ids`.
5. **Coreference confidence cutoff.** Default 0.75; tune after measuring
   false-merge rate.
6. **Worker scope shape.** Single global-ish vs. per-tenant.
   Deployments with shared cross-tenant content (public threat intel,
   company-wide glossaries) favor global-ish; strict tenant isolation
   favors per-tenant.
7. **Graph-path query performance.** Step 4 of the graph path traverses
   `kg_entities → kg_edges → kg_alias_map → kg_mentions_raw →
   rag_embeddings` — four joins, all on UUIDs, all row-policy
   filtered. Measure end-to-end latency on a representative corpus
   before committing to no materialization. If hot, materialize an
   `entity_chunks_mv` (`entity_id, chunk_id, auth_id`) view that
   collapses `kg_alias_map ⨝ kg_mentions_raw` and lets the graph path
   skip two joins. The materialized view inherits the per-row policy
   shape and stays consistent with the underlying tables via standard
   CH MV semantics.
8. **When to revisit communities.** Trigger metric: ratio of
   `synthesize()` calls returning the explicit
   "sources don't support an answer" refusal text (parsed
   post-hoc from the audit record) to total calls, computed over a
   rolling 14-day window. Re-evaluate communities if this ratio
   exceeds 10% sustained — that's the empirical signal that the
   local path is missing aggregative answers.
