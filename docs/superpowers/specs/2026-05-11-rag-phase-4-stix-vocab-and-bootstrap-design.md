# RAG phase 4 — STIX vocabulary + connector — high-level spec

**Status:** design only.
**Date:** 2026-05-11.
**Series:**
- Phase 1 (`2026-05-11-rag-phase-1-vector-rag-with-acl-design.md`) — vector RAG with row-policy ACL.
- Phase 2 (`2026-05-11-rag-phase-2-ingestion-design.md`) — data ingestion pipeline.
- Phase 3 (`2026-05-11-rag-phase-3-knowledge-graph-design.md`) — knowledge graph extension.
- **Phase 4 (this spec)** — STIX vocabulary + connector.

## Goal

Two related capabilities for deployments whose corpus includes
STIX 2.1 content (most commonly DFIR-flavoured, but the mechanism is
generic — any structured-data feed could follow the same pattern):

1. **Derive the KG vocabulary from STIX bundles.** Generate
   `kg_schema.json` (the entity-type / relation-type vocabulary fed
   to the phase-3 LLM extractor) directly from the STIX content the
   deployment will ingest, so LLM extractions from unstructured
   documents resolve cleanly against STIX-sourced canonical entities.
2. **Ingest STIX content via a connector for the phase-2 pipeline.**
   A `StixConnector` implementing the phase-2 `IngestionConnector`
   Protocol yields one `AcquiredDocument` per STIX object, with
   `pre_extracted` filled in so the phase-2 pipeline writes the KG
   rows directly without invoking the LLM extractor. STIX is already
   structured; running it through extraction would be pure waste.
   The connector reuses every phase-2 mechanism (auth_id validation,
   audit tables, `redocument` helper, idempotency); no parallel
   loader, no separate workflow.

This phase is also a **template for any structured-data connector**.
The same `pre_extracted` mechanism could front a JIRA exporter (each
issue → entity, each link → relation), a Confluence exporter (each
page → entity, mentions of people → entities via author/assignee),
an email connector that pre-extracts From/To/Subject as entities,
etc. STIX is the canonical example because it's well-specified and
because MITRE-CTI is free, but the design is not STIX-specific.

## Scope

In scope:
1. STIX vocabulary scanner + renderer (Part 1; reusable for any
   structured corpus that has a fixed type vocabulary).
2. STIX → iris schema mapping (entity / relation type tables).
3. ID-assignment rules (STIX-native UUIDs for entities; `uuid5` for
   edges).
4. `StixConnector` implementing phase-2's `IngestionConnector` —
   what the connector yields for SDOs vs SROs, including the
   `pre_extracted` payload.
5. `auth_id` is supplied externally by the operator (same model as
   the phase-2 ingestion pipeline).
6. Refresh / idempotency strategy via the phase-2 pipeline's
   existing mechanisms.

Out of scope:
- TAXII network client implementation (use `stix2`/`taxii2-client` or
  a static dump).
- Vendor-specific STIX dialect normalization beyond MITRE-CTI's
  `x-mitre-*` extensions.
- Live STIX subscription (the connector is invoked by phase-2's
  normal scheduling).
