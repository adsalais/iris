# RAG phase 1 — vector RAG with row-policy ACL — high-level spec

**Status:** design only.
**Date:** 2026-05-11.
**Series:**
- **Phase 1 (this spec)** — vector RAG with row-policy ACL.
- Phase 2 (`2026-05-11-rag-phase-2-ingestion-design.md`) — data ingestion pipeline.
- Phase 3 (`2026-05-11-rag-phase-3-knowledge-graph-design.md`) — knowledge graph extension.
- Phase 4 (`2026-05-11-rag-phase-4-stix-vocab-and-bootstrap-design.md`) — STIX vocabulary + bootstrap.

## Goal

The smallest end-to-end RAG system that:

- Runs vector-similarity search over ClickHouse.
- Enforces per-chunk authorization via a row policy on `rag_embeddings`,
  keyed by an `auth_id` column resolved against an operator-curated
  `rag_acl` table via a CH dictionary.
- Synthesizes a cited answer from the retrieved chunks via a single LLM
  call.
- Ships with a `.rag_env`-driven test fixture that skips RAG tests
  cleanly when the required external resources aren't configured.

No knowledge graph, no STIX, no automated ingestion pipeline in this
phase. Phase 1 assumes chunks land in `rag_embeddings` by some manual
process (a script, manual `INSERT`s); phase 2 specifies the ingestion
path properly.

## Scope

In scope:
1. ClickHouse storage layout: `rag_embeddings`, `rag_acl`, `rag_acl_dict`.
2. Row policy on `rag_embeddings` keyed by `auth_id`.
3. `tlp` column on `rag_embeddings` (informational metadata, not auth).
4. Required grants for the `dictGet` policy invocation.
5. Vector-only synthesis: pre-synthesis pipeline (rerank + truncate),
   prompt template, citation enforcement, refusal/uncertainty handling.
6. `.rag_env` test configuration with skip-if-missing semantics.
7. Feature module shape (one route, one service method).

Out of scope:
- Data ingestion pipeline (phase 2).
- KG extraction / structural context block in the prompt (phase 3).
- STIX bootstrap / vocabulary (phase 4).
- Streaming answers, conversational memory, agentic loops.

## Storage layout

Three ClickHouse objects per RAG dataset, all colocated in one CH
database (e.g. `rag_docs`).

### `rag_embeddings` — one row per chunk

| Column | Type | Notes |
|---|---|---|
| `doc_id` | `UUID` | Parent document. Many chunks share. Used for grouping / display, **not** for auth. |
| `chunk_id` | `UUID` | `uuid5(NS_CHUNK, f"{doc_id}::{content_hash}")` — `content_hash = sha256(chunk_text)`. Phase 2 extends to `uuid5(NS_CHUNK, f"{doc_id}::{ordinal}::{content_hash}")` so two chunks with identical content but different positions stay distinct; Phase 1 has no ordinal because there's no automated chunking yet. Phase-4 STIX-derived chunks also use `uuid5` (`uuid5(NS_CHUNK, f"stix:{stix_id}:description")`), so this column is uniformly `UUID` — no mixing with non-UUID strings. `NS_CHUNK` is fixed once at deployment and never rotated. |
| `auth_id` | `String` | Authorization key. References `rag_acl.auth_id`. |
| `tlp` | `Enum8('clear' = 1, 'green' = 2, 'amber' = 3, 'amber_strict' = 4, 'red' = 5)` DEFAULT `'clear'` | Informational TLP marker for analyst awareness. **Not used for authorization.** UI surfaces it next to retrieved chunks so analysts know dissemination constraints when sharing. |
| `embedding` | `Array(Float32)` | Vector. Has an HNSW ANN index — see "Vector indexes" below. |
| `content` | `String` | Chunk text. |
| `source_uri` | `String` | Original document URI. |
| `ingested_at` | `DateTime` | Wall-clock UTC. |
| … | | Loader-specific metadata (page, section, language, etc.). |

Engine:
```sql
ENGINE = MergeTree
PARTITION BY toYYYYMM(ingested_at)
ORDER BY (doc_id, chunk_id)
```

Partitioning by ingest-month makes retention drops cheap (`ALTER TABLE
DROP PARTITION`) and gives query pruning for time-bounded analyst
queries. ORDER BY `(doc_id, chunk_id)` clusters chunks of the same
document together — both compresses better (UUID prefix gets repeated
many times per granule) and accelerates `redocument(doc_id)` deletes.

