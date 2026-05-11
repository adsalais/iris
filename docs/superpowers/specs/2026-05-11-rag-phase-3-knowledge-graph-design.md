# RAG phase 3 — knowledge graph extension — high-level spec

**Status:** design only.
**Date:** 2026-05-11.
**Series:**
- Phase 1 (`2026-05-11-rag-phase-1-vector-rag-with-acl-design.md`) — vector RAG with row-policy ACL.
- Phase 2 (`2026-05-11-rag-phase-2-ingestion-design.md`) — data ingestion pipeline.
- **Phase 3 (this spec)** — knowledge graph extension.
- Phase 4 (`2026-05-11-rag-phase-4-stix-vocab-and-bootstrap-design.md`) — STIX vocabulary + bootstrap.

## Goal

Augment phase 1's vector RAG with a knowledge graph:

- Extract typed entities and relationships from each ingested chunk via
  an LLM with a fixed schema.
- Resolve entity mentions into canonical nodes with a hybrid strategy
  (deterministic → embedding cluster → LLM pairwise).
- Resolve cross-document coreference (pronouns, definite descriptions)
  into canonical entities.
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
3. Extraction pipeline (LLM-driven, per chunk; runs after phase-2 chunk
   write).
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
intentionally out. For DFIR-style workloads, almost every analyst
question is entity-anchored ("what does T1059.001 do?", "which past
cases mention this hash?"). The regular entity-match + 1–2-hop
traversal handles those. Global / aggregative questions are rare and
not the bread-and-butter; they don't justify the cost (Leiden + a
worker + question classifier + new table + partition coordination).

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
for DFIR deployments.

**Entity types:** `Person | Organization | Location | Concept | Document | Event | Product`

**Relation types:** `AUTHORED | AFFILIATED_WITH | LOCATED_IN | MENTIONS | CITES | INTRODUCED | RELATED_TO | PART_OF | OCCURRED_AT | REFERS_TO`

Each entity type may declare typed properties; anything else goes into
a free-form `properties Map(String, String)`.

## Storage layout

Five tables, colocated with phase-1's `rag_embeddings`.

### `kg_mentions_raw` — extractor output, one row per mention

| Column | Type | Notes |
|---|---|---|
| `mention_id` | `UUID` | `uuid5(NS, f"{chunk_id}::{span_start}::{span_end}")`. |
| `chunk_id` | `String` | Joins to `rag_embeddings`. |
| `doc_id` | `String` | Copied for convenience. |
| `auth_id` | `String` | Inherited from the source chunk. Gates row visibility. |
| `entity_type` | `LowCardinality(String)` | From the schema. |
| `name_surface` | `String` | Verbatim surface form (or referring expression for coreference). |
| `aliases` | `Array(String)` | Other surface forms. |
| `mention_kind` | `Enum8('direct' = 1, 'coreference_in_doc' = 2, 'coreference_cross_doc' = 3)` | |
| `properties` | `Map(String, String)` | Free-form. |
| `mention_embedding` | `Array(Float32)` | Embedding of `name + type + context`. |
| `extractor_version` / `prompt_version` | `LowCardinality(String)` | |
| `extracted_at` | `DateTime` | |

Engine: `MergeTree ORDER BY (chunk_id, mention_id)`. Append-only.

### `kg_relations_raw` — extractor output, one row per relation

| Column | Type |
|---|---|
| `relation_id` | `UUID` (`uuid5` of `chunk_id::source_mention_id::target_mention_id::relation_type`) |
| `chunk_id` / `doc_id` / `auth_id` | `String` (inherited from source chunk) |
| `source_mention_id` / `target_mention_id` | `UUID` |
| `relation_type` | `LowCardinality(String)` |
| `evidence` | `String` (verbatim quote) |
| `extractor_version` / `prompt_version` | `LowCardinality(String)` |
| `extracted_at` | `DateTime` |

Engine: `MergeTree ORDER BY (chunk_id, relation_id)`. Append-only.

### `kg_entities` — canonical entities after resolution

| Column | Type |
|---|---|
| `entity_id` | `UUID` (`uuid5(NS, f"{canonical_name_normalized}::{entity_type}")` for LLM-extracted; STIX-native UUIDs for phase-4-bootstrapped entries) |
| `entity_type` | `LowCardinality(String)` |
| `canonical_name` | `String` |
| `aliases` | `Array(String)` |
| `properties_merged` | `Map(String, String)` |
| `auth_ids` | `Array(String)` — union over contributing mentions. ANY-match policy. |
| `resolution_version` | `LowCardinality(String)` |
| `first_seen` / `last_seen` | `DateTime` |

Engine: `ReplacingMergeTree(resolution_version) ORDER BY entity_id`.

### `kg_alias_map` — mention → canonical entity

