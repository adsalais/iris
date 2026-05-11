# RAG knowledge-graph extraction and resolution — high-level spec

**Status:** design only.
**Date:** 2026-05-11.
**Companion to:** `2026-05-11-rag-row-policy-acl-design.md` (the chunk-level vector RAG spec).

## Goal

Augment iris's vector RAG with a knowledge graph: extract typed entities and
relationships from each indexed chunk using an LLM with a fixed schema,
resolve the entity mentions into canonical nodes with a hybrid strategy, and
store the result in ClickHouse so query-time graph traversal can run
alongside vector search. **All KG tables sit inside the same authorization
boundary as `rag_embeddings`** — the row-dict-policy substrate is extended
to gate every read of mentions, relations, entities, edges, and alias
mappings.

## Scope

In scope:
1. Schema (entity types + relation types).
2. ClickHouse storage layout (5 tables, all row-policied).
3. Extraction pipeline (LLM-driven, per chunk).
4. Hybrid entity-resolution pipeline (deterministic → embedding cluster → LLM pairwise).
5. How the KG plugs into query time alongside vector search.

Out of scope: the synthesis prompt that fuses graph and vector context;
community detection / hierarchical summaries; cross-document coreference
beyond entity resolution.

## Authorization stance

**The KG tables sit inside iris's auth boundary, using the same
`rag_acl_dict` substrate as `rag_embeddings`.** No structural metadata
about entities or edges leaks to a user who has no authorized evidence for
them.

Two flavors of row policy are needed:

1. **Per-row tables** (`kg_mentions_raw`, `kg_relations_raw`,
   `kg_alias_map`) carry a single `auth_id String` column, inherited from
   the source chunk. They use the same row policy expression as
   `rag_embeddings`.
2. **Aggregated tables** (`kg_entities`, `kg_edges`) carry an
   `auth_ids Array(String)` column — the union of `auth_id`s of all
   contributing mentions/relations. Their row policy uses ANY-match
   semantics: the row is visible if the user has access to at least one
   of the contributing `auth_id`s.

**Visibility semantics this gives:**

- A user can see an entity iff they can read at least one chunk that
  mentions it.
- A user can see an edge iff they can read at least one chunk that
  evidences it.
- Entity/edge names are never exposed to a user who has no authorized
  evidence for them.
- A user reading an authorized `kg_edges` row sees all `chunk_id`s in
  its `evidence_chunks` array — including any whose underlying
  `rag_embeddings` row they cannot read. CH row policies are row-level,
  not column-level; the synthesis stage's structural-block filter masks
  the unauthorized `chunk_id`s before they reach the LLM prompt. That
  masking is the only remaining defense for `evidence_chunks` contents,
  and the chunk content itself stays gated by the row policy at fetch.

**Performance note.** The aggregated-table policy does `arrayExists` over
`auth_ids × currentRoles()` with a `dictGet` per pair. Cost scales with
`|auth_ids| × |currentRoles|`. Both are typically small (a few each).
Worth monitoring if entity-level `auth_ids` arrays grow into the
hundreds for popular entities.

## Schema (starter, operator-editable)

Loaded as config by both extractor and resolver.

**Entity types:** `Person | Organization | Location | Concept | Document | Event | Product`

**Relation types:** `AUTHORED | AFFILIATED_WITH | LOCATED_IN | MENTIONS | CITES | INTRODUCED | RELATED_TO | PART_OF | OCCURRED_AT | REFERS_TO`

Each entity type may declare a small set of typed properties (e.g.
`Person.affiliation`, `Document.year`); anything outside the typed set goes
into a free-form `properties Map(String, String)`.

## Storage layout

Five tables, colocated with embeddings in the RAG database (e.g. `rag_docs`).

### `kg_mentions_raw` — extractor output, one row per mention

| Column | Type | Notes |
|---|---|---|
| `mention_id` | `UUID` | `uuid5(NS, f"{chunk_id}::{span_start}::{span_end}")` — stable across re-extraction. |
| `chunk_id` | `String` | Joins to `rag_embeddings`. |
| `doc_id` | `String` | Copied for convenience. |
| `auth_id` | `String` | Inherited from the source chunk in `rag_embeddings`. Gates row visibility. |
| `entity_type` | `LowCardinality(String)` | From the schema. |
| `name_surface` | `String` | Verbatim surface form. |
| `aliases` | `Array(String)` | Other surface forms the LLM emitted. |
| `properties` | `Map(String, String)` | Free-form properties. |
| `mention_embedding` | `Array(Float32)` | Embedding of `name + type + context`. |
| `extractor_version` | `LowCardinality(String)` | Pipeline version stamp. |
| `prompt_version` | `LowCardinality(String)` | Prompt revision stamp. |
| `extracted_at` | `DateTime` | |