### Vector index on `embedding`

```sql
ALTER TABLE rag_embeddings ADD INDEX embedding_hnsw embedding
TYPE vector_similarity('hnsw', 'cosineDistance', ${RAG_EMBEDDING_VECTOR_SIZE})
GRANULARITY 1
```

The dimension is read from `RAG_EMBEDDING_VECTOR_SIZE` in `.rag_env`
at table-creation time. Common values: 768 (BGE-base), 1024
(Voyage-3, Cohere-v3), 1536 (OpenAI `text-embedding-3-small`), 3072
(OpenAI `text-embedding-3-large`). The index lets ANN queries (`ORDER
BY cosineDistance(embedding, $q) LIMIT k`) prune granules instead of
scanning. Falls back to brute force if disabled or unavailable in
the CH version. ClickHouse's vector-similarity index is in active
development; pin the CH version supported by ops and document a
fallback path (`SETTINGS allow_experimental_vector_similarity_index = 1`
where required).

**Changing the embedding model post-deployment** requires re-embedding
every chunk AND rebuilding the index with a new dimension. Iris's
DDL helper refuses to ALTER the dimension on an existing table; the
migration path is "create rag_embeddings_v2 with the new
RAG_EMBEDDING_VECTOR_SIZE, re-embed into it, atomically swap via a
view". Out of v1 scope but flagged here so the constraint is visible.

**`tlp` is metadata, not access control.** Authorization is enforced
exclusively by `auth_id` + `rag_acl` + the row policy. Operators may
*choose* to encode TLP intent into `auth_id` naming (e.g.,
`auth_id = "tlp:amber"` with a matching `rag_acl` row), but that's an
organizational convention, not an iris-enforced linkage.

### `rag_acl` — operator-curated authorization table

| Column | Type | Notes |
|---|---|---|
| `auth_id` | `String` | Key. |
| `allowed_roles` | `Array(String)` | iris tier-role names. Empty array ⇒ no one ⇒ deny. |

