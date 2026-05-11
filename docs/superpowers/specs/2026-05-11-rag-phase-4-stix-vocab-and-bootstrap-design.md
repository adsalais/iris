# RAG phase 4 — STIX vocabulary + bootstrap loader — high-level spec

**Status:** design only.
**Date:** 2026-05-11.
**Series:**
- Phase 1 (`2026-05-11-rag-phase-1-vector-rag-with-acl-design.md`) — vector RAG with row-policy ACL.
- Phase 2 (`2026-05-11-rag-phase-2-ingestion-design.md`) — data ingestion pipeline.
- Phase 3 (`2026-05-11-rag-phase-3-knowledge-graph-design.md`) — knowledge graph extension.
- **Phase 4 (this spec)** — STIX vocabulary + bootstrap.

## Goal

Two related capabilities for DFIR-flavoured deployments:

1. **Derive the KG vocabulary from STIX bundles.** Generate
   `kg_schema.json` (the entity-type / relation-type vocabulary fed
   to the phase-3 LLM extractor) directly from the STIX content the
   deployment will ingest, so LLM extractions from unstructured DFIR
   documents resolve cleanly against STIX-sourced canonical entities.
2. **Bootstrap the KG with pre-structured STIX content.** Load
   STIX 2.1 bundles (MITRE ATT&CK / MITRE-CTI first; TAXII feeds,
   MISP exports, vendor STIX later) directly into phase-1's
   `rag_embeddings` and phase-3's KG tables **without going through
   the LLM extractor** — STIX is already structured, so extraction is
   pure waste. Gives iris a working KG on day one (thousands of
   techniques, threat actors, malware families with their relations).

## Scope

In scope:
1. STIX vocabulary scanner + renderer (Part 1).
2. STIX → iris schema mapping (entity / relation type tables).
3. ID-assignment rules (STIX-native UUIDs for entities; `uuid5` for
   edges).
4. Where STIX content lands across phase-1's `rag_embeddings` and
   phase-3's 5 KG tables.
5. Authorization — externally supplied `auth_id`, same model as
   phase-2 ingestion. The loader does NOT derive `auth_id` from STIX
   TLP markings. TLP markings are parsed into `rag_embeddings.tlp` as
   informational metadata.
6. Loader workflow, refresh / idempotency strategy, operator
   interface.

Out of scope:
- TAXII network client implementation (use `stix2`/`taxii2-client` or
  a static dump).
- Vendor-specific STIX dialect normalization beyond MITRE-CTI's
  `x-mitre-*` extensions.
- Live STIX subscription (phase-4 is a scheduled batch).
- LLM re-extraction over STIX-sourced descriptions (skipped by
  design).
- Two-way sync (iris-extracted entities never pushed back out as
  STIX).
- Multi-language vocabulary handling.

---

# Part 1 — STIX vocabulary extraction

## Why

The phase-3 KG extractor needs a fixed vocabulary to constrain LLM
output. For DFIR corpora, the right vocabulary matches the STIX
objects the deployment will ingest — anything else creates a naming
mismatch and the resolver's Stage 1.5 can't merge.

This part defines two small utilities and the lifecycle:

1. `tools/scan_stix_vocab.py` — scans a STIX bundle, emits the
   present entity / relation types as JSON.
2. `tools/render_kg_vocab.py` — converts the scan output to iris's
   canonical naming (`PascalCase` entities, `SCREAMING_SNAKE_CASE`
   relations) and produces the prompt fragment the extractor injects.

## Scanner

Single-file utility (`tools/scan_stix_vocab.py`), stdlib-only:

