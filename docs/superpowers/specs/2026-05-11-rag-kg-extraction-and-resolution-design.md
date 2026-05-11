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
descriptions into the same canonical nodes, build a **hierarchical
community index** over the resulting graph for "global" questions, and
expose the artefacts (`kg_entities`, `kg_edges`, `kg_communities`) to the
synthesis stage. All tables sit inside the same authorization boundary as
`rag_embeddings` — the row-dict-policy substrate is extended to gate every
read of mentions, relations, entities, edges, alias mappings, and
community summaries.

## Scope

In scope:
1. Schema (entity types + relation types).
2. ClickHouse storage layout (6 tables, all row-policied).
3. Extraction pipeline (LLM-driven, per chunk).
4. Hybrid entity-resolution pipeline (deterministic → embedding cluster → LLM pairwise).
5. **Coreference resolution** (in-document during extraction; cross-document as a post-resolution pass).
6. **Community detection + hierarchical summarization** (Leiden + per-community LLM summaries, partitioned by `auth_id` for authorization correctness).
7. How the KG plugs into query time alongside vector search.
8. **What the KG side produces for the synthesis stage** (structural block, community summaries; the prompt itself is in the synthesis spec).

Out of scope: the synthesis prompt template (lives in the synthesis spec);
streaming / incremental resolution; cross-language entity resolution
beyond the embedding model's intrinsic ability.

## Authorization stance

**All KG tables sit inside iris's auth boundary, using the same
`rag_acl_dict` substrate as `rag_embeddings`.** No structural metadata
about entities, edges, or communities leaks to a user who has no
authorized evidence for them.

Two flavors of row policy are needed:

1. **Per-row tables** (`kg_mentions_raw`, `kg_relations_raw`,
   `kg_alias_map`) carry a single `auth_id String` column, inherited from
   the source chunk. They use the same row policy expression as
   `rag_embeddings`.
2. **Aggregated tables** (`kg_entities`, `kg_edges`, `kg_communities`)
   carry an `auth_ids Array(String)` column — the union of `auth_id`s of
   all contributing mentions / relations / member entities. Their row
   policy uses ANY-match semantics.

**Visibility semantics:**

- A user can see an entity iff they can read at least one chunk that
  mentions it.
- A user can see an edge iff they can read at least one chunk that
  evidences it.
- A user can see a community summary iff they can read at least one
  chunk evidencing one of the community's member entities.
- Entity/edge/community names are never exposed to a user who has no
  authorized evidence for them.
- A user reading an authorized `kg_edges` row sees all `chunk_id`s in
  its `evidence_chunks` array — including any whose underlying
  `rag_embeddings` row they cannot read. CH row policies are row-level,
  not column-level; the synthesis stage's structural-block filter masks
  the unauthorized `chunk_id`s before they reach the LLM prompt. That
  masking is the only remaining defense for `evidence_chunks` contents,
  and the chunk content itself stays gated by the row policy at fetch.
- **Community summaries are partitioned by `auth_id`** (see Community
  Detection below) so summary text never blends authorization scopes.

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

Six tables, colocated with embeddings in the RAG database (e.g. `rag_docs`).

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

### `kg_communities` — hierarchical community index over the graph

| Column | Type | Notes |
|---|---|---|
| `community_id` | `UUID` | `uuid5(NS, f"{auth_id_partition}::{level}::{sorted_entity_ids}")` — stable across re-runs with identical membership and partition. |
| `auth_id_partition` | `String` | The `auth_id` partition this community lives in (see "Community detection" below). Single value, not an array, because partitioning is strict. |
| `level` | `UInt8` | 0 = finest resolution; ascending = coarser. Default depth 3. |
| `entity_ids` | `Array(UUID)` | Member entities (all share `auth_id_partition` in their `auth_ids`). |
| `summary` | `String` | LLM-generated, 3–5 sentences. |
| `summary_embedding` | `Array(Float32)` | For routing global questions to relevant communities. |
| `auth_ids` | `Array(String)` | Singleton: `[auth_id_partition]`. Row visibility via ANY-match (degenerate to exact match). |
| `parent_community_id` | `Nullable(UUID)` | Link to the coarser-level community this one belongs to. |
| `support_count` | `UInt32` | Sum of member-edge `support_count`s. |
| `resolution_version` | `LowCardinality(String)` | |
| `summarized_at` | `DateTime` | |

Engine: `ReplacingMergeTree(resolution_version) ORDER BY (auth_id_partition, level, community_id)`.

### Row policies on KG tables

All six tables receive policies installed by iris's Authorization feature
(extend the create-database flow alongside the `rag_embeddings` policy).

**Per-row tables** (`kg_mentions_raw`, `kg_relations_raw`, `kg_alias_map`):

```sql
USING arrayExists(r -> has(
  dictGet('rag_docs.rag_acl_dict', 'allowed_roles', auth_id),
  r
), currentRoles())
```

Identical shape to the `rag_embeddings` policy.

**Aggregated tables** (`kg_entities`, `kg_edges`, `kg_communities`), ANY-match semantics:

```sql
USING arrayExists(a -> arrayExists(r -> has(
  dictGet('rag_docs.rag_acl_dict', 'allowed_roles', a),
  r
), currentRoles()), auth_ids)
```

Required grants: every tier role attached to the policies needs
`GRANT dictGet ON rag_docs.rag_acl_dict`. This is the same grant the
`rag_embeddings` policy already requires; install it once and all seven
tables (embeddings + 6 KG tables) are covered.

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

**Re-derivation.** After Stage 6 (below), re-run Stage 5's
`kg_entities`/`kg_edges` derivation so the new coreference mentions
contribute to `auth_ids` and edge `support_count`.

## Community detection + hierarchical summarization

Builds the structural index that makes "global" questions tractable
(e.g., "what are the dominant themes in our incident history?"). Runs
after entity resolution + coreference + edge derivation.

**Worker access model.** Same as the resolution job — runs under
`query_as_user(worker_session, ...)` with the worker's groups granted in
`rag_acl` for every auth_id it should index. The community-detection
worker may be the same user as the resolution worker or a separate one
(e.g., `kg-communities`) with a smaller scope.

### Partitioning rule

**Detection runs per `auth_id` partition, not over the worker's full
visible graph.** For each distinct `auth_id` the worker can see, build
a separate sub-graph from `kg_edges` whose `auth_ids` contains that
partition value, limited to that partition's slice. Communities are
computed and summarized within the partition only. Each `kg_communities`
row's `auth_id_partition` records which partition it came from.

Why: a community whose member entities span multiple `auth_id`s would
inherit a union `auth_ids` that's correct at the row-policy level but
the summary text could still describe entities a user can see existence
of without being able to read evidence for them. Partitioning eliminates
that ambiguity at the cost of duplicated structural work.

Operator may coarsen the partition (e.g., group all `tlp:*` into one
"public" partition) via config when the strict per-`auth_id` partitioning
is too expensive.

### Detection algorithm

1. **Graph projection.** Weighted undirected graph: nodes =
   `kg_entities` in partition, edges = `kg_edges` in partition, weight =
   `support_count`.
2. **Leiden community detection** (via `graspologic` or `igraph-python`),
   run at multiple resolution parameters → a hierarchy of levels (level
   0 finest, level N coarsest). Default depth: 3 levels.
3. **Assign `community_id`** = `uuid5(NS, f"{auth_id_partition}::{level}::{sorted_entity_ids}")` — stable across re-runs with identical membership.
4. **Link parent communities** at each level via `parent_community_id`.

### Per-community summarization

For each community at each level:

1. Gather member entities (names + types + most salient properties) and
   a small sample of supporting edges (with their `evidence_chunks`).
2. Prompt the LLM:
   ```
   Summarize this cluster of entities and the relationships between them
   in 3–5 sentences. Cite entities by canonical name. Do not invent facts
   not supported by the listed evidence.

   Entities: [{name, type, top_aliases, top_properties}, ...]
   Sample relations: [{source, type, target, support_count, evidence_chunk_sample}, ...]
   ```
3. Write a `kg_communities` row. Compute `summary_embedding` for global
   question routing.
4. Cache by `hash(sorted_entity_ids || sorted_edge_ids)`; only
   re-summarize when membership or edge support actually changed.

### Budgeting and caps

The community job is **expensive** — it makes O(communities × LLM-call)
calls. To keep the cost bounded:

- **Minimum community size.** Skip communities with fewer than
  `min_members = 5` member entities (singletons and tiny pairs aren't
  useful summary targets and dominate the long tail).
- **Per-partition hard cap.** `max_communities_per_partition = 500` per
  level. If Leiden produces more, drop the smallest by `support_count`.
  Tune after measurement.
- **Resummarization gate.** Cache by
  `hash(sorted_entity_ids || sorted_supporting_edge_ids || resolution_version)`;
  re-summarize only when membership or supporting evidence actually
  changed. Pure `resolution_version` bumps that don't move membership
  reuse cached summaries.

### Refresh cadence

Run less often than the entity resolution job (e.g., weekly vs. nightly).
Operator-tunable.

## Query path alongside vector RAG

Both paths run on the user's `DatabaseSession` (so `currentRoles()` is
correct for every row-policy evaluation along the path). **Every read in
the path traverses a row-policied table**, so the graph itself only
returns entities/edges/communities the user is authorized to see.

**Vector path** (existing): top-K from `rag_embeddings`, row-policy
filtered.

**Graph path:**
1. Run a lightweight extractor on the question (same schema, simpler
   prompt) to get question entities and optional relation hints.
2. Classify the question as **local** (asks about specific entities) vs.
   **global** (asks about the corpus / a broad slice). Heuristic: presence
   of named entities + relational phrasing → local; aggregative wording
   ("summarize", "what are the main", "across all") → global.