| Column | Type |
|---|---|
| `mention_id` | `UUID` |
| `entity_id` | `UUID` |
| `auth_id` | `String` (inherited) |
| `resolution_method` | `Enum8('exact' = 1, 'embedding_cluster' = 2, 'llm_judged' = 3, 'coreference' = 4)` |
| `confidence` | `Float32` |
| `resolution_version` | `LowCardinality(String)` |

Engine: `ReplacingMergeTree(resolution_version) ORDER BY mention_id`.

### `kg_edges` — canonical edges, derived

| Column | Type |
|---|---|
| `edge_id` | `UUID` (`uuid5` of `source_entity_id::relation_type::target_entity_id`) |
| `source_entity_id` / `target_entity_id` | `UUID` |
| `relation_type` | `LowCardinality(String)` |
| `evidence_chunks` | `Array(String)` |
| `auth_ids` | `Array(String)` — union over contributing relations. ANY-match policy. |
| `support_count` | `UInt32` |
| `resolution_version` | `LowCardinality(String)` |

Engine: `ReplacingMergeTree(resolution_version) ORDER BY edge_id`.

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

## Extraction pipeline

Runs once per chunk during ingestion (after phase-2 writes the chunk
to `rag_embeddings`). Operator-owned, same boundary as the rest of
the ingestion pipeline.

1. **Fetch chunk content** — same text that was embedded for
   `rag_embeddings`.
2. **Call the schema-guided LLM extractor** — fixed vocabulary, JSON
   output, small context window of neighboring chunks. Extractor emits
   direct mentions + (optionally) in-document coreference mentions.
   Validate against a Pydantic schema; reject and retry on violations.
3. **Compute mention embeddings** — embed
   `f"{entity_type}: {name_surface} | {context_snippet}"`.
4. **Compute deterministic IDs** — `mention_id` as defined above.
5. **Insert** into `kg_mentions_raw` and `kg_relations_raw`,
   propagating `auth_id` onto every row and setting `mention_kind`
   per emission.

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

**Worker access model.** Runs under
`query_as_user(worker_session, ...)` where `worker_session` belongs to
a regular iris user (e.g., `kg-resolver`) — NOT a tier admin. The
worker's groups (e.g., `KG_RESOLVER_GRP`) must be explicitly added to
`rag_acl.allowed_roles` for every `auth_id` the operator wants the
worker to aggregate over. Row policies apply normally. No service-tier
bypass.

Two common shapes:

1. **Single global-ish worker** — granted via every `rag_acl` row.
   Cross-`auth_id` canonical entities (e.g., a public STIX
   `AttackPattern` referenced in a customer chunk resolves to the same
   canonical).
2. **Per-tenant workers** — strict tenant isolation; no cross-tenant
   canonicals.

DFIR deployments typically want shape (1) for public-STIX sharing.

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
   uuid5(NS, f"{canonical_name_normalized}::{entity_type}")`.

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

1. Build `kg_entities` by aggregating per `entity_id`: merge aliases,
   merge properties, pick most frequent surface form as
   `canonical_name`, set `auth_ids = groupUniqArray(auth_id)`.
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
   - Assemble candidates (chunk's own direct mentions + parent doc's
     top-K + neighbor chunks' top-K).
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
       `uuid5(NS, f"{chunk_id}::coref::{entity_id}::{resolution_version}")`
       (not the span-based form, since synthetic mentions have no
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
    structural_edges_cap: int = 50,
) -> SynthesisResult: ...
```

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
| Maintaining schema config (entity types, relation types, normalization) | Operator |
| Running the extraction LLM (incl. in-doc coreference), computing mention embeddings, loading raw tables | Ingestion pipeline |
| Propagating `auth_id` from `rag_embeddings` onto KG rows | Ingestion pipeline |
| Provisioning the worker user (`kg-resolver`) and granting groups in `rag_acl.allowed_roles` | Operator |
| Running the resolution batch job as `query_as_user(worker_session, ...)` | Ingestion pipeline |
| Running the cross-document coreference pass | Ingestion pipeline |
| Provisioning the 5 KG tables with the right engines | Iris (extend Authorization feature's create-database flow) |
| Attaching row policies to the 5 KG tables | Iris |
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
3. **`uuid5` namespace stability.** Fix once and never rotate.
4. **Aggregated-table policy cost at scale.** Measure if popular
   entities accumulate hundreds of `auth_ids`.
5. **Coreference confidence cutoff.** Default 0.75; tune after measuring
   false-merge rate.
6. **Worker scope shape.** Single global-ish vs. per-tenant. DFIR
   with public STIX favors global-ish.
7. **When to revisit communities.** Add a metric: count of questions
   that fail the local path. Re-evaluate after a few months.