**Invariant the operator must uphold:** every element of `allowed_roles`
ends in `_USER` or `_GRP`. Other tier suffixes (`_DBADMIN`, `_DBREADER`,
`_DBWRITER`) are rejected by convention — admin tiers are not row-policy
audiences. Iris does not validate (it doesn't own `rag_acl`); call it
out in operator runbooks.

### `rag_acl_dict` — `CREATE DICTIONARY` over `rag_acl`

- Layout: `COMPLEX_KEY_HASHED` (String key).
- Key: `auth_id`. Attribute: `allowed_roles Array(String)`.
- `LIFETIME(MIN 3000 MAX 3600)` — refresh window ≈ 1 hour, the
  worst-case revocation lag.
- Dict miss returns `[]` ⇒ deny. Deny-by-default.

### Row policy on `rag_embeddings`

Attached to **the user-facing tier roles only** — `*_USER` and `*_GRP`.
Admin tiers (`*_DBADMIN`, `*_DBWRITER`, `*_DBREADER`) are not in the
policy's `TO …` list and remain unrestricted at the table level, which
is how iris admin paths read across all rows.

```sql
USING arrayExists(r -> has(
  dictGet('rag_docs.rag_acl_dict', 'allowed_roles', auth_id),
  r
), currentRoles())
```

Plain English: "this row is visible iff at least one of the caller's
current roles appears in the document's `allowed_roles`."

Iris already runs user queries through `query_as_user(...)` on a
`DatabaseSession`, which sets `currentRoles()` to the user's tier roles.
The policy expresses both "users granted DB access individually" and
"anyone in group X" with the same machinery.

Install via `iris.clickhouse.policies.add_row_dict_policy(...)`.

### Required grants

Every role attached to the policy needs `GRANT dictGet ON rag_docs.rag_acl_dict`.
Without it CH raises `Code: 497` server-side and the policy fails closed
— the user sees zero rows. Iris's create-database flow installs both
the dict grant and the row policy.

### Row policies apply to SELECTs only

ClickHouse row policies gate `SELECT` only — `INSERT`, `UPDATE`, and
`ALTER` succeed regardless of policy membership. Write access is
controlled separately by table-level `GRANT INSERT`. This is correct
for the design: the ingestion pipeline (phase 2) writes whatever
chunks it's told to ingest with the correct `auth_id`; the row policy
then determines who can read them. Don't add defensive insert-side
checks expecting the row policy to gate writes — it doesn't.

## Synthesis (vector-only)

A single LLM call grounds a cited answer in the retrieved chunks.

### Pre-synthesis pipeline

```
question
   |
   v
embed
   |
   v
top-K ANN over rag_embeddings (LIMIT k')   <- row policy filters here
   |
   v
cross-encoder rerank vs question  (optional)
   |
   v
truncate to top-N  -> selected_chunks
   |
   v
construct prompt
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

**Defaults.** `k' = 24`, `final_n = 12`, token budget reserves ~70% of
the model's context for SOURCES.

**Over-fetching note.** ANN indexes return top-K *before* row-policy
filtering. If a user has access to a small subset of the corpus, the
engine may scan deeper to surface enough authorized neighbors. Start
`k' = 2 × final_n` and tune; for users with tiny allowed slices,
consider falling back to exhaustive scan (no ANN).

### Prompt template

```
[SYSTEM]
You are a research assistant. Answer the user's question using ONLY the
sources provided below. Every factual claim must cite the source(s)
that support it using inline references of the form [C<n>], where <n>
is the source number. If the provided sources don't support an answer,
say so explicitly — do not fabricate.

Output format:
1. A direct answer (a few sentences to a few paragraphs).
2. A "Sources" trailer listing only the [C<n>] references you actually
   cited, with their doc_id.

[SOURCES]
[C1] doc_id=<doc_id>, chunk_id=<chunk_id>, source=<source_uri>, tlp=<tlp>, page=<page>, section="<section_path[0]>", score=0.91
<chunk content verbatim>

[C2] doc_id=<doc_id>, chunk_id=<chunk_id>, source=<source_uri>, tlp=<tlp>, page=<page>, section="<section_path[0]>", score=0.84
<chunk content verbatim>

...

[QUESTION]
<the user's question>
```

`QUESTION` is placed last so it stays in the recency window even at
long context. SOURCES are ordered by rerank score (highest first).

The widened source header travels with citations into analyst notes —
`tlp` in particular is load-bearing for DFIR write-ups (TLP:AMBER
content must not be quoted in a TLP:WHITE report). Fields whose value
is empty or missing render as `(none)` rather than being omitted, so
the positional structure stays parseable.

### Citation enforcement

- Inline `[C<n>]` references are required by the system prompt.
- Post-processing parses the model output, extracts cited `[C<n>]`
  tokens, and constructs an audit record:
  `(question, sources_provided, sources_cited, answer, model, prompt_version)`.
- Citation hygiene: every emitted `[C<n>]` must match an `n` in
  `SOURCES`. Bogus citations are stripped + logged (v1 default); retry
  is a follow-on.

### Refusal / uncertainty

If retrieval surfaces fewer than `M` chunks (`M = 2` in v1), the prompt
prepends:

```
[NOTE] Few sources were retrieved. If they don't substantively answer
the question, say so directly — do not stretch them.
```

The system prompt's "do not fabricate" rule does the heavy lifting.

### Authorization invariants

1. Every chunk in `SOURCES` has already passed the row policy on
   `rag_embeddings`. Synthesis never re-evaluates authz.
2. No content from outside `SOURCES`. The model has no other
   information channel; the system prompt forbids fabrication.

## Feature module shape

A new `src/iris/features/rag/`:

- `install.py` — registers nav entry; checks the user has read
  capability on at least one database carrying `rag_embeddings`.
- `routes.py` — one POST route `/feature/rag/ask` accepting
  `{question: str, database: str}`, returning
  `{answer, sources_cited, sources_unused, audit_record}`. The
  handler resolves `database` to a `DatabaseSession` via iris's
  existing per-database session machinery (the same path the
  Authorization feature uses for its database-scoped routes — see
  `iris.auth.views.DatabaseSession`); if the user has no admission
  to that database, the session-resolver raises and the route
  returns 403 before `synthesize()` is reached.
- `service.py` — `synthesize(session, question, *, vector_k=24, final_n=12)`;
  runs on a `DatabaseSession` so `currentRoles()` is correct.
- `templates/rag/answer.html` — Datastar template rendering answer +
  cited sources. Streaming is out of v1 (see Non-goals); the route
  returns the full JSON payload and the shell re-renders the answer
  panel as a single SSE `datastar-patch-elements` event.

## Test environment (`.rag_env`)

The phase-1 test suite needs four external resources:

- **A ClickHouse instance to write/read** — reuses iris's standard
  CH env vars (`CLICKHOUSE_HOST`, `_PORT`, `_USER`, `_PASSWORD`,
  `_SECURE`, `_VERIFY`, `_CA_CERT_PATH`, per `CLAUDE.md`). These live
  in iris's main `.env`, not in `.rag_env` — the RAG feature shares
  the connection like every other iris subsystem.
- **An embedding model** (URL + model name + API key).
- **A synthesis LLM** (URL + model name + API key).
- **A reranker** (optional; URL + model name + API key).

The three model-provider configs live in a `.rag_env` file at the
repo root (sibling to `.env`). The file is **never committed** —
it's in `.gitignore` from day one.

**`.rag_env` is iris's RAG config source for BOTH runtime and tests**
— a single file, loaded by:

- The iris service at startup (alongside `.env`), so production
  reads the same vars.
- The pytest `rag_env` fixture (described below) for the test suite.

This keeps "where do RAG keys live?" answerable in one place.
Operators who don't want a file on disk (CI, containers) set the vars
directly in the environment and pass `RAG_SKIP_DOTENV=1` to bypass the
file lookup, exactly the same pattern as iris's existing
`IRIS_SKIP_DOTENV=1`.

### File layout

```
# .rag_env
# ClickHouse connection: NOT here -- iris's standard env vars are
# reused (CLICKHOUSE_HOST / _PORT / _USER / _PASSWORD / _SECURE /
# _VERIFY / _CA_CERT_PATH, per CLAUDE.md). The RAG feature picks the
# CH database to use per-request via the route's `database` field;
# there's no global RAG_CLICKHOUSE_DATABASE.

# --- Embedding model (OpenAI-compatible /v1/embeddings) ---
RAG_EMBEDDING_URL=https://api.openai.com/v1
RAG_EMBEDDING_MODEL=text-embedding-3-large
RAG_EMBEDDING_API_KEY=sk-...
RAG_EMBEDDING_VECTOR_SIZE=3072   # 768 BGE / 1024 Voyage / 1536 OAI-small / 3072 OAI-large

# --- Synthesis LLM (OpenAI-compatible /v1/chat/completions) ---
RAG_LLM_URL=https://api.openai.com/v1
RAG_LLM_MODEL=gpt-4o
RAG_LLM_API_KEY=sk-...

# --- Reranker (optional; Voyage/Cohere-style /v1/rerank) ---
RAG_RERANKER_URL=https://api.voyageai.com/v1
RAG_RERANKER_MODEL=rerank-2
RAG_RERANKER_API_KEY=
```

### Talking to providers: one HTTP client, three endpoint shapes

Iris does NOT carry per-vendor SDK adapters. All three external
services speak HTTP with one of three well-known JSON shapes; the
runtime uses a single `httpx`-based client.

- **Embeddings.** OpenAI-compatible `POST <URL>/embeddings` with
  `{"model": ..., "input": [...]}` returning `{"data": [{"embedding": [...]}, ...]}`.
- **LLM.** OpenAI-compatible `POST <URL>/chat/completions` with
  `{"model": ..., "messages": [...]}` returning the standard
  completions envelope.
- **Reranker.** Voyage/Cohere/Mixedbread/Jina-style
  `POST <URL>/rerank` with `{"model": ..., "query": str, "documents": [str, ...], "top_k": int}`
  returning `{"results": [{"index": int, "relevance_score": float}, ...]}`.

Any service that speaks one of these shapes at the configured URL
works. The two operator-supportable defaults are:

- **OpenAI directly** — `RAG_LLM_URL=https://api.openai.com/v1`,
  `RAG_EMBEDDING_URL=https://api.openai.com/v1`. OpenAI's own models
  for both. Works out of the box.
- **OpenRouter** — `RAG_LLM_URL=https://openrouter.ai/api/v1`,
  same shape (OpenRouter IS OpenAI-compatible — the `/chat/completions`
  endpoint accepts the standard payload and adds optional fields
  iris ignores). Use this when you want to pick a non-OpenAI LLM
  (Claude, Gemini, Llama) under one API key without standing up a
  proxy.

OpenAI vs. OpenRouter is **not** an architectural distinction for
iris — both flow through the same single HTTP client. The choice is
billing/sourcing convenience.

Other compatible endpoints (Voyage for embeddings + rerank, Cohere
for rerank, Together, Groq, a local LiteLLM proxy, vLLM/Ollama for
self-hosted models) all work the same way: pick the URL, pick the
model name, set the API key. Authentication is `Authorization:
Bearer <RAG_*_API_KEY>` for all three.

If a target service doesn't speak one of the three shapes natively
(e.g., Anthropic's `/v1/messages` differs from OpenAI's
`/v1/chat/completions`), front it with OpenRouter or a LiteLLM proxy
— that's iris's recommended path for non-OpenAI-native models.

A reranker entry with `RAG_RERANKER_API_KEY` empty is treated as
"reranker URL configured but no key supplied" → the rerank step is
skipped entirely (the fixture exposes `rag_env.reranker is None`).
This lets a developer enable the reranker selectively per test
without editing the file.

### Test fixture

A session-scoped `rag_env` fixture (`tests/conftest.py` or
`tests/rag/conftest.py`):

- If `.rag_env` is missing, `pytest.skip("…")` the entire RAG test
  module.
- If `.rag_env` exists but a required var is empty/missing,
  `pytest.skip(f"…missing: {names}")`.
- Otherwise return a typed `RagEnv` dataclass with the parsed values.

Required vars (must all be present for tests to run):

- `CLICKHOUSE_HOST` — iris's standard CH var, **not** a RAG-specific
  one (other `CLICKHOUSE_*` vars get sensible defaults; the same set
  iris uses everywhere else).
- `RAG_EMBEDDING_URL`, `RAG_EMBEDDING_MODEL`, `RAG_EMBEDDING_API_KEY`, `RAG_EMBEDDING_VECTOR_SIZE`. The vector size is needed at table-creation time (the HNSW index dimension is fixed once the table exists); changing the model later requires re-embedding + a new table.
- `RAG_LLM_URL`, `RAG_LLM_MODEL`, `RAG_LLM_API_KEY`.

Reranker vars (`RAG_RERANKER_URL`, `RAG_RERANKER_MODEL`,
`RAG_RERANKER_API_KEY`) are optional — tests that need a reranker
check `rag_env.reranker is not None` and skip individually otherwise.

### Dotenv interaction

Iris already gates `.env` loading via `IRIS_SKIP_DOTENV=1` for hermetic
tests (see `CLAUDE.md`). `.rag_env` follows the same pattern: a
`RAG_SKIP_DOTENV=1` env var bypasses the file lookup so CI can inject
the vars directly without a file on disk.

### Why skip-not-fail

Phase-1 RAG tests need external network / paid API access. Failing the
whole iris suite when those aren't configured would block contributors
who aren't working on RAG. Skipping with a clear reason
("`.rag_env` not configured; run `cp .rag_env.example .rag_env` and fill
in to enable RAG tests") keeps the suite hermetic-by-default.

A `.rag_env.example` template (committed) shows the expected keys with
placeholder values.

## What iris owns vs. what the operator owns

| Concern | Owner |
|---|---|
| `rag_embeddings` / `rag_acl` / `rag_acl_dict` schema (DDL helpers) | Iris (extend Authorization feature's create-database flow) |
| Attaching the row policy on `rag_embeddings` | Iris |
| Granting `dictGet` on `rag_acl_dict` to each tier role | Iris |
| Synthesis service + route + audit | Iris |
| `.rag_env` parsing + test fixture | Iris |
| Provisioning the CH database itself | Operator |
| Populating `rag_acl` rows | Operator |
| Inserting chunks into `rag_embeddings` (phase 1: manual; phase 2: pipeline) | Operator |
| Choosing embedding / LLM / reranker endpoint URLs, model names, and API keys | Operator (via `.rag_env`) |
| Running a LiteLLM / OpenRouter / similar proxy if the target backend isn't natively OpenAI-compatible | Operator |

## Non-goals

- No structural-context block in the prompt (phase 3 adds it).
- No KG, no STIX, no community summaries.
- No streaming / conversational memory in v1.
- No agentic re-querying based on the LLM's intermediate output.
- No automatic question rewriting / expansion before retrieval.

## Open questions

1. **ANN index choice + `k'` heuristic.** Benchmark on representative
   data; defer until a dataset exists.
2. **Admin-UI surfacing of missing `dictGet` grants.** Already an open
   iris operator follow-up (see `CLAUDE.md` → "Operator follow-ups").
   The RAG feature should consume that warning once it lands.
3. **Reranker model.** Worth running a cross-encoder before truncation,
   or does the row-policied top-K already fit budget? Measure with real
   data before committing.
4. **Retry-on-bad-citation vs strip-and-log.** v1 strips and logs;
   switch if measured citation-error rate is high.
