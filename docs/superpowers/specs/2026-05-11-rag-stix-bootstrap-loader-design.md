# STIX bundle bootstrap loader for the RAG KG — high-level spec

**Status:** design only.
**Date:** 2026-05-11.
**Companions:**
- `2026-05-11-rag-row-policy-acl-design.md` (chunk-level row-policy ACL).
- `2026-05-11-rag-kg-extraction-and-resolution-design.md` (KG storage + extraction + resolution).
- `2026-05-11-rag-synthesis-prompt-design.md` (synthesis-stage prompt).

## Goal

Bootstrap iris's RAG knowledge graph with pre-structured threat intelligence
shipped as STIX 2.1 bundles (MITRE ATT&CK / MITRE-CTI first; TAXII feeds,
MISP exports, and vendor STIX later). The loader writes directly into the
KG tables and the chunk store **without going through the LLM extractor** —
STIX content is already structured, so extraction is pure waste. This gives
iris a working KG on day one (thousands of techniques, threat actors,
malware families with their relations), leaving the LLM extractor to handle
only unstructured corpus content (incident reports, blog posts, PDFs).

## Scope

In scope:
1. STIX SDO / SRO → iris schema (entity/relation type) mapping.
2. Where STIX content lands across `rag_embeddings` and the 5 KG tables
   (mentions, relations, entities, alias_map, edges).
3. `auth_id` is supplied externally by the operator (same model as the
   ingestion-pipeline spec) — the loader does not derive auth_id from
   STIX TLP markings.
4. TLP markings are parsed from `object_marking_refs` and stored in
   `rag_embeddings.tlp` as **informational metadata** for analyst
   awareness; they do not influence authorization.
5. ID-assignment rules (use STIX-native UUIDs for entities).
6. Refresh / idempotency strategy.
7. Loader workflow and operator interface.

Out of scope:
- TAXII network client implementation (use `stix2` / `taxii2-client` or a
  static bundle dump).
- Vendor-specific STIX dialect normalization beyond MITRE-CTI's `x-mitre-*`
  extensions.
- Live STIX subscription (loader is a scheduled batch).
- LLM re-extraction over STIX-sourced descriptions (skipped by design).
- Two-way sync — iris-extracted entities are never pushed back out as STIX.

## STIX → schema mapping

### Entity-type mapping

| STIX SDO type | iris `entity_type` |
|---|---|
| `threat-actor` | `ThreatActor` |
| `intrusion-set` | `IntrusionSet` |
| `malware` | `Malware` |
| `tool` | `Tool` |
| `attack-pattern` | `AttackPattern` |
| `vulnerability` | `Vulnerability` |
| `indicator` | `Indicator` |
| `infrastructure` | `Infrastructure` |
| `identity` | `Identity` |
| `location` | `Location` |
| `course-of-action` | `CourseOfAction` |
| `campaign` | `Campaign` |
| `x-mitre-tactic` (MITRE extension) | `Tactic` |
| `x-mitre-data-source` (MITRE extension) | `DataSource` |
| `x-mitre-matrix` (MITRE extension) | *skipped* — purely organizational |

Kill-chain phases referenced inside STIX SDOs (`kill_chain_phases`) become
standalone `KillChainPhase` entities, linked to the referencing SDO via
`PART_OF_KILLCHAIN_STAGE` relations. The phase name (e.g.
`initial-access`) is the canonical name; the kill-chain name (e.g.
`mitre-attack`) lives in `properties_merged`.

These entity types extend the starter schema from the KG extraction spec —
operators using STIX must update the schema config to add them.

### Relation-type mapping

STIX SROs (`relationship` objects) carry a `relationship_type` string.
Canonical mapping:

| STIX `relationship_type` | iris `relation_type` |
|---|---|
| `uses` | `USES` |
| `attributed-to` | `ATTRIBUTED_TO` |
| `targets` | `TARGETS` |
| `indicates` | `INDICATES` |
| `mitigates` | `MITIGATES` |
| `derived-from` | `DERIVED_FROM` |
| `related-to` | `RELATED_TO` |
| `subtechnique-of` (MITRE) | `PART_OF` |
| `revoked-by` | *not stored as edge — treated as a soft-delete signal* |

Unmapped relationship types fall back to `RELATED_TO`. Since
`kg_relations_raw` has no dedicated column for the original string, the
loader prefixes the `evidence` field with a structured marker:
`f"[stix_relationship_type={original}] {sro_description_if_present}"`.
This keeps the original visible without schema changes. Falls are also
logged so the operator can extend the mapping table directly.