Engine: `MergeTree ORDER BY (chunk_id, mention_id)`. Append-only.

### `kg_relations_raw` — extractor output, one row per relation

| Column | Type |
|---|---|
| `relation_id` | `UUID` (`uuid5` of `chunk_id::source_mention_id::target_mention_id::relation_type`) |
| `chunk_id` | `String` |
| `doc_id` | `String` |
| `auth_id` | `String` (inherited from the source chunk) |
| `source_mention_id` | `UUID` (FK to `kg_mentions_raw.mention_id`) |
| `target_mention_id` | `UUID` |
| `relation_type` | `LowCardinality(String)` |
| `evidence` | `String` (verbatim quote) |
| `extractor_version` | `LowCardinality(String)` |
| `prompt_version` | `LowCardinality(String)` |
| `extracted_at` | `DateTime` |

Engine: `MergeTree ORDER BY (chunk_id, relation_id)`. Append-only.

### `kg_entities` — canonical entities after resolution

| Column | Type |
|---|---|
| `entity_id` | `UUID` (`uuid5(NS, f"{canonical_name_normalized}::{entity_type}")`) |
| `entity_type` | `LowCardinality(String)` |
| `canonical_name` | `String` |
| `aliases` | `Array(String)` |
| `properties_merged` | `Map(String, String)` |
| `auth_ids` | `Array(String)` — union of source-chunk `auth_id`s across all contributing mentions. Gates row visibility via ANY-match. |
| `resolution_version` | `LowCardinality(String)` (bumped per full re-resolution) |
| `first_seen` | `DateTime` |
| `last_seen` | `DateTime` |

Engine: `ReplacingMergeTree(resolution_version) ORDER BY entity_id`. Read via
a view that applies `FINAL`.

### `kg_alias_map` — mention → canonical entity

| Column | Type |
|---|---|
| `mention_id` | `UUID` |
| `entity_id` | `UUID` |
| `auth_id` | `String` (inherited from the mention) |
| `resolution_method` | `Enum8('exact' = 1, 'embedding_cluster' = 2, 'llm_judged' = 3)` |
| `confidence` | `Float32` |
| `resolution_version` | `LowCardinality(String)` |

Engine: `ReplacingMergeTree(resolution_version) ORDER BY mention_id`.

### `kg_edges` — canonical edges, derived

| Column | Type |
|---|---|
| `edge_id` | `UUID` (`uuid5` of `source_entity_id::relation_type::target_entity_id`) |
| `source_entity_id` | `UUID` |
| `target_entity_id` | `UUID` |
| `relation_type` | `LowCardinality(String)` |
| `evidence_chunks` | `Array(String)` |
| `auth_ids` | `Array(String)` — union of source-chunk `auth_id`s across all contributing relations. Gates row visibility via ANY-match. |
| `support_count` | `UInt32` |
| `resolution_version` | `LowCardinality(String)` |

Engine: `ReplacingMergeTree(resolution_version) ORDER BY edge_id`. Re-derived
from `kg_relations_raw` + `kg_alias_map` after each resolution run.

### Row policies on KG tables

All five tables receive policies installed by iris's Authorization feature
(extend the create-database flow alongside the `rag_embeddings` policy).

**Per-row tables** (`kg_mentions_raw`, `kg_relations_raw`, `kg_alias_map`):

```sql
USING arrayExists(r -> has(
  dictGet('rag_docs.rag_acl_dict', 'allowed_roles', auth_id),
  r
), currentRoles())
```

Identical shape to the `rag_embeddings` policy.

**Aggregated tables** (`kg_entities`, `kg_edges`), ANY-match semantics:

```sql
USING arrayExists(a -> arrayExists(r -> has(
  dictGet('rag_docs.rag_acl_dict', 'allowed_roles', a),
  r
), currentRoles()), auth_ids)
```