```python
"""Emit the entity/relation vocabulary present in a STIX bundle."""
import json
import sys
from collections import Counter
from pathlib import Path


def scan(path: Path) -> dict:
    bundle = json.loads(path.read_text())
    objects = bundle.get("objects", [])

    entity_type_counts: Counter[str] = Counter()
    relation_type_counts: Counter[str] = Counter()
    properties_per_type: dict[str, set[str]] = {}

    for obj in objects:
        t = obj.get("type")
        if t in ("marking-definition", "bundle"):
            continue
        if t == "relationship":
            rt = obj.get("relationship_type")
            if rt:
                relation_type_counts[rt] += 1
            continue
        entity_type_counts[t] += 1
        properties_per_type.setdefault(t, set()).update(obj.keys())

    return {
        "entity_types": entity_type_counts.most_common(),
        "relation_types": relation_type_counts.most_common(),
        "properties_per_entity_type": {
            k: sorted(v) for k, v in properties_per_type.items()
        },
    }


if __name__ == "__main__":
    print(json.dumps(scan(Path(sys.argv[1])), indent=2))
```

Run once per bundle (Enterprise ATT&CK, ICS, Mobile, MISP feeds, etc.).
Merge the per-bundle counters.

## Renderer

`tools/render_kg_vocab.py` — applies STIX → iris canonicalization and
emits the extractor prompt fragment:

```python
ENTITY_RENAME = {
    "attack-pattern": "AttackPattern",
    "course-of-action": "CourseOfAction",
    "intrusion-set": "IntrusionSet",
    "threat-actor": "ThreatActor",
    "x-mitre-tactic": "Tactic",
    "x-mitre-data-source": "DataSource",
    "x-mitre-data-component": "DataComponent",
}
RELATION_RENAME = {
    "uses": "USES",
    "mitigates": "MITIGATES",
    "subtechnique-of": "PART_OF",
    "detects": "DETECTS",
    "attributed-to": "ATTRIBUTED_TO",
    "targets": "TARGETS",
    "indicates": "INDICATES",
    "revoked-by": None,  # not modeled as an edge
}


def render(vocab: dict, top_entities: int = 15, top_relations: int = 15) -> str:
    entities = [
        ENTITY_RENAME.get(t, t.replace("-", "_").title().replace("_", ""))
        for t, _ in vocab["entity_types"][:top_entities]
    ]
    relations = [
        RELATION_RENAME.get(r, r.upper().replace("-", "_"))
        for r, _ in vocab["relation_types"][:top_relations]
        if RELATION_RENAME.get(r, r) is not None
    ]
    return (
        "Entity types (pick one per entity):\n  "
        + " | ".join(entities)
        + "\n\nRelation types (pick one per relation):\n  "
        + " | ".join(relations)
    )
```

Sample output (from MITRE-CTI Enterprise ATT&CK):

```
Entity types (pick one per entity):
  AttackPattern | Malware | CourseOfAction | IntrusionSet |
  DataComponent | Tool | DataSource | Campaign | Tactic | Identity

Relation types (pick one per relation):
  USES | DETECTS | MITIGATES | PART_OF | ATTRIBUTED_TO
```

That fragment goes straight into the phase-3 extractor prompt's
`Entity types:` / `Relation types:` lines.

## Vocabulary lifecycle

- **Path:** `config/kg_schema.json` (committed, versioned with code).
- **Shape:**

  ```json
  {
    "schema_version": "2026-05-11-mitre-attack-15.1",
    "entity_types": ["AttackPattern", "Malware", "..."],
    "relation_types": ["USES", "MITIGATES", "..."],
    "stix_rename": {
      "entity_types": {"attack-pattern": "AttackPattern", "...": "..."},
      "relation_types": {"uses": "USES", "...": "..."}
    },
    "stix_skip": ["bundle", "marking-definition", "x-mitre-matrix"]
  }
  ```

- **Stamp:** `schema_version` feeds the phase-3 `prompt_version` column
  in `kg_mentions_raw` / `kg_relations_raw`. Old extractions stay
  queryable; new ones use the new vocab.

## Operational rhythm

1. Operator runs `scan_stix_vocab.py` against every bundle the
   deployment ingests.