### Property preservation

Critical STIX properties land in `properties_merged` on the entity:

- `mitre_attack_id` (e.g., `T1059.001`) — from
  `external_references[*].external_id` where `source_name == "mitre-attack"`.
- `cve_id` — from CVE external references on `vulnerability` SDOs.
- `capec_id`, `cwe_id` — from corresponding external references.
- `tlp` — parsed from `object_marking_refs` (TLP 1.0 `definition_type =
  "tlp"` or TLP 2.0 extension form). Stored on the `rag_embeddings.tlp`
  column for the SDO's description chunk as informational metadata
  ONLY. Does not affect `auth_id` or row-policy evaluation.
- `kill_chain_phases` — preserved as a denormalized list even though the
  graph also models them as separate entities (cheaper lookup for filters).
- `stix_id` — the original STIX object id, for traceability and back-refs.
- `stix_revoked` — `bool` (default `false`); flips to `true` on refresh
  when a newer bundle marks the object revoked.
- `stix_source` and `stix_source_version` — which bundle produced this
  entity (e.g., `mitre-cti`, `15.1`).

## ID-assignment policy

STIX-sourced entities use the **STIX-native UUID** (parsed out of the
`<type>--<uuid>` form) as their `entity_id`. This bypasses the
`uuid5(NS, canonical_name||entity_type)` scheme used for LLM-extracted
entities.

Rationale:
- STIX IDs are globally unique and stable across bundle releases.
- Refresh becomes idempotent (same STIX object → same `entity_id` forever).
- Future LLM-extracted mentions (e.g., "T1059" in a blog post) get
  resolved into the STIX-sourced canonical node via the resolution
  pipeline's Stage 2/3, since the embedding match will find it.

**Edge IDs** continue to use `uuid5(NS, source_entity_id || relation_type || target_entity_id)`
— STIX SROs have their own UUIDs but those change when MITRE re-issues
the same relationship; we don't want edge identity to flap.

The two `entity_id` schemes (STIX-native for bootstrap, `uuid5` for
LLM-extracted) coexist in the same `kg_entities` table without conflict —
both are UUIDs.

## Where STIX content lands

For each STIX SDO (entity-like object):

**1. One row in `rag_embeddings`** — content is the SDO's `description`
prefixed with a normalized header (`name`, aliases, MITRE ID, kill-chain
phases). This makes STIX descriptions retrievable as RAG chunks.

| Column | Value |
|---|---|
| `doc_id` | `f"stix:{stix_source}"` (e.g. `"stix:mitre-cti"`) |
| `chunk_id` | `f"stix:{stix_id}:description"` |
| `auth_id` | **supplied to the loader by the operator** (per-bundle CLI arg or per-object manifest; same external-classification model as the ingestion pipeline) |
| `tlp` | parsed from the SDO's `object_marking_refs` if present, else `'clear'` (the column default). Informational only. |
| `embedding` | computed by the loader |
| `content` | `<header>\n\n<description>` |

**2. One row in `kg_mentions_raw`** — synthetic mention pointing at the
chunk above:

| Column | Value |
|---|---|
| `mention_id` | `uuid5(NS, f"{chunk_id}::stix")` |
| `chunk_id` | matches the `rag_embeddings` row |
| `doc_id` | matches the `rag_embeddings` row |
| `auth_id` | matches the chunk |
| `entity_type` | per the mapping table |
| `name_surface` | SDO's `name` |
| `aliases` | SDO's `aliases` list |
| `mention_kind` | `'direct'` (always — STIX SDOs are direct mentions of named entities) |
| `properties` | the property set above |
| `mention_embedding` | embedding of `f"{entity_type}: {name} | {description[:200]}"` |
| `extractor_version` | `"stix-bootstrap-<version>"` |
| `prompt_version` | `"n/a"` |

**3. One row in `kg_entities`** — canonical entity:

| Column | Value |
|---|---|
| `entity_id` | the STIX-native UUID |
| `entity_type` | per the mapping table |
| `canonical_name` | SDO's `name` |
| `aliases` | SDO's `aliases` |
| `properties_merged` | the property set above |
| `auth_ids` | `[auth_id]` (singleton; resolution may extend later) |
| `resolution_version` | loader's stamp (e.g., `"stix-bootstrap-2026-05-11"`) |

**4. One row in `kg_alias_map`** — pointing the synthetic mention at the
canonical entity:

| Column | Value |
|---|---|
| `mention_id` | from step 2 |
| `entity_id` | from step 3 |
| `auth_id` | same as the mention |
| `resolution_method` | `'exact'` |
| `confidence` | `1.0` |
| `resolution_version` | same as the entity |