Required grants: every tier role attached to the policies needs
`GRANT dictGet ON rag_docs.rag_acl_dict`. This is the same grant the
`rag_embeddings` policy already requires; install it once and all six
tables (embeddings + 5 KG tables) are covered.

## Extraction pipeline

Operator-owned, runs once per chunk during ingestion (same boundary as
`rag_acl`).

1. **Fetch chunk content** — the same text being embedded for
   `rag_embeddings`.
2. **Call the schema-guided LLM extractor** — fixed vocabulary, JSON output,
   small context window of neighboring chunks for disambiguation. Validate
   the response against a Pydantic schema; reject and retry on schema
   violations.
3. **Compute mention embeddings** — embed `f"{entity_type}: {name_surface} | {context_snippet}"`
   with the same model used for RAG, or a cheaper one (operator's choice).
4. **Compute deterministic IDs** — `mention_id = uuid5(NS, f"{chunk_id}::{span_start}::{span_end}")`
   so re-extraction of the same chunk produces identical mention IDs.
5. **Insert** into `kg_mentions_raw` and `kg_relations_raw`, **propagating
   the source chunk's `auth_id` onto every row**. Both append-only.

Extractor prompt shape (illustrative — operator owns the final version):

```
You are an entity-relation extractor. Output JSON of this shape:
{
  "entities": [
    {"local_id": <int>, "type": <EntityType>, "name": <str>,
     "aliases": [<str>...], "properties": {<str>: <str>}}
  ],
  "relations": [
    {"source": <local_id>, "target": <local_id>,
     "type": <RelationType>, "evidence": <verbatim quote>}
  ]
}

Constraints:
- `type` values must come from the supplied vocabulary.
- `evidence` must be a verbatim quote from the input text.
- If uncertain, omit rather than guess.
```

`local_id` is per-chunk; the loader translates it to a stable `mention_id`
before insert.

## Hybrid resolution pipeline

Batch job at operator-controlled cadence (e.g. nightly, or on-demand after a
large ingest). Each run bumps `resolution_version`. **The job must run with
service-tier privilege** (`query_as_service`, not a user session) so it can
read all mentions/relations across `auth_id`s to perform the aggregation.

### Stage 1 — deterministic normalization + exact-match merge

1. Normalize each mention name per entity type: lowercase, strip diacritics,
   collapse whitespace, strip common suffixes (`Inc.`, `Ltd.`, `Dr.`,
   honorifics) via per-type rules.
2. Group mentions by `(entity_type, normalized_name)`. Each group is a
   candidate canonical cluster.
3. For unambiguous groups, assign `entity_id` immediately and write
   `kg_alias_map` rows with `resolution_method = 'exact'`,
   `confidence = 1.0`, `auth_id` carried over from the mention.

Captures 60–80% of mentions cheaply in typical corpora.

### Stage 2 — embedding clustering, within entity_type

For mentions not assigned in Stage 1 (plus a sampled fraction of those that
were, to detect under-merging):

1. Within each `entity_type`, run agglomerative clustering on
   `mention_embedding` with a tight cosine threshold (start: 0.90).
2. If a cluster overlaps an existing Stage 1 canonical entity, merge into
   it; otherwise create a new canonical entity.
3. Write `kg_alias_map` with `resolution_method = 'embedding_cluster'` and
   `confidence = cluster_cohesion`.

In ClickHouse this is feasible with `cosineDistance` plus a Python clustering
driver; or run fully offline and load the result.

### Stage 3 — LLM pairwise judge on the ambiguous tail

For mention pairs whose embedding similarity falls in the uncertain band
(e.g. 0.75–0.90) and whose normalized names differ:

1. Block candidates by `(entity_type, prefix_of_normalized_name)` to bound
   the comparison set.
2. For each blocked pair, prompt:
   ```
   Are these the same entity?
   A: {name_a, context_a, properties_a}
   B: {name_b, context_b, properties_b}
   Answer JSON: {same: bool, confidence: float, reason: str}
   ```
3. Apply a confidence threshold and write `kg_alias_map` with
   `resolution_method = 'llm_judged'`.

Blocking from Stage 2 is what keeps Stage 3 tractable — target a few
thousand pairs per batch, not millions.

### Final step — derive `kg_entities` and `kg_edges`

1. Build `kg_entities` by aggregating per `entity_id`: merge `aliases`,
   merge `properties_merged`, pick the most frequent surface form as
   `canonical_name`, and **set `auth_ids = groupUniqArray(auth_id)` over
   the contributing mentions** (via `kg_alias_map` joined to
   `kg_mentions_raw`).
2. Derive `kg_edges` by joining `kg_relations_raw` to `kg_alias_map` on both
   source and target mentions, then aggregating to
   `(source_entity_id, relation_type, target_entity_id)` with
   `groupArray(chunk_id)` as `evidence_chunks`, `count()` as
   `support_count`, and **`groupUniqArray(auth_id)` as `auth_ids`**.

Both written with the new `resolution_version`. Old versions are kept for
A/B until pruned manually.

## Query path alongside vector RAG

Both paths run on the user's `DatabaseSession` (so `currentRoles()` is
correct for every row-policy evaluation along the path). **Every read in
the path traverses a row-policied table**, so the graph itself only
returns entities/edges the user is authorized to see.