2. Merge per-bundle counters.
3. Take up to ~15 entity types and ~15 relation types by frequency
   (MITRE-CTI alone yields ~10 entity types and ~6 relation types —
   the cap is an upper bound, not a target).
4. Eyeball the list — drop unwanted types (`marking-definition`,
   `bundle`, `x-mitre-matrix`).
5. Apply the rename tables.
6. Commit `config/kg_schema.json` with a bumped `schema_version`.
7. On each MITRE release, re-scan, diff, decide whether to widen the
   vocab.

The same pipeline works for non-STIX corpora — substitute step 1 with
"run open extraction on a sample, count emitted types" (the inductive
approach from the phase-3 spec).

---

# Part 2 — STIX bundle bootstrap loader

## STIX → iris schema mapping

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
| `x-mitre-tactic` | `Tactic` |
| `x-mitre-data-source` | `DataSource` |
| `x-mitre-matrix` | *skipped* — purely organizational |

Kill-chain phases referenced inside SDOs become standalone
`KillChainPhase` entities, linked to the referencing SDO via
`PART_OF_KILLCHAIN_STAGE` relations.

These extend the phase-3 starter schema; operators update
`config/kg_schema.json` accordingly (Part 1 above generates the right
entries).

### Relation-type mapping

| STIX `relationship_type` | iris `relation_type` |
|---|---|
| `uses` | `USES` |
| `attributed-to` | `ATTRIBUTED_TO` |
| `targets` | `TARGETS` |
| `indicates` | `INDICATES` |
| `mitigates` | `MITIGATES` |
| `derived-from` | `DERIVED_FROM` |
| `related-to` | `RELATED_TO` |
| `subtechnique-of` | `PART_OF` |
| `revoked-by` | *soft-delete signal; not stored as edge* |

Unmapped relationship types fall back to `RELATED_TO`. The loader
prefixes the `evidence` field with a structured marker:
`f"[stix_relationship_type={original}] {sro_description_if_present}"`.
Falls are logged so the operator can extend the mapping.

### Property preservation

Critical STIX properties land in `properties_merged` on the entity:

- `mitre_attack_id` (e.g. `T1059.001`) — from
  `external_references[*].external_id` where `source_name == "mitre-attack"`.
- `cve_id` — from CVE external references.
- `capec_id`, `cwe_id` — from corresponding external references.
- `tlp` — parsed from `object_marking_refs`. **Stored on
  `rag_embeddings.tlp` for the SDO's description chunk as
  informational metadata ONLY.** Does not affect `auth_id` or
  row-policy evaluation.
- `kill_chain_phases` — denormalized list.
- `stix_id` — original STIX object id, for traceability.
- `stix_revoked` — bool (default `false`); flipped to `true` on
  refresh when a newer bundle marks the object revoked.
- `stix_source`, `stix_source_version` — bundle provenance.

## ID-assignment policy

STIX-sourced entities use the **STIX-native UUID** (parsed out of the
`<type>--<uuid>` form) as their `entity_id`. This bypasses the
`uuid5(NS, canonical_name||entity_type)` scheme used for LLM-extracted
entities.

Rationale:
- STIX IDs are globally unique and stable across bundle releases.
- Refresh becomes idempotent (same STIX object → same `entity_id`
  forever).
- Future LLM-extracted mentions ("T1059" in a blog post) get resolved
  into STIX-sourced canonicals via phase-3's Stage 1.5 lookup.

**Edge IDs** continue to use
`uuid5(NS, source_entity_id || relation_type || target_entity_id)` —
STIX SROs have their own UUIDs but those change when MITRE re-issues
the same relationship; we don't want edge identity to flap.

The two `entity_id` schemes (STIX-native, `uuid5`) coexist in
`kg_entities` without conflict — both are UUIDs.

## Authorization (`auth_id`)

