# RAG knowledge-graph extraction and resolution — high-level spec

**Status:** design only.
**Date:** 2026-05-11.
**Companion to:** `2026-05-11-rag-row-policy-acl-design.md` (the chunk-level vector RAG spec).
**Co-companion:** `2026-05-11-rag-synthesis-prompt-design.md` (the synthesis prompt; this spec covers the KG-side inputs it consumes).

## Goal

Augment iris's vector RAG with a knowledge graph: extract typed entities and
relationships from each indexed chunk using an LLM with a fixed schema,
resolve the entity mentions into canonical nodes with a hybrid strategy,
resolve **cross-document coreference** to merge pronouns and definite
descriptions into the same canonical nodes, and expose the artefacts
(`kg_entities`, `kg_edges`) to the synthesis stage. All tables sit inside
the same authorization boundary as `rag_embeddings` — the row-dict-policy
substrate is extended to gate every read of mentions, relations, entities,
edges, and alias mappings.

## Scope

In scope:
1. Schema (entity types + relation types).
2. ClickHouse storage layout (5 tables, all row-policied).
3. Extraction pipeline (LLM-driven, per chunk).
4. Hybrid entity-resolution pipeline (deterministic → embedding cluster → LLM pairwise).
5. **Coreference resolution** (in-document during extraction; cross-document as a post-resolution pass).
6. How the KG plugs into query time alongside vector search.
7. **What the KG side produces for the synthesis stage** (structural block; the prompt itself is in the synthesis spec).

Out of scope: the synthesis prompt template (lives in the synthesis spec);
streaming / incremental resolution; cross-language entity resolution
beyond the embedding model's intrinsic ability; **community detection and
hierarchical summarization** (GraphRAG-style global-question routing) —
explicitly deferred (see "Deferred" below).

## Deferred to a later phase

**Community detection + per-community LLM summarization** ("GraphRAG-style"
structural index that pre-computes summaries of entity clusters at multiple
resolution levels) is intentionally not in v1. The motivation:

- For DFIR-style workloads, almost every analyst question is
  **entity-anchored**: "what does T1059.001 do?", "which past cases mention
  this hash?", "what malware does APT29 use?". These are handled well by
  the regular entity-match + 1–2-hop graph traversal.