**Vector path** (existing): top-K from `rag_embeddings`, row-policy
filtered.

**Graph path:**
1. Run a lightweight extractor on the question (same schema, simpler
   prompt) to get question entities and optional relation hints.
2. Match question entities to `kg_entities` by name-embedding similarity
   with an exact-alias fallback. The match query is row-policy filtered:
   the user only sees entities they have evidence for.
3. Traverse `kg_edges` 1–2 hops from matched entities, optionally filtering
   by relation type. Pure SQL JOINs; `kg_edges` is row-policy filtered.
4. From the resulting entity set, follow `kg_alias_map` →
   `kg_mentions_raw.chunk_id` to assemble candidate chunks. Both joined
   tables are row-policy filtered, so only `chunk_id`s the user can read
   survive.
5. Fetch those chunks from `rag_embeddings` — the same row policy applies
   one final time as defense in depth.

Merge the two paths' chunks, deduplicate by `(doc_id, chunk_id)`,
optionally rerank, then synthesize.

## What iris owns vs. what the operator owns

| Concern | Owner |
|---|---|
| Maintaining schema config (entity types, relation types, normalization rules) | Operator |
| Running the extraction LLM, computing mention embeddings, loading raw tables | Ingestion pipeline (out of scope for iris) |
| Propagating `auth_id` from `rag_embeddings` onto KG rows at ingest | Ingestion pipeline |
| Running the resolution batch job under service-tier privilege | Ingestion pipeline |
| Provisioning the 5 KG tables with the right engines | Iris (extend Authorization feature's create-database flow) |
| Attaching row policies to the 5 KG tables | Iris |
| Granting roles `dictGet` on `rag_acl_dict` (single grant, covers all 6 tables) | Iris |
| Issuing the graph-path queries on the user's session | A new feature module (alongside the RAG feature) |
| Issuing the vector-path queries (existing, row-policied) | Same feature module |

## Non-goals

- No streaming / incremental resolution — resolution is batch.
- No community detection / hierarchical summarization.
- No cross-language entity resolution beyond what the embedding model gives
  for free.
- No automatic schema drift detection — schema is operator-curated.
- **No column-level masking of `evidence_chunks` arrays.** A user reading
  an authorized `kg_edges` row sees all `chunk_id`s in `evidence_chunks`,
  including any whose underlying `rag_embeddings` row they can't read.
  The chunk content itself remains gated by the row policy at fetch; the
  synthesis stage masks unauthorized `chunk_id`s in the structural prompt
  block. Out-of-scope to enforce at the column level.

## Open questions

1. **Embedding model for mentions vs. chunks.** Same model = simpler;
   cheaper model for mentions is usually fine because the resolution task
   is short-string similarity. Pick after a small benchmark.
2. **Stage 2 cosine threshold and Stage 3 uncertainty band.** Need real
   data to tune. Start with 0.90 / 0.75–0.90 and adjust.
3. **`uuid5` namespace stability.** The namespace UUID must be fixed up
   front and never rotated, or every `entity_id` and `edge_id` will change
   across runs.
4. **Aggregated-table policy cost at scale.** `arrayExists × arrayExists`
   with a `dictGet` inside is O(|auth_ids| × |currentRoles|) per row. Fine
   for small arrays; measure if popular entities accumulate hundreds of
   `auth_ids` and consider materializing a per-role visibility flag if
   needed.
