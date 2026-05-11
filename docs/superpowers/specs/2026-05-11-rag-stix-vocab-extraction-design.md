# Extracting the STIX vocabulary for the LLM extractor — high-level spec

**Status:** design only.
**Date:** 2026-05-11.
**Companions:**
- `2026-05-11-rag-kg-extraction-and-resolution-design.md` (KG extraction + schema).
- `2026-05-11-rag-stix-bootstrap-loader-design.md` (STIX bundle bootstrap).

## Goal

Define how to derive iris's `kg_schema.json` (the operator-editable entity-
and relation-type vocabulary fed to the LLM extractor) directly from the
STIX 2.1 bundles iris ingests. The vocabulary must match the empirical
content of the bundles — not the OASIS spec in the abstract — so LLM
extractions from unstructured DFIR documents resolve cleanly against
STIX-sourced canonical entities.

## Scope

In scope:
1. Sources for STIX vocabulary (spec, library, bundle).
2. A small Python utility that scans one or more STIX bundles and emits the
   present vocabulary as JSON.
3. A renderer that converts the scan output into the extractor prompt's
   `Entity types:` / `Relation types:` lines.
4. STIX → iris naming canonicalization (snake-case → PascalCase /
   SCREAMING_SNAKE_CASE).
5. Where the resulting config lives in iris and how it's refreshed.

Out of scope:
- LLM-driven open extraction on non-STIX corpora (covered indirectly by the
  KG spec; the same shape of output works there).
- TAXII subscription / live polling — the scanner consumes a static bundle.
- Multi-language vocabulary handling.

## Sources of STIX vocabulary

Three places to pull from, ordered by relevance to a real iris deployment:

1. **OASIS STIX 2.1 spec.** Authoritative; lists every defined SDO/SRO
   type. Static, only changes on spec revision. Useful for sanity-checking
   the scan, not as the primary source.
2. **The `stix2` Python library.** Each STIX class carries `_type`;
   `stix2.v21` exposes them. Programmatic but reflects the spec, not your
   data.
3. **The actual bundle being loaded** (MITRE-CTI, MISP export, vendor
   STIX). **This is the primary source.** What's empirically present is
   what queries will need to match.

## Scanner

Single-file utility (proposed location: `tools/scan_stix_vocab.py` in the
iris repo). One-shot script; no external dependencies beyond the stdlib.

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

Invocation:

```
python tools/scan_stix_vocab.py enterprise-attack.json > stix_vocab.json
```

Run it once per bundle the deployment ingests (Enterprise ATT&CK, ICS
ATT&CK, Mobile ATT&CK, MISP feeds, etc.). Merge the per-bundle counters
into a combined vocabulary.

### Sample output (MITRE-CTI `enterprise-attack.json`)

```json
{
  "entity_types": [
    ["attack-pattern", 798],
    ["malware", 681],
    ["course-of-action", 269],
    ["intrusion-set", 159],
    ["x-mitre-data-component", 116],
    ["tool", 87],
    ["x-mitre-data-source", 38],
    ["campaign", 31],
    ["x-mitre-tactic", 14],
    ["identity", 2]
  ],
  "relation_types": [
    ["uses", 14328],
    ["detects", 4682],
    ["mitigates", 1041],
    ["subtechnique-of", 467],
    ["revoked-by", 220],
    ["attributed-to", 64]
  ],
  "properties_per_entity_type": {
    "attack-pattern": ["created", "description", "external_references",
                       "kill_chain_phases", "name", "..."]
  }
}
```

Frequency tail is the noise filter — drop singletons and rare types.

## Renderer (vocab → extractor prompt fragment)

Second small utility (proposed location: `tools/render_kg_vocab.py`). Reads
the scan output, applies STIX → iris canonicalization, returns the prompt
fragment the extractor injects.

```python
import json
from pathlib import Path

ENTITY_RENAME = {
    "attack-pattern": "AttackPattern",
    "course-of-action": "CourseOfAction",
    "intrusion-set": "IntrusionSet",
    "threat-actor": "ThreatActor",
    "x-mitre-tactic": "Tactic",
    "x-mitre-data-source": "DataSource",
    "x-mitre-data-component": "DataComponent",
    # extend as new types appear
}
RELATION_RENAME = {
    "uses": "USES",
    "mitigates": "MITIGATES",
    "subtechnique-of": "PART_OF",
    "detects": "DETECTS",
    "attributed-to": "ATTRIBUTED_TO",
    "targets": "TARGETS",
    "indicates": "INDICATES",
    "revoked-by": None,  # not modeled as edge (see STIX bootstrap spec)
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


if __name__ == "__main__":
    vocab = json.loads(Path("stix_vocab.json").read_text())
    print(render(vocab))
```

Output (illustrative):

```
Entity types (pick one per entity):
  AttackPattern | Malware | CourseOfAction | IntrusionSet |
  DataComponent | Tool | DataSource | Campaign | Tactic | Identity

Relation types (pick one per relation):
  USES | DETECTS | MITIGATES | PART_OF | ATTRIBUTED_TO
```

That fragment drops straight into the extractor prompt's `Entity types:` /
`Relation types:` lines defined in the KG extraction spec.

## STIX → iris canonicalization rules

- **Entity types:** STIX uses `kebab-case` (`attack-pattern`). iris uses
  `PascalCase` (`AttackPattern`). Default rule: `t.replace("-", "_").title().replace("_", "")`,
  with an override table for non-obvious renames (`intrusion-set` →
  `IntrusionSet`; `x-mitre-tactic` → `Tactic` — drops the `XMitre` prefix).