For each STIX SRO (relationship):

**5. One row in `kg_relations_raw`** — referencing the synthetic source
and target mentions (looked up by their STIX SDO ids):

| Column | Value |
|---|---|
| `relation_id` | `uuid5(NS, f"{stix_relationship_id}")` |
| `chunk_id` | `f"stix:{stix_relationship_id}"` (logical — see note below) |
| `auth_id` | the same operator-supplied `auth_id` used for SDOs in the bundle (loader does not infer per-relation auth) |
| `source_mention_id` | the synthetic mention of the SRO's source SDO |
| `target_mention_id` | the synthetic mention of the SRO's target SDO |
| `relation_type` | per the relation mapping |
| `evidence` | the SRO's `description` if present; prefixed with `[stix_relationship_type=<orig>]` when the type fell back to `RELATED_TO` |

**Note on `chunk_id` for SRO-derived relation rows.** This `chunk_id`
has no corresponding `rag_embeddings` row. The downstream consequence:
when Stage 5 of the resolution job derives `kg_edges.evidence_chunks` by
`groupArray(chunk_id)` over the relations, STIX-derived edges will carry
`stix:<sro_id>` entries that don't fetch. The synthesis stage filters
these out when computing `authorized_chunk_ids` (the chunks the user can
actually read from `rag_embeddings`). STIX-derived edges therefore
appear in the structural block only when they share evidence with a
fetchable chunk (e.g., an SDO description chunk that also mentions both
endpoints) — otherwise the edge surfaces via the entity-traversal path
and the model grounds its claim in the source/target SDOs' description
chunks, which **are** fetchable.

**6. `kg_edges`** — the loader does **not** insert directly. The next
resolution-job run picks the new relations up via the standard derivation
path (`kg_relations_raw + kg_alias_map → kg_edges`). This preserves the
single `kg_edges`-derivation invariant from the KG spec.

**Operator step:** run the resolution job after each bootstrap load so the
edges materialize. Until that runs, traversal still works on
`kg_relations_raw` / `kg_alias_map` but is slower.

## Authorization (`auth_id`)

The loader does **not** interpret STIX TLP markings as authorization
input. `auth_id` for every row written by the loader is supplied
externally by the operator, exactly the same model as the ingestion
starter-stack spec uses for ordinary documents.

Two supported delivery mechanisms (loader picks based on CLI flags):

1. **Per-bundle `auth_id`** — `--auth-id customer:acme` on the CLI.
   Every SDO and SRO in the bundle gets the same `auth_id`. Simplest;
   correct when the whole bundle has one classification.
2. **Per-object manifest** — `--auth-id-manifest <path.json>` providing
   `{stix_id: auth_id}` overrides. Falls back to the per-bundle default
   for unlisted objects. Use this when a single bundle mixes
   classifications (rare).

Operator pre-conditions (same as the ingestion-pipeline spec):

- The supplied `auth_id` must already exist in `rag_acl` with a sensible
  `allowed_roles` array. The loader **validates** every distinct
  `auth_id` against `rag_acl` before any insert; missing rows cause the
  bundle to be rejected with a clear error.
- The loader **does not** auto-create `rag_acl` rows.

The TLP marker inside each STIX object is parsed and stored in
`rag_embeddings.tlp` (informational), independently of `auth_id`. An
operator may *choose* to align `auth_id` with TLP (e.g., set
`--auth-id tlp:amber` for an AMBER bundle and provision a matching
`rag_acl` row), but that's an organizational convention, not a loader
behaviour.

## Loader workflow

A new CLI entry point — proposed: `uv run iris stix-bootstrap <bundle.json>`
— or a management endpoint. Invoked by the operator (not iris runtime).