3. **Local path:**
   a. Match question entities to `kg_entities` by name-embedding
      similarity with an exact-alias fallback.
   b. Traverse `kg_edges` 1–2 hops from matched entities, optionally
      filtering by relation type.
   c. From the resulting entity set, follow `kg_alias_map` →
      `kg_mentions_raw.chunk_id` to assemble candidate chunks.
   d. Fetch those chunks from `rag_embeddings` — final row-policy
      filter as defense in depth.
4. **Global path:**
   a. Embed the question with the same embedding model used for
      `summary_embedding`.
   b. Top-K nearest community summaries via `cosineDistance` on
      `kg_communities.summary_embedding`, row-policy filtered. Pull
      from coarser levels first (typically level N or N-1) — they're
      the "themes".
   c. Optionally drill down: for the top-K coarse communities, also
      fetch their level-0 children's summaries for detail.
   d. Use community member entities to seed a graph-path traversal
      (step 3c onwards) for evidence chunks.

Merge vector + graph chunks, deduplicate by `(doc_id, chunk_id)`,
optionally rerank, then synthesize.

## Synthesis integration

The synthesis prompt template is defined in
`2026-05-11-rag-synthesis-prompt-design.md`. The KG side contributes
three inputs to the pre-synthesis pipeline:

1. **Question-entity matches and candidate chunks** — from the
   local-path query (steps 3a–3c above), row-policy filtered.
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
3. **Community summaries** (global questions only) — for questions the
   graph path classified as global, the KG side surfaces a `COMMUNITY
   SUMMARIES` block: the top-K `kg_communities.summary` strings by
   `summary_embedding` similarity to the question, ordered by level
   (coarsest first), all row-policy filtered. The synthesis spec
   describes how this block plugs into the prompt; this spec is the
   source of the data.

## What iris owns vs. what the operator owns

| Concern | Owner |
|---|---|
| Maintaining schema config (entity types, relation types, normalization rules) | Operator |
| Running the extraction LLM (including in-doc coreference emission), computing mention embeddings, loading raw tables | Ingestion pipeline (out of scope for iris) |
| Propagating `auth_id` from `rag_embeddings` onto KG rows at ingest | Ingestion pipeline |
| Provisioning the worker user (`kg-resolver` or per-tenant equivalents) and granting its groups in `rag_acl.allowed_roles` | Operator |
| Running the resolution batch job as `query_as_user(worker_session, ...)` | Ingestion pipeline |
| Running the cross-document coreference pass under the same worker session | Ingestion pipeline |
| Running the community detection + summarization job under the same (or a separate, smaller-scope) worker session | Ingestion pipeline |
| Provisioning the 6 KG tables with the right engines | Iris (extend Authorization feature's create-database flow) |
| Attaching row policies to the 6 KG tables | Iris |
| Granting roles `dictGet` on `rag_acl_dict` (single grant, covers all 7 tables) | Iris |
| Question classification (local vs. global) and graph-path execution | A new feature module (alongside the RAG feature) |
| Producing the synthesis-stage inputs (structural block, community summaries) | Same feature module |
| Issuing the vector-path queries (existing, row-policied) | Same feature module |

## Non-goals

- No streaming / incremental resolution — resolution, coreference, and
  community detection are batch.
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
- **No cross-partition community detection.** Communities never span
  `auth_id` partitions by design (see Partitioning rule).
- No reified coreference chains (we link each referring expression to
  one canonical entity; we do not store the chain of intermediate
  mentions).

## Open questions

1. **Embedding model for mentions vs. chunks vs. community summaries.**
   Same model = simpler; cheaper model for short-string tasks usually
   fine. Pick after benchmark.
2. **Stage 2 / Stage 3 thresholds.** Need real data to tune. Start with
   0.90 / 0.75–0.90 and adjust.
3. **`uuid5` namespace stability.** Must be fixed up front and never
   rotated, or every `entity_id`, `edge_id`, and `community_id` will
   change across runs.
4. **Aggregated-table policy cost at scale.** `arrayExists × arrayExists`
   with a `dictGet` inside is O(|auth_ids| × |currentRoles|) per row.
   Fine for small arrays; measure if popular entities accumulate
   hundreds of `auth_ids`.
5. **Coreference confidence cutoff.** Too low → wrong merges pollute the
   graph; too high → coreference adds little. Default 0.75; operator
   tunes after measuring real false-merge rate.
6. **Community-detection partition coarsening.** Strict per-`auth_id`
   partitioning is most secure but most expensive. Operator may want to
   group, e.g., all `tlp:white` + `tlp:green` into one "public"
   partition. Spec the grouping config but defer the heuristic.
7. **Question local-vs-global classification.** Heuristic v1; an LLM
   classifier or learned router is the obvious upgrade once usage data
   exists.
8. **Community summary refresh trigger.** v1 re-summarizes when
   membership or edge support changes. A more aggressive cache (e.g.,
   "only resummarize if support changed by ≥10%") is worth measuring.
9. **Worker scope shape.** Single global-ish worker vs. per-tenant
   workers (see Worker access model). DFIR deployments with
   public-STIX-shared canonicals favor the single worker; strict
   multi-tenancy favors per-tenant. Operator decision encoded in
   `rag_acl` grants — no code change required to switch.