The loader does **not** interpret STIX TLP markings as authorization
input. `auth_id` for every row written by the loader is supplied
externally by the operator, exactly the same model as the phase-2
ingestion spec uses for ordinary documents.

Two supported delivery mechanisms (CLI flags):

1. **Per-bundle `auth_id`** — `--auth-id customer:acme`. Every SDO and
   SRO in the bundle gets the same `auth_id`.
2. **Per-object manifest** — `--auth-id-manifest <path.json>`
   `{stix_id: auth_id}` overrides; falls back to the per-bundle default.

Operator pre-conditions:

- The supplied `auth_id` must already exist in `rag_acl`. The loader
  validates every distinct `auth_id` against `rag_acl` before any
  insert; missing rows cause the bundle to be rejected with a clear
  error.
- The loader **does not** auto-create `rag_acl` rows.

TLP markers inside STIX objects populate `rag_embeddings.tlp` only.
Operators may *choose* to align `auth_id` with TLP (e.g.,
`--auth-id tlp:amber` for an AMBER bundle + a matching `rag_acl` row)
but that's an organizational convention, not loader behaviour.

## Where STIX content lands

For each STIX SDO (entity-like object):

**1. One row in `rag_embeddings`** — content = SDO `description`
prefixed with a normalized header (name, aliases, MITRE ID, kill-chain
phases).

| Column | Value |
|---|---|
| `doc_id` | `f"stix:{stix_source}"` (e.g. `"stix:mitre-cti"`) |
| `chunk_id` | `f"stix:{stix_id}:description"` |
| `auth_id` | operator-supplied (per-bundle / per-object) |
| `tlp` | parsed from `object_marking_refs`, else `'clear'`. Informational. |
| `embedding` | computed by the loader |
| `content` | `<header>\n\n<description>` |

**2. One row in `kg_mentions_raw`** — synthetic mention pointing at
the chunk above:

| Column | Value |
|---|---|
| `mention_id` | `uuid5(NS, f"{chunk_id}::stix")` |
| `chunk_id` / `doc_id` / `auth_id` | matches the chunk |
| `entity_type` | per the mapping table |
| `name_surface` | SDO's `name` |
| `aliases` | SDO's `aliases` list |
| `mention_kind` | `'direct'` (always — STIX SDOs are direct mentions) |
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
| `properties_merged` | the property set |
| `auth_ids` | `[auth_id]` (singleton; resolution may extend later) |
| `resolution_version` | loader's stamp |

**4. One row in `kg_alias_map`**:

| Column | Value |
|---|---|
| `mention_id` | from step 2 |
| `entity_id` | from step 3 |
| `auth_id` | matches the mention |
| `resolution_method` | `'exact'`, `confidence = 1.0` |

For each STIX SRO (relationship):

**5. One row in `kg_relations_raw`**:

| Column | Value |
|---|---|
| `relation_id` | `uuid5(NS, f"{stix_relationship_id}")` |
| `chunk_id` | `f"stix:{stix_relationship_id}"` (logical — no `rag_embeddings` row) |
| `auth_id` | resolved by the precedence rules below |
| `source_mention_id` / `target_mention_id` | synthetic mentions of the SRO's endpoints |
| `relation_type` | per the relation mapping |
| `evidence` | SRO `description` if present; prefixed with `[stix_relationship_type=<orig>]` for fall-back types |

**SRO `auth_id` precedence** (apply in order; first match wins):

1. **Manifest entry for the SRO's own `stix_id`.** Operator explicitly
   chose the auth_id for this relationship. Use it.
2. **Strictest of the two endpoint SDO `auth_id`s,** where "strictest"
   is determined by the operator-configured ordering. Default ordering
   (most-restrictive first):
   `tlp:red > tlp:amber_strict > tlp:amber > tlp:green > tlp:clear`,
   with `customer:*` entries treated as incomparable across customers
   (a relationship between two different `customer:*` SDOs is rejected
   with `error_kind = 'cross_tenant_sro'` — the operator must split
   the bundle or add an explicit manifest entry).