```
1. Parse the STIX bundle with `stix2`. Validate version == 2.1.
2. Resolve auth_id for the run:
   - From --auth-id (per-bundle) and/or --auth-id-manifest (per-object).
   - Build the set of distinct auth_ids referenced.
   - SELECT auth_id FROM rag_acl WHERE auth_id IN (...). Reject the
     whole bundle if any referenced auth_id is missing.
3. Build the marking-definition table:
   marking_id -> tlp_string (for the rag_embeddings.tlp column only;
   no auth_id implication).
4. First pass: SDOs.
   For each entity-like object:
     a. If revoked == True:
        - On first load: hard-skip (loader option).
        - On refresh load: flip stix_revoked=true on the existing entity.
     b. Resolve auth_id (from manifest if listed, else bundle default).
     c. Resolve tlp from object_marking_refs (default 'clear').
     d. Build the chunk content (header + description).
     e. Compute chunk embedding and mention embedding.
     f. Insert rag_embeddings (with auth_id and tlp), kg_mentions_raw,
        kg_entities, kg_alias_map rows per the layout above.
5. Second pass: SROs.
   For each relationship:
     a. Validate both endpoints exist; skip dangling refs with logging.
     b. Resolve auth_id the same way (manifest or bundle default).
     c. Insert kg_relations_raw row.
6. Stamp the run in kg_stix_bootstrap_runs (audit table; iris-provided):
   (bundle_name, bundle_version, loaded_at, sdo_count, sro_count,
    skipped_count, errors).
7. Operator follow-up: run the resolution job to materialise kg_edges.
```

**Idempotency.** Re-running the loader on the same bundle produces the
same row primary keys. `ReplacingMergeTree` on `kg_entities` /
`kg_alias_map` dedups by `resolution_version`. Append-only tables
(`rag_embeddings`, `kg_mentions_raw`, `kg_relations_raw`) get duplicate
rows on the same primary key; the deterministic-ID scheme keeps them
logically equivalent. Operator can periodically run
`OPTIMIZE TABLE … DEDUPLICATE` to reclaim space.

## Refresh strategy

MITRE-CTI publishes versioned releases (~quarterly). Recommended cadence:

1. Schedule the loader monthly, or on MITRE release.
2. New entities → inserted as fresh rows.
3. Updated entities (same `stix_id`, new content) → `ReplacingMergeTree`
   keeps the highest `resolution_version`.
4. Revoked entities (new bundle has `revoked: true`) → loader sets
   `stix_revoked = true` in `properties_merged`. The query path filters
   `stix_revoked = true` by default; an operator-controlled flag (e.g.
   `include_revoked = true` in the feature query params) brings them
   back for historical investigations.
5. Re-run the resolution job to refresh `kg_edges`.

## What iris owns vs. what the operator owns

| Concern | Owner |
|---|---|
| Implementing the loader (CLI command and module) | Iris |
| Maintaining the STIX→schema mapping config in the repo | Iris |
| Providing the `kg_stix_bootstrap_runs` audit table | Iris |
| Fetching STIX bundles (TAXII / file download / `mitre/cti` git submodule) | Operator |
| Scheduling and invoking the loader | Operator |
| **Choosing `auth_id`(s) for each bundle / object** (the classification decision) | Operator |
| Ensuring the chosen `auth_id`(s) exist in `rag_acl` before loading | Operator |
| Running the resolution job after a bootstrap load to refresh `kg_edges` | Operator |
| Pruning duplicate rows in append-only tables (`OPTIMIZE … DEDUPLICATE`) | Operator |

## Non-goals

- No live TAXII subscription / push-driven updates in v1.
- No vendor STIX dialect normalization beyond MITRE-CTI's `x-mitre-*`.
- **No content-based `auth_id` inference.** TLP markings inside STIX
  objects are *not* read as authorization input; they only populate the
  informational `rag_embeddings.tlp` column. `auth_id` is supplied
  externally by the operator.
- No automatic creation of missing `rag_acl` rows.
- No LLM re-extraction over STIX descriptions.
- No two-way sync (iris-extracted entities never exported as STIX).
- No fine-grained masking inside a single STIX object — the whole
  description chunk shares one `auth_id` even if portions might warrant
  different markings (rare in practice).

## Open questions

1. **STIX descriptions in synthesis citations.** They land in
   `rag_embeddings` as chunks, so they're retrievable and citable
   alongside corpus chunks. Probably wanted — analysts will ground
   answers in ATT&CK descriptions. Distinguish them in the UI by `doc_id`
   prefix (`stix:`). Confirm UX expectation.
2. **MITRE sub-techniques: separate entities or properties?**
   Sub-techniques (`T1059.001`) have their own STIX objects with
   `subtechnique-of` relations to parents. v1 treats them as separate
   `AttackPattern` entities with a `PART_OF` edge to the parent.
   Alternative: model children only as a `subtechniques` array property
   on the parent. v1 keeps both (edge for traversal, property for fast
   lookup) — revisit if storage cost matters.
3. **Handling vendor-CTI STIX with non-standard relationship types.**
   Currently fall back to `RELATED_TO`. If a frequent unmapped type
   appears, the operator extends the mapping; a metrics tab on the
   admin UI counting "fallback edges by stix_relationship_type" would
   make this visible. Out of v1 scope but worth flagging.
