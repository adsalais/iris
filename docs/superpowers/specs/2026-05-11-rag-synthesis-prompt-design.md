# RAG synthesis: fusing graph and vector context — high-level spec

**Status:** design only.
**Date:** 2026-05-11.
**Companions:**
- `2026-05-11-rag-row-policy-acl-design.md` — chunk-level vector RAG +
  row-policy authorization.
- `2026-05-11-rag-kg-extraction-and-resolution-design.md` — KG extraction,
  hybrid resolution, storage.

## Goal

Define the synthesis stage: how vector-path and graph-path retrievals are
fused into a single LLM call that produces a grounded, cited answer. This
is the final stage of the RAG pipeline; everything upstream has already
filtered for authorization.

## v1 design choices (call out if you want to flip any)

1. **Single-pass synthesis.** One LLM call with all selected context. No
   map-reduce in v1.
2. **Hybrid context presentation.** A small `STRUCTURAL CONTEXT` block
   listing relevant entity-relation tuples drawn from `kg_edges`, followed
   by numbered chunk sources. Not just chunks; not just structure.
3. **Required inline citations.** Every factual claim must cite `[C<n>]`.
   Post-processing audits.
4. **No conversational memory in v1.** One question, one answer.
5. **No streaming.** Returns the full answer; UI streaming is follow-on.

## Inputs to the synthesis stage

When the synthesizer is called it has:

1. **The user's question** (raw text).
2. **Vector-path chunks** — top-K from `rag_embeddings` after row-policy
   filtering. Each carries `chunk_id`, `doc_id`, `content`, `vector_score`.
3. **Graph-path chunks** — chunks reached by traversing `kg_edges` and
   following `kg_mentions_raw.chunk_id`, then fetched from
   `rag_embeddings` (so they too pass the row policy). Each carries
   `chunk_id`, `doc_id`, `content`, plus the traversal evidence
   (entities + edges that led there).
4. **Authorized chunk-id set** — the union of all `chunk_id`s returned
   by the row-policied retrieval in (2) and (3), **computed before
   rerank or top-N truncation**. This is the broader set the structural
   block is filtered against; an edge whose evidence is in an authorized
   chunk that didn't make the final top-N still appears in the
   structural block — only the `[Cx]` citation may be missing for that
   chunk. Without this distinction, the structural block becomes
   over-pruned.
## Pre-synthesis pipeline

```
[vector chunks]   [graph chunks]
       \             /
        dedup by chunk_id  (mark dual-source hits "high-confidence")
                |
                v
       authorized_chunk_ids = {c.chunk_id for c in unioned set}
       (the BROAD set, before rerank/truncate)
                |
                v
       cross-encoder rerank vs question  (optional, recommended)
                |
                v
       truncate to top-N (token budget)  -> selected_chunks
                |
                v
       build structural block from kg_edges
       constraint: every included edge has at least one
       chunk_id in evidence_chunks ∩ authorized_chunk_ids
       (uses the BROAD set — keeps edges with un-ranked but authorized evidence)
                |
                v
       construct synthesis prompt
                |
                v
                LLM (single call)
                |
                v
       parse citations + build audit record
                |
                v
              answer
```

**Dedup rule.** If a chunk surfaces from both paths, label it
`retrieval=vector+graph` and keep the higher rerank score. Treat as a
strong-signal source.

**Token budget.** Reserve ~70% of the model's context window for
`SOURCES`. Concrete v1 defaults: `N = 12` chunks, structural block capped
at `~50` edges (further filtered by relevance to question entities).

**Structural-block ranking.** Edges ordered by
`relevance_to_question_entities × support_count`. Edges whose endpoints
include a question-matched entity rank first.

## Prompt structure

A single LLM call, structured as:

```
[SYSTEM]
You are a research assistant. Answer the user's question using ONLY the
sources provided below. Every factual claim must cite the source(s) that
support it using inline references of the form [C<n>], where <n> is the
source number. If the provided sources don't support an answer, say so
explicitly — do not fabricate.

When a STRUCTURAL CONTEXT block is provided, treat it as a summary of
known relationships extracted from the same sources. You may use it to
plan your answer, but every claim in your final answer must still cite a
specific [C<n>] source — not the structural context itself.

Output format:
1. A direct answer (a few sentences to a few paragraphs).
2. A "Sources" trailer listing only the [C<n>] references you actually
   cited, with their doc_id.

[STRUCTURAL CONTEXT]
The following relationships are present in the sources:
- <entity_name_A> (<entity_type_A>) --[<relation_type>]--> <entity_name_B> (<entity_type_B>)
  evidence: [C3, C7]
- <entity_name_C> (<entity_type_C>) --[<relation_type>]--> <entity_name_D> (<entity_type_D>)
  evidence: [C1, C9, C11]
...

[SOURCES]
[C1] doc_id=<doc_id>, chunk_id=<chunk_id>, retrieval=vector+graph, score=0.91
<chunk content verbatim>

[C2] doc_id=<doc_id>, chunk_id=<chunk_id>, retrieval=vector, score=0.84
<chunk content verbatim>

...

[QUESTION]
<the user's question>
```

### Notes on each block