- **Relation types:** STIX uses `kebab-case` (`subtechnique-of`). iris uses
  `SCREAMING_SNAKE_CASE` (`PART_OF`). Default rule: `r.upper().replace("-", "_")`,
  with overrides where semantics merge (`subtechnique-of` → `PART_OF`) or
  drop (`revoked-by` → handled as a soft-delete signal, not an edge).
- **Skip list:** never emit `bundle`, `marking-definition`. Optionally
  skip `x-mitre-matrix` (purely organizational).

The rename tables and skip list are part of `kg_schema.json`'s
provenance — keep them visible in the repo so the mapping decisions are
auditable.

## Where the vocab lives in iris

- **Path:** `config/kg_schema.json` (checked in, versioned with code).
- **Loader:** a small startup function reads the file and exposes
  `(entity_types: list[str], relation_types: list[str])` as typed config
  to the extractor + resolver.
- **Stamp:** the file includes a `schema_version` field. Every regeneration
  bumps it; the value becomes part of the `prompt_version` column in
  `kg_mentions_raw` and `kg_relations_raw`, so old extractions stay
  queryable while new ones use the new vocab.
- **Shape:**

  ```json
  {
    "schema_version": "2026-05-11-mitre-attack-15.1",
    "entity_types": ["AttackPattern", "Malware", "...", "Custom"],
    "relation_types": ["USES", "MITIGATES", "...", "RELATED_TO"],
    "stix_rename": {
      "entity_types": {"attack-pattern": "AttackPattern", "...": "..."},
      "relation_types": {"uses": "USES", "...": "..."}
    },
    "stix_skip": ["bundle", "marking-definition", "x-mitre-matrix"]
  }
  ```

## Where to get bundles

Three sources, pick by latency vs. friction:

```bash
# 1. Git clone the canonical mitre/cti repo (large but simple)
git clone --depth 1 https://github.com/mitre/cti.git
# bundles at: cti/enterprise-attack/enterprise-attack.json
#             cti/ics-attack/ics-attack.json
#             cti/mobile-attack/mobile-attack.json

# 2. Direct file download (smaller, current release)
curl -O https://raw.githubusercontent.com/mitre/cti/master/enterprise-attack/enterprise-attack.json

# 3. TAXII (programmatic, supports incremental updates)
pip install taxii2-client
# server:      https://attack-taxii.mitre.org/api/v21/
# collections: enterprise-attack | mobile-attack | ics-attack
```

For full DFIR coverage, scan all three MITRE bundles plus any external
STIX feeds (vendor CTI, MISP exports), and merge the per-bundle vocabs.

## Operational rhythm

1. Operator runs `scan_stix_vocab.py` against every bundle the deployment
   ingests.
2. Merge per-bundle entity/relation counters.
3. Take up to ~15 entity types and up to ~15 relation types by frequency
   (MITRE-CTI alone yields ~10 entity types and ~6 relation types — the
   cap is an upper bound, not a target).
4. Eyeball the list — drop unwanted types (`marking-definition`, `bundle`,
   purely-organizational `x-mitre-matrix`).
5. Apply the rename tables.
6. Commit the resulting `config/kg_schema.json` with a bumped
   `schema_version`.
7. On each MITRE release, re-scan, diff against the previous schema,
   decide whether to widen the vocab or hold steady.

The same pipeline works for non-STIX corpora — substitute step 1 with a
"run open extraction on a sample, count emitted types" pass (the inductive
approach from the KG extraction spec). Either way the artifact is the
same: a `kg_schema.json` with the active vocabulary.

## What iris owns vs. what the operator owns

| Concern | Owner |
|---|---|
| Providing `tools/scan_stix_vocab.py` and `tools/render_kg_vocab.py` | Iris |
| Maintaining the rename tables (`STIX_RENAME`) in repo | Iris |
| Loading `config/kg_schema.json` at startup, surfacing as typed config | Iris |
| Fetching STIX bundles | Operator |
| Running the scanner + renderer to regenerate `kg_schema.json` | Operator |
| Reviewing the proposed vocab additions on each MITRE release | Operator |
| Committing the updated schema | Operator (PR) |

## Non-goals

- No automatic schema-drift detection across MITRE releases — the operator
  runs the scan and reviews diffs manually.
- No live integration with TAXII for vocabulary refresh.
- No automatic regeneration of `kg_schema.json` from production extraction
  output. (That would be useful — see "Open questions" — but is out of v1.)
- No multi-language entity-name normalization at the vocab layer.

## Open questions

1. **Live feedback loop from production extractions.** Should iris log the
   distribution of LLM-emitted entity/relation types from real extractions
   (alongside `prompt_version`) so the operator can see, e.g., "the model
   tried to emit `Customer` 50 times last week but it's not in the
   vocab"? Useful signal for vocab expansion. Defer to a follow-on.
2. **Per-corpus schemas.** A DFIR deployment and a corporate-KB deployment
   want different vocabs. Today the schema is global per iris instance;
   if iris later hosts multiple RAG databases of different shapes, this
   needs to become per-database config.
3. **STIX 2.0 fallback.** A few older feeds still emit 2.0. The scanner is
   version-agnostic on the property-counting path but the rename tables
   assume 2.1 type names. Pin to 2.1 in v1; document the upgrade path if
   2.0 sources appear.