- LLM re-extraction over STIX-sourced descriptions (skipped by
  design — that's the whole point of `pre_extracted`).
- Two-way sync (iris-extracted entities never pushed back out as
  STIX).
- Multi-language vocabulary handling.
- A parallel STIX-only loader pipeline (the previous design; replaced
  by the connector-on-phase-2-pipeline pattern).

---

# Part 1 — STIX vocabulary extraction

## Why

The phase-3 KG extractor needs a fixed vocabulary to constrain LLM
output. The right vocabulary matches the actual content of the
ingested corpora — anything else creates a naming mismatch and the
resolver's Stage 1.5 can't merge. STIX bundles (used by DFIR
deployments most commonly, but also threat-intel feeds in any
security-aware deployment) come with an implicit vocabulary baked
into their type system; this section turns that into iris's
`kg_schema.json`.

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

# Part 2 — `StixConnector` (a phase-2 `IngestionConnector`)

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

Unmapped relationship types fall back to `RELATED_TO`. The connector
prefixes the `evidence` field with a structured marker:
`f"[stix_relationship_type={original}] {sro_description_if_present}"`.
Falls are logged so the operator can extend the mapping.

### Property preservation

STIX identifiers go into `kg_entities.external_ids` (keyed by namespace);
other STIX metadata goes into `kg_entities.properties_merged`.

**Into `external_ids`** (one Map, one bloom-filter index, generic
across all connectors):

- `mitre_attack`: e.g. `"T1059.001"` — from
  `external_references[*].external_id` where `source_name == "mitre-attack"`.
- `cve`: e.g. `"CVE-2024-1234"` — from CVE external references.
- `capec`: e.g. `"CAPEC-242"` — from corresponding external references.
- `cwe`: e.g. `"CWE-78"` — from corresponding external references.
- `stix`: the original STIX object id (e.g. `"attack-pattern--abc..."`)
  for traceability back to the source bundle.

**Into `properties_merged`** (non-identifier metadata):

- `kill_chain_phases` — denormalized list.
- `stix_revoked` — bool (default `false`); flipped to `true` on
  refresh when a newer bundle marks the object revoked. The synthesis
  path filters `stix_revoked = "true"` by default.
- `stix_source`, `stix_source_version` — bundle provenance.

## ID-assignment policy

STIX-sourced entities use the **STIX-native UUID** (parsed out of the
`<type>--<uuid>` form) as their `entity_id`. This bypasses the
`uuid5(NS_ENTITY, f"{entity_type}::{canonical_name_normalized}")` scheme used for LLM-extracted
entities.

Rationale:
- STIX IDs are globally unique and stable across bundle releases.
- Refresh becomes idempotent (same STIX object → same `entity_id`
  forever).
- Future LLM-extracted mentions ("T1059" in a blog post) get resolved
  into STIX-sourced canonicals via phase-3's Stage 1.5 lookup.

**Edge IDs** continue to use
`uuid5(NS_EDGE, f"{source_entity_id}::{relation_type}::{target_entity_id}")` —
STIX SROs have their own UUIDs but those change when MITRE re-issues
the same relationship; we don't want edge identity to flap.

The two `entity_id` schemes (STIX-native, `uuid5`) coexist in
`kg_entities` without conflict — both are UUIDs.

## Authorization (`auth_id`)

The connector does **not** interpret STIX content as authorization
input. `auth_id` for every document yielded by the connector is
supplied externally by the operator, exactly the same model as the
phase-2 ingestion pipeline uses for ordinary documents.

Two delivery mechanisms, configured on the connector instance:

1. **Per-bundle `auth_id`** — `StixConnector(bundle_path, auth_id="customer:acme")`.
   Every SDO and SRO in the bundle gets the same `auth_id`.
2. **Per-object manifest** — `StixConnector(bundle_path, auth_id_manifest=Path(...))`
   with a `{stix_id: auth_id}` map; falls back to a per-bundle
   default for unlisted objects.

Operator pre-conditions are the same as for any other connector:

- Every distinct `auth_id` the connector will yield must already
  exist in `rag_acl`. The phase-2 pipeline's existing validation
  (Stage 6 — `enrich metadata`) rejects documents with an unknown
  `auth_id`, so a misconfigured bundle fails the same way a
  misconfigured PDF would.
- The connector **does not** auto-create `rag_acl` rows.

## What the `StixConnector` yields

The connector's `acquire()` iterates the STIX bundle and yields one
`AcquiredDocument` per STIX object. Two shapes:

### For each STIX SDO (entity-like object) — one content-bearing document

```python
AcquiredDocument(
    source_uri = f"stix:{stix_id}",
    raw_bytes  = (<header> + "\n\n" + sdo.description).encode("utf-8"),
    auth_id    = <operator-supplied, per-bundle or per-object>,
    source_metadata = {
        "stix_source": "mitre-cti",
        "stix_source_version": "15.1",
        "classified_by": "stix-connector",
    },
    pre_extracted = PreExtractedKG(
        mentions = [PreExtractedMention(
            local_id = 1,
            entity_type = <per the mapping table>,
            name_surface = sdo.name,
            aliases = sdo.get("aliases", []),
            # identifiers -> external_ids on kg_entities (resolver routes them)
            external_ids = {
                "mitre_attack": <from external_references>,  # optional
                "cve": <from external_references>,           # optional
                "capec": <from external_references>,         # optional
                "cwe": <from external_references>,           # optional
                "stix": sdo.id,
            },
            # non-identifier metadata -> properties_merged
            properties = {
                "kill_chain_phases": ...,
                "stix_revoked": str(sdo.get("revoked", False)),
                "stix_source": "mitre-cti",
                "stix_source_version": "15.1",
            },
            canonical_entity_id = <the STIX-native UUID parsed from sdo.id>,
        )],
        relations = [],
    ),
)
```

The phase-2 pipeline runs the full content path for this document:
parse the embedded text, chunk it (typically one chunk per SDO since
descriptions are short), embed, store. The `pre_extracted` payload
makes Stage 9 (KG handoff) write a single synthetic mention plus a
direct alias-map row pointing at the STIX-native canonical
`entity_id`. No queue task is enqueued. The phase-3 resolution job's
next run picks up the new `kg_entities` row (deterministic by
`entity_id`, which is the STIX UUID) and aggregates it normally.

### For each STIX SRO (relationship) — one content-less relation-only document

```python
AcquiredDocument(
    source_uri = f"stix:{sro.id}",
    raw_bytes  = b"",                # no content; relation-only
    auth_id    = <operator-supplied, per-bundle or per-object>,
    source_metadata = {...},
    pre_extracted = PreExtractedKG(
        mentions = [],
        relations = [PreExtractedRelation(
            source_local_id = -1,    # cross-document: refers to the
                                     #   synthetic mention emitted by
                                     #   the SDO document at sro.source_ref
            target_local_id = -2,    # likewise for sro.target_ref
            relation_type = <per the relation mapping>,
            evidence = sro.get("description", "") or f"[stix_relationship_type={sro.relationship_type}]",
        )],
    ),
)
```

The phase-2 pipeline detects empty `raw_bytes` and **skips parse →
chunk → embed → store**. It still validates `auth_id` and writes the
`kg_relations_raw` row in Stage 9. The cross-document `local_id`
references (negative values, or any sentinel the connector designs) are
resolved by the connector before yielding: the connector knows the
SDO chunk_ids ahead of time (deterministic from
`uuid5(doc_id, f"stix:{stix_id}:description")`) so it can compute the
target mention_ids directly. The relation row gets the per-bundle
`doc_id` for grouping and the operator-supplied `auth_id` (matching
the source/target SDOs by default; manifest overrides per SRO if
needed).

**`kg_edges`** — the connector does not insert directly. The next
phase-3 resolution-job run derives `kg_edges` from `kg_relations_raw`
+ `kg_alias_map` via the standard derivation path. Operator runs the
resolution job after each bundle ingest.

### Idempotency and refresh

All UUIDs the connector emits derive from stable STIX inputs (object
id, bundle source name), so re-running on the same bundle produces
identical row keys; `ReplacingMergeTree` on `kg_entities` /
`kg_alias_map` / `kg_mentions_raw` / `kg_relations_raw` dedupes
naturally.

On a new MITRE-CTI release (or any bundle refresh):

1. Operator re-runs the connector against the new bundle.
2. Updated SDOs (same `stix_id`, new content) overwrite the previous
   `kg_entities` / `kg_alias_map` rows via ReplacingMergeTree's
   `resolution_version` semantics.
3. SDOs marked `revoked: true`: the connector sets `stix_revoked =
   "true"` in the mention's `properties`; the resolution job
   propagates that to `kg_entities.properties_merged`. The synthesis
   path filters revoked entries by default.
4. Operator runs the phase-3 resolution job to refresh `kg_edges`.

Recommended cadence: run the connector monthly (or on MITRE release),
then the resolution job. No separate STIX audit table — the phase-2
pipeline's `ingest_runs` and `ingest_failures` audit every bundle
ingest like any other connector run.

## What iris owns vs. what the operator owns

| Concern | Owner |
|---|---|
| Implementing the scanner + renderer (`tools/scan_stix_vocab.py`, `tools/render_kg_vocab.py`) | Iris |
| Implementing the `StixConnector` (subclass of `IngestionConnector`) | Iris |
| Maintaining the STIX → schema mapping config in repo | Iris |
| Loading `config/kg_schema.json` at startup | Iris |
| Fetching STIX bundles (TAXII / file download / `mitre/cti` submodule) | Operator |
| Running the scanner + renderer to regenerate `kg_schema.json` | Operator |
| Reviewing proposed vocab additions on each MITRE release | Operator |
| **Choosing `auth_id`(s) for each bundle / object** (classification decision) | Operator |
| Ensuring chosen `auth_id`(s) exist in `rag_acl` before connector runs | Operator |
| Scheduling the phase-2 pipeline runs that drive `StixConnector` | Operator |
| Running the phase-3 resolution job after each bundle ingest | Operator |

## Tests

Phase-4 tests use the phase-1 `rag_env` fixture (skip-on-missing).
Additional test surface:

- Scanner / renderer unit tests on a fixture STIX bundle (no external
  resources).
- Connector end-to-end (requires `.rag_env`): run the phase-2
  pipeline with `StixConnector` over a fixture MITRE-CTI-shaped
  bundle into a test database, run resolution, query graph-path,
  verify STIX-sourced entities are reachable and the Stage 1.5
  merge correctly de-duplicates LLM-extracted mentions of the same
  MITRE technique.

## Non-goals

- No live TAXII subscription / push-driven updates in v1.
- No vendor STIX dialect normalization beyond MITRE-CTI's `x-mitre-*`.
- **No content-based `auth_id` inference.** STIX markings (including
  TLP) are not interpreted as authorization input; `auth_id` is
  always operator-supplied via the connector instance.
- No automatic creation of missing `rag_acl` rows.
- No LLM re-extraction over STIX descriptions (defeats the point of
  `pre_extracted`).
- No two-way sync.
- No fine-grained masking inside a single STIX object — the whole
  description chunk shares one `auth_id`.
- No separate parallel STIX-loader pipeline (this is the design
  change from the previous spec revision — STIX now runs through the
  phase-2 pipeline as a connector).

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
5. **Per-corpus schemas.** Different deployments (DFIR, internal
   documentation, email archive, customer support tickets) want
   different vocabs. Today the schema is global per iris instance;
   if iris later hosts multiple RAG databases of different shapes,
   this needs to become per-database config.
6. **STIX 2.0 fallback.** A few older feeds still emit 2.0. The
   scanner is version-agnostic on the property-counting path but the
   rename tables assume 2.1 type names. Pin to 2.1 in v1.