- **`STRUCTURAL CONTEXT`** lists `kg_edges` rows by their canonical entity
  names. Evidence pointers are the same `[C<n>]` labels used in `SOURCES`
  so the model can correlate structure to verbatim text. Omitted entirely
  if no relevant edges survive the authorization filter. **The
  `[Cx]` references here are intersected with the actual `SOURCES`
  numbering** — if an edge's evidence chunks didn't end up in top-N, the
  edge still appears but with fewer (or no) `[Cx]` pointers.
- **`SOURCES`** are numbered `[C1]..[CN]`, ordered by rerank score
  (highest first). `retrieval=` tells the model where each chunk came
  from; `score=` is the post-rerank score. **STIX-logical chunk_ids
  (`stix:<sro_id>` from STIX SROs) are excluded from SOURCES** — they
  point at no `rag_embeddings` row. STIX SDO description chunks
  (`stix:<stix_id>:description`) are valid SOURCES.
- **`QUESTION`** is placed last so it stays in the model's recency window
  even at long context.

### Citation enforcement

- Inline `[C<n>]` references are required by the system prompt.
- Post-processing parses the model output, extracts cited `[C<n>]` tokens,
  and constructs an audit record:
  `(question, sources_provided, sources_cited, answer, model, prompt_version)`.
- Citation hygiene rule: every emitted `[C<n>]` must match an `n` in
  `SOURCES`. Bogus citations are a soft failure → operator-tunable
  policy: either single retry with an explicit correction instruction, or
  strip the bogus citation and log. v1 default: **strip + log**, retry is
  follow-on.

### Refusal / uncertainty

If retrieval surfaces fewer than `M` chunks (`M = 2` in v1), the prompt
prepends:

```
[NOTE] Few sources were retrieved. If they don't substantively answer the
question, say so directly — do not stretch them.
```

A soft signal; the system prompt's "do not fabricate" rule does the heavy
lifting.

## Authorization-related invariants

Non-negotiable:

1. **Every chunk in `SOURCES` has already passed the row policy on
   `rag_embeddings`.** Synthesis itself never re-evaluates authz; the
   fetch in the pre-synthesis pipeline is the enforcement point.
2. **Structural-block filtering.** Every `kg_edges` row included in
   `STRUCTURAL CONTEXT` must have at least one `chunk_id` in its
   `evidence_chunks` that is in `authorized_chunk_ids` (the BROAD
   pre-rerank set, not `selected_chunks`). Note that the `kg_edges` row
   policy already enforces "user has access to at least one contributing
   `auth_id`" at the database layer; the synthesis filter here is the
   second-layer defense that masks `chunk_id`s the user can't fetch even
   though the edge as a whole is visible.
3. **No content from outside `SOURCES`.** The model has no other
   information channel; the system prompt forbids fabrication. The
   structural block is summary metadata, not content.

## Caller shape

A new feature module (alongside the RAG feature) exposes one method
roughly:

```
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

Internally:
1. Embed the question.
2. In parallel: vector-path query; graph-path query (both on `session`).
3. Union and dedup the row-policied results → `retrieval_set` (the
   broad set).
4. **`authorized_chunk_ids = {c.chunk_id for c in retrieval_set}`** —
   computed *here*, before rerank/truncate. These are exactly the
   chunks the user is allowed to see; STIX-logical chunk_ids (no
   `rag_embeddings` row) are filtered out here.
5. Rerank `retrieval_set` and truncate to `final_n` → `selected_chunks`.
6. Build the structural block from `kg_edges`, filtered against
   `authorized_chunk_ids` (not `selected_chunks`).
7. Render the prompt; call the LLM.
8. Parse citations → assemble `SynthesisResult`:
   `{answer, sources_cited, sources_unused, audit_record}`.

The session carries `currentRoles()`; the row policy enforces auth on
every `rag_embeddings` and `kg_*` read performed inside this function.

## What iris owns vs. what the operator owns

| Concern | Owner |
|---|---|
| Choice of synthesis LLM (model, vendor, API) | Operator (env config) |
| Choice of reranker model | Operator (env config) |
| The synthesis prompt template (this spec) | Iris |
| Pre-synthesis pipeline (dedup, rerank, truncate, structural-block build) | Iris |
| Citation parsing and audit-record construction | Iris |
| `SynthesisResult` shape returned to the UI | Iris |
| Persisting audit records (where, retention) | Operator decision; iris produces them |

## Non-goals

- No conversational memory / multi-turn synthesis in v1.
- No SSE streaming of the LLM output to the UI in v1; returns complete
  answer.
- No map-reduce synthesis. Over-budget chunk sets are truncated by rerank
  score, not re-summarized.
- **No community-summary block in v1.** Community detection is deferred
  in the KG spec; revisit once global-question demand is measured.
- No agentic re-querying based on the LLM's intermediate output.
- No automatic question rewriting / expansion before retrieval.

## Open questions

1. **Reranker model.** Worth running a cross-encoder before truncation,
   or does the unioned set already fit budget after vector+graph dedup?
   Benchmark with real data.
2. **Structural-block density.** 50 edges may waste tokens when the
   question needs only a handful. v1 applies a relevance filter (edges
   within the question-matched entity neighborhood); revisit after
   measuring.
3. **Retry-on-bad-citation vs strip-and-log.** Retry is cheap insurance
   but doubles latency on the unhappy path. v1 strips and logs; switch
   if measured citation-error rate is high.
4. **How to surface the audit record to the user.** Inline alongside the
   answer ("Sources used: …"), or only to admins via the Authorization
   feature? Pure UX choice; defer to UI design.