3. **Per-bundle `--auth-id` default.**

Operators can override the default strictness ordering via a config
file (`--strictness-config <path.json>`). The chosen `auth_id` must
already exist in `rag_acl` for the SRO to load; otherwise it's
rejected like any other unknown-auth_id row.

**Note on `chunk_id` for SRO-derived relation rows.** This `chunk_id`
has no corresponding `rag_embeddings` row. Consequence: when phase-3's
Stage 5 derives `kg_edges.evidence_chunks` via `groupArray(chunk_id)`,
STIX-derived edges carry `stix:<sro_id>` entries that don't fetch.
The synthesis stage filters them when computing `authorized_chunk_ids`.
STIX-derived edges still surface via entity-traversal; the model
grounds claims in the source/target SDO description chunks, which
**are** fetchable.

**6. `kg_edges`** — loader does **not** insert. The next phase-3
resolution-job run derives them. Operator must run the resolution job
after each bootstrap load.

## Loader workflow

A new CLI entry point: `uv run iris stix-bootstrap <bundle.json> [opts]`.

```
1. Parse the STIX bundle with `stix2`. Validate version == 2.1.
2. Resolve auth_id for the run:
   - From --auth-id and/or --auth-id-manifest.
   - Validate every referenced auth_id exists in rag_acl.
   - Reject the bundle if any auth_id is missing.
3. Build the marking-definition table:
   marking_id -> tlp_string -> tlp_enum_value, per the mapping below.
   STIX 2.0 markings (definition_type="tlp" with definition.tlp) and
   STIX 2.1 markings (extension-definition--<TLP2.0-UUID> form) are
   both supported:
     - TLP:WHITE (2.0)         -> 'clear'
     - TLP:CLEAR (2.1)         -> 'clear'
     - TLP:GREEN               -> 'green'
     - TLP:AMBER               -> 'amber'
     - TLP:AMBER+STRICT (2.1)  -> 'amber_strict'
     - TLP:RED                 -> 'red'
     - missing / unrecognized  -> 'clear' (the column default)
   The mapping populates rag_embeddings.tlp only (informational); it
   never feeds auth_id.
4. First pass: SDOs.
   For each entity-like object:
     a. If revoked == True:
        - On first load: hard-skip (loader option).
        - On refresh: flip stix_revoked=true on the existing entity.
     b. Resolve auth_id (manifest or per-bundle default).
     c. Resolve tlp from object_marking_refs (default 'clear').
     d. Build chunk content; compute embeddings.
     e. Insert rag_embeddings, kg_mentions_raw, kg_entities, kg_alias_map.
5. Second pass: SROs.
   For each relationship:
     a. Validate both endpoints exist; skip dangling refs with logging.
     b. Resolve auth_id (manifest or default).
     c. Insert kg_relations_raw row.
6. Stamp kg_stix_bootstrap_runs (audit table; iris-provided):
   (bundle_name, bundle_version, loaded_at, sdo_count, sro_count,
    skipped_count, errors).
7. Operator follow-up: run the phase-3 resolution job to materialise
   kg_edges.
```

**Idempotency.** Re-running on the same bundle produces the same row
primary keys. `ReplacingMergeTree` on `kg_entities` / `kg_alias_map`
dedups by `resolution_version`. Append-only tables get duplicate rows
on the same primary key; the deterministic-ID scheme keeps them
logically equivalent. Run `OPTIMIZE TABLE … DEDUPLICATE` periodically.

## Refresh strategy

MITRE-CTI publishes versioned releases (~quarterly). Recommended
cadence:

1. Schedule the loader monthly, or on MITRE release.
2. New entities → inserted as fresh rows.
3. Updated entities (same `stix_id`, new content) →
   `ReplacingMergeTree` keeps the highest `resolution_version`.