- The questions communities help with are **global / aggregative** ("what
  are the dominant TTPs in our incident history?"). These are real but
  rare; they're strategic-review queries, not investigation-flow queries.
- The cost is real: Leiden + per-community LLM summaries, a second
  worker, a second access-grant story, a question-classifier in the
  synthesis path, a new table with its own row policy, and partition
  coordination. Worth it only when global-question demand is measured.

If global questions turn out to matter in practice, the addition is clean
because communities sit *on top of* `kg_edges` without changing it. Add a
`kg_communities` table, a community-detection job, and a
`COMMUNITY SUMMARIES` prompt block in a follow-on phase.

## Authorization stance

**All KG tables sit inside iris's auth boundary, using the same
`rag_acl_dict` substrate as `rag_embeddings`.** No structural metadata
about entities or edges leaks to a user who has no authorized evidence
for them.

Two flavors of row policy are needed:

1. **Per-row tables** (`kg_mentions_raw`, `kg_relations_raw`,
   `kg_alias_map`) carry a single `auth_id String` column, inherited from
   the source chunk. They use the same row policy expression as
   `rag_embeddings`.
2. **Aggregated tables** (`kg_entities`, `kg_edges`) carry an
   `auth_ids Array(String)` column — the union of `auth_id`s of all
   contributing mentions / relations. Their row policy uses ANY-match
   semantics.

**Visibility semantics:**

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
| `name_surface` | `String` | Verbatim surface form (or referring expression for coreference mentions). |
| `aliases` | `Array(String)` | Other surface forms the LLM emitted. |
| `mention_kind` | `Enum8('direct' = 1, 'coreference_in_doc' = 2, 'coreference_cross_doc' = 3)` | See Coreference resolution. |
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
| `resolution_method` | `Enum8('exact' = 1, 'embedding_cluster' = 2, 'llm_judged' = 3, 'coreference' = 4)` |
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
   small context window of neighboring chunks for disambiguation. The
   extractor emits both **direct** mentions and (if in-doc coreference
   is enabled) **in-document coreference** mentions in the same call.
   Validate against a Pydantic schema; reject and retry on schema
   violations.
3. **Compute mention embeddings** — embed `f"{entity_type}: {name_surface} | {context_snippet}"`
   with the same model used for RAG, or a cheaper one (operator's choice).
4. **Compute deterministic IDs** — `mention_id = uuid5(NS, f"{chunk_id}::{span_start}::{span_end}")`
   so re-extraction of the same chunk produces identical mention IDs.
5. **Insert** into `kg_mentions_raw` and `kg_relations_raw`, **propagating
   the source chunk's `auth_id` onto every row** and setting `mention_kind`
   per emission. Both append-only.

Extractor prompt shape (illustrative — operator owns the final version):

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
- For `kind = "coreference_in_doc"`, `refers_to_local_id` must point to a
  `direct` entity in this same response.
- If uncertain, omit rather than guess.
```

`local_id` is per-chunk; the loader translates it to a stable `mention_id`
before insert. Coreference emissions with a `refers_to_local_id` get a
`kg_alias_map` row pointing to the same canonical entity as the referent.

## Hybrid resolution pipeline

Batch job at operator-controlled cadence (e.g. nightly, or on-demand after a
large ingest). Each run bumps `resolution_version`.

**Worker access model.** The job runs under `query_as_user(worker_session, ...)`
where `worker_session` belongs to a regular iris user (e.g., `kg-resolver`),
NOT a tier admin. The worker's groups (e.g., `KG_RESOLVER_GRP`) must be
explicitly added to `rag_acl.allowed_roles` for every `auth_id` the operator
wants the worker to aggregate over. Row policies apply normally; the worker
sees exactly the auth_ids the operator deliberately granted. No service-tier
bypass.

Two common shapes:

1. **Single global-ish worker.** One worker user granted via every `rag_acl`
   row. Effectively "see everything" — but via explicit grants, auditable in
   `rag_acl` itself. Best when cross-auth_id entity correlation matters
   (e.g., LLM-extracted "T1059" in a customer chunk should resolve into the
   public STIX `AttackPattern` canonical).
2. **Per-tenant workers.** One worker per tenant + the public auth_ids the
   tenant references. Strict tenant isolation; no cross-tenant entity
   correlation. Multiple resolution runs (one per worker) into the same
   shared tables.

Pick based on whether canonical entities should span auth_id boundaries.
DFIR deployments typically want shape (1) so customer mentions of public
STIX entities resolve correctly.

### Stage 1 — deterministic normalization + exact-match merge

1. Normalize each direct mention name per entity type: lowercase, strip
   diacritics, collapse whitespace, strip common suffixes (`Inc.`,
   `Ltd.`, `Dr.`, honorifics) via per-type rules.
2. Group mentions by `(entity_type, normalized_name)`. Each group is a
   candidate canonical cluster.
3. For unambiguous groups, assign `entity_id` immediately and write
   `kg_alias_map` rows with `resolution_method = 'exact'`,
   `confidence = 1.0`, `auth_id` carried over from the mention.

Captures 60–80% of direct mentions cheaply in typical corpora.
Coreference mentions are not clustered — they're resolved separately
(see "Cross-document coreference" below).

### Stage 1.5 — merge into pre-existing canonicals

**Critical for the STIX bootstrap to work end-to-end.** Before creating
any *new* canonical from Stage 1's groups, look up whether an existing
`kg_entities` row already covers this canonical_name + entity_type:

1. For each Stage 1 group `(entity_type, normalized_name)`, query
   `kg_entities` for any row whose `entity_type` matches AND
   (`canonical_name` normalizes to the same value OR `normalized_name`
   appears in `aliases`).
2. If exactly one match: assign that pre-existing `entity_id` to every
   mention in the group. Write `kg_alias_map` rows with
   `resolution_method = 'exact'`, `confidence = 1.0`. Do NOT create a new
   canonical.
3. If multiple matches: defer to Stage 3 (LLM pairwise judge) to pick
   the right one.
4. If no match: proceed to create a new canonical in Stage 2 / 5 with
   `entity_id = uuid5(NS, f"{canonical_name_normalized}::{entity_type}")`.

This is what lets LLM-extracted mentions ("T1059", "Mimikatz", "APT29")
from later corpus ingests resolve into the STIX-sourced canonicals
(whose `entity_id` is a STIX-native UUID, not the `uuid5` form).
Without Stage 1.5, the resolver would silently fork — one entity per
ID-derivation scheme — and traversal queries would split.

### Stage 2 — embedding clustering, within entity_type

For direct mentions not assigned in Stage 1 (plus a sampled fraction of
those that were, to detect under-merging):

1. Within each `entity_type`, run agglomerative clustering on
   `mention_embedding` with a tight cosine threshold (start: 0.90).
2. For each cluster, **first check for overlap with any pre-existing
   `kg_entities` row** by computing the cluster centroid's nearest
   neighbours in `kg_entities` (within `entity_type`) — if the nearest
   pre-existing canonical exceeds the threshold, merge into it. Then
   fall back to Stage 1 / 1.5 canonicals; otherwise create a new
   canonical.
3. Write `kg_alias_map` with `resolution_method = 'embedding_cluster'` and
   `confidence = cluster_cohesion`.

In ClickHouse this is feasible with `cosineDistance` plus a Python
clustering driver; or run fully offline and load the result.

### Stage 3 — LLM pairwise judge on the ambiguous tail

For direct-mention pairs whose embedding similarity falls in the
uncertain band (e.g. 0.75–0.90) and whose normalized names differ:

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

Blocking from Stage 2 keeps Stage 3 tractable — target a few thousand
pairs per batch, not millions.

### Stage 4 — in-document coreference write-through

Coreference mentions emitted by the extractor with a `refers_to_local_id`
already carry the resolved target. The resolver writes the corresponding
`kg_alias_map` rows with `resolution_method = 'coreference'`,
`confidence` carried from the extractor's emission (typically lower than
direct mentions; operator tunes the cutoff).

### Stage 5 — derive `kg_entities` and `kg_edges`

1. Build `kg_entities` by aggregating per `entity_id`: merge `aliases`,
   merge `properties_merged`, pick the most frequent surface form as
   `canonical_name`, and **set `auth_ids = groupUniqArray(auth_id)` over
   the contributing mentions** (via `kg_alias_map` joined to
   `kg_mentions_raw`). Coreference mentions count toward `auth_ids` but
   not toward `canonical_name` voting.
2. Derive `kg_edges` by joining `kg_relations_raw` to `kg_alias_map` on
   both source and target mentions, then aggregating to
   `(source_entity_id, relation_type, target_entity_id)` with
   `groupArray(chunk_id)` as `evidence_chunks`, `count()` as
   `support_count`, and **`groupUniqArray(auth_id)` as `auth_ids`**.

Both written with the new `resolution_version`. Old versions are kept for
A/B until pruned manually.

## Cross-document coreference (post-resolution pass)

Resolves referring expressions — pronouns ("it", "they"), definite
descriptions ("the company", "the attacker"), demonstratives ("this
technique") — that the in-document extractor couldn't link to an entity
because the referent lives in another chunk or another document.

**Triggering signal.** The extractor flags chunks containing unresolved
referring expressions (a small JSON field alongside the entity/relation
output: `unresolved_references: [{span: str, hint: str}]`).

**Pass workflow** (operator-owned, runs after Stage 5):

1. For each chunk with unresolved references:
   a. Assemble candidate entities — the chunk's own direct-mention
      entities, plus the parent document's top-K most-mentioned entities,
      plus the document's neighbor chunks' top-K entities.
   b. Prompt the LLM:
      ```
      In this chunk, does any unresolved referring expression refer to
      one of these candidate entities?
      Chunk: <text>
      Unresolved spans: <list>
      Candidates: [{entity_id, canonical_name, type, summary_snippet}, ...]
      Answer JSON: [{span, entity_id|null, confidence}]
      ```
   c. For each non-null answer above the confidence cutoff:
      - Synthesize a `kg_mentions_raw` row. Because synthetic mentions
        don't have a span in the chunk's tokens, `mention_id` is derived
        as `uuid5(NS, f"{chunk_id}::coref::{entity_id}::{resolution_version}")`
        (not the `chunk_id::span_start::span_end` form). The
        `resolution_version` term keeps the id stable within a run and
        re-derivable on the next run.
      - Set `mention_kind = 'coreference_cross_doc'`, `auth_id` from the
        chunk, `name_surface` = the referring expression text,
        `extractor_version = "coref-pass-<version>"`.
      - Write a `kg_alias_map` row pointing at the resolved `entity_id`
        with `resolution_method = 'coreference'`.

**Cost.** One additional LLM call per chunk that flags unresolved
references. Off by default; operator enables when corpus quality
demands it (e.g., narrative incident reports with heavy anaphora).

**Re-derivation.** After the coreference pass writes new mentions and
alias-map rows, re-run Stage 5's `kg_entities` / `kg_edges` derivation so
the new coreference mentions contribute to `auth_ids` and edge
`support_count`.

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
3. Traverse `kg_edges` 1–2 hops from matched entities, optionally
   filtering by relation type. Pure SQL JOINs; `kg_edges` is row-policy
   filtered.
4. From the resulting entity set, follow `kg_alias_map` →
   `kg_mentions_raw.chunk_id` to assemble candidate chunks. Both joined
   tables are row-policy filtered, so only `chunk_id`s the user can read
   survive.
5. Fetch those chunks from `rag_embeddings` — the same row policy applies
   one final time as defense in depth.

Merge vector + graph chunks, deduplicate by `(doc_id, chunk_id)`,
optionally rerank, then synthesize.

## Synthesis integration

The synthesis prompt template is defined in
`2026-05-11-rag-synthesis-prompt-design.md`. The KG side contributes two
inputs to the pre-synthesis pipeline:

1. **Question-entity matches and candidate chunks** — from the graph-path
   query (steps 2–4 above), row-policy filtered.
2. **Structural block** — a set of `kg_edges` rows used to build the
   `STRUCTURAL CONTEXT` block of the synthesis prompt. The KG side:
   - Selects `kg_edges` whose endpoints include a question-matched
     entity.
   - Orders by `relevance_to_question_entities × support_count`.
   - Caps to `structural_edges_cap` (default 50, per the synthesis spec).
   - Returns rendering inputs `{source_entity_name, source_entity_type,
     relation_type, target_entity_name, target_entity_type, evidence_chunk_ids}`.
   - The synthesis-stage filter further restricts `evidence_chunk_ids`
     to the user's authorized chunk set before they land in the prompt.

## What iris owns vs. what the operator owns

| Concern | Owner |
|---|---|
| Maintaining schema config (entity types, relation types, normalization rules) | Operator |
| Running the extraction LLM (including in-doc coreference emission), computing mention embeddings, loading raw tables | Ingestion pipeline (out of scope for iris) |
| Propagating `auth_id` from `rag_embeddings` onto KG rows at ingest | Ingestion pipeline |
| Provisioning the worker user (`kg-resolver` or per-tenant equivalents) and granting its groups in `rag_acl.allowed_roles` | Operator |
| Running the resolution batch job as `query_as_user(worker_session, ...)` | Ingestion pipeline |
| Running the cross-document coreference pass under the same worker session | Ingestion pipeline |
| Provisioning the 5 KG tables with the right engines | Iris (extend Authorization feature's create-database flow) |
| Attaching row policies to the 5 KG tables | Iris |
| Granting roles `dictGet` on `rag_acl_dict` (single grant, covers all 6 tables) | Iris |
| Graph-path execution (entity match, edge traversal, candidate chunks) | A new feature module (alongside the RAG feature) |
| Producing the synthesis-stage inputs (structural block) | Same feature module |
| Issuing the vector-path queries (existing, row-policied) | Same feature module |

## Non-goals

- No streaming / incremental resolution — resolution and coreference
  are batch.
- **No community detection / hierarchical summarization in v1.** See
  "Deferred to a later phase" above.
- No automatic schema drift detection — schema is operator-curated.
- No cross-language entity resolution beyond what the embedding model
  gives for free.
- **No column-level masking of `evidence_chunks` arrays.** A user
  reading an authorized `kg_edges` row sees all `chunk_id`s in
  `evidence_chunks`, including any whose underlying `rag_embeddings` row
  they can't read. The chunk content itself remains gated by the row
  policy at fetch; the synthesis stage masks unauthorized `chunk_id`s in
  the structural prompt block. Out-of-scope to enforce at the column
  level.
- No reified coreference chains (we link each referring expression to
  one canonical entity; we do not store the chain of intermediate
  mentions).

## Open questions

1. **Embedding model for mentions vs. chunks.** Same model = simpler;
   cheaper model for short-string tasks usually fine. Pick after
   benchmark.
2. **Stage 2 / Stage 3 thresholds.** Need real data to tune. Start with
   0.90 / 0.75–0.90 and adjust.
3. **`uuid5` namespace stability.** Must be fixed up front and never
   rotated, or every `entity_id` and `edge_id` will change across runs.
4. **Aggregated-table policy cost at scale.** `arrayExists × arrayExists`
   with a `dictGet` inside is O(|auth_ids| × |currentRoles|) per row.
   Fine for small arrays; measure if popular entities accumulate
   hundreds of `auth_ids`.
5. **Coreference confidence cutoff.** Too low → wrong merges pollute the
   graph; too high → coreference adds little. Default 0.75; operator
   tunes after measuring real false-merge rate.
6. **Worker scope shape.** Single global-ish worker vs. per-tenant
   workers (see Worker access model). DFIR deployments with
   public-STIX-shared canonicals favor the single worker; strict
   multi-tenancy favors per-tenant. Operator decision encoded in
   `rag_acl` grants — no code change required to switch.
7. **When to revisit communities.** Add a metric — count of questions
   classified as "global" or that fail to find a satisfying answer via
   the local path. If that count is non-trivial after a few months,
   re-evaluate the community-detection addition.