4. Revoked entities → loader sets `stix_revoked = true` in
   `properties_merged`. Query path filters revoked by default; an
   `include_revoked = true` flag brings them back for historical
   investigations.
5. Re-run the resolution job to refresh `kg_edges`.

## What iris owns vs. what the operator owns

| Concern | Owner |
|---|---|
| Implementing the scanner + renderer (`tools/scan_stix_vocab.py`, `tools/render_kg_vocab.py`) | Iris |
| Implementing the bootstrap loader (CLI + module) | Iris |
| Maintaining the STIX → schema mapping config in repo | Iris |
| Providing the `kg_stix_bootstrap_runs` audit table | Iris |
| Loading `config/kg_schema.json` at startup | Iris |
| Fetching STIX bundles (TAXII / file download / `mitre/cti` submodule) | Operator |
| Running the scanner + renderer to regenerate `kg_schema.json` | Operator |
| Reviewing proposed vocab additions on each MITRE release | Operator |
| **Choosing `auth_id`(s) for each bundle / object** (classification decision) | Operator |
| Ensuring chosen `auth_id`(s) exist in `rag_acl` before loading | Operator |
| Scheduling and invoking the loader | Operator |
| Running the phase-3 resolution job after each bootstrap load | Operator |
| Pruning duplicate rows in append-only tables | Operator |

## Tests

Phase-4 tests use the phase-1 `rag_env` fixture (skip-on-missing).
Additional test surface:

- Scanner / renderer unit tests on a fixture STIX bundle (no external
  resources).
- Loader end-to-end (requires `.rag_env`): load a fixture
  MITRE-CTI-shaped bundle into a test database, run resolution, query
  graph-path, verify STIX-sourced entities are reachable and the
  Stage 1.5 merge correctly de-duplicates LLM-extracted mentions of
  the same MITRE technique.

## Non-goals

- No live TAXII subscription / push-driven updates in v1.
- No vendor STIX dialect normalization beyond MITRE-CTI's `x-mitre-*`.
- **No content-based `auth_id` inference.** TLP markings inside STIX
  populate `rag_embeddings.tlp` only.
- No automatic creation of missing `rag_acl` rows.
- No LLM re-extraction over STIX descriptions.
- No two-way sync.
- No fine-grained masking inside a single STIX object — the whole
  description chunk shares one `auth_id`.

## Open questions

1. **STIX descriptions in synthesis citations.** They land in
   `rag_embeddings` as chunks, so they're retrievable and citable
   alongside corpus chunks. Probably wanted; distinguish in the UI by
   `doc_id` prefix (`stix:`). Confirm UX expectation.
2. **MITRE sub-techniques: separate entities or property?**
   `T1059.001` has its own STIX object with `subtechnique-of` to the
   parent. v1 keeps both (edge for traversal + `subtechniques` array
   property on the parent for fast lookup). Revisit if storage matters.
3. **Handling vendor-CTI STIX with non-standard relationship types.**
   Currently fall back to `RELATED_TO`. If a frequent unmapped type
   appears, the operator extends the mapping; an admin-UI metrics tab
   counting "fallback edges by `stix_relationship_type`" would make
   this visible. Out of v1.
4. **Live feedback loop from production extractions.** Should iris log
   the distribution of LLM-emitted entity / relation types from real
   extractions (alongside `prompt_version`) so the operator can see,
   e.g., "the model tried to emit `Customer` 50 times last week but
   it's not in the vocab"? Useful signal for vocab expansion. Defer
   to follow-on.
5. **Per-corpus schemas.** A DFIR deployment and a corporate-KB
   deployment want different vocabs. Today the schema is global per
   iris instance; if iris later hosts multiple RAG databases of
   different shapes, this needs to become per-database config.
6. **STIX 2.0 fallback.** A few older feeds still emit 2.0. The
   scanner is version-agnostic on the property-counting path but the
   rename tables assume 2.1 type names. Pin to 2.1 in v1.
