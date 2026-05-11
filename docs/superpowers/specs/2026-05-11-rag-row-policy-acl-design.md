# RAG with row-dict-policy ACLs — high-level spec

**Status:** design only — no implementation yet.
**Date:** 2026-05-11

## Goal

Run vector-similarity RAG over a large dataset stored in ClickHouse, where each
query returns only the embeddings the calling iris user is authorized to see.
Authorization is enforced **inside ClickHouse** via a row policy that evaluates
against a per-document allow-list of iris tier roles — no application-side
filtering.

## Data model

Three ClickHouse objects per RAG dataset, all colocated in the same database
(e.g. `rag_docs`):

### 1. `rag_embeddings` — one row per chunk

| Column | Type | Notes |
|---|---|---|
| `doc_id` | `String` | Parent document. Chunks of the same document share this. Used for grouping / re-ranking / display, **not** for auth. |
| `auth_id` | `String` | Authorization key. References `rag_acl.auth_id`. Many `doc_id`s can map to the same `auth_id`. |
| `embedding` | `Array(Float32)` | Vector. Optional ANN index. |
| `content` | `String` | The chunk text. |
| … | | Loader-specific metadata as needed. |

### 2. `rag_acl` — source of truth, owned by the ingestion pipeline

| Column | Type | Notes |
|---|---|---|
| `auth_id` | `String` | Key. |
| `allowed_roles` | `Array(String)` | iris tier-role names. Empty array ⇒ no one ⇒ deny-by-default. |

**Invariant the ingestion pipeline must uphold:** every element of
`allowed_roles` ends in `_USER` or `_GRP`. Other suffixes (`_DBADMIN`,
`_DBREADER`, `_DBWRITER`) are rejected by convention — they would confuse the
policy semantics with iris's broader tier grants. Iris does not validate this
because iris does not own `rag_acl`; call it out in operator runbooks.

### 3. `rag_acl_dict` — `CREATE DICTIONARY` over `rag_acl`

- Layout: `COMPLEX_KEY_HASHED` (String key).
- Key: `auth_id`. Attribute: `allowed_roles Array(String)`.
- `LIFETIME(MIN 3000 MAX 3600)` — refresh window centered around 1 hour.
  Worst-case revocation lag ≈ 1h.
- Dict miss (an `auth_id` not present in `rag_acl`) returns the type default
  `[]`, which means deny. Consistent with deny-by-default.

## Enforcement

A single row policy on `rag_embeddings`, attached to every tier role that may
read the database:

```sql
USING arrayExists(r -> has(
  dictGet('rag_docs.rag_acl_dict', 'allowed_roles', auth_id),
  r
), currentRoles())
```

Plain English: "this row is visible iff at least one of the caller's current
roles appears in the document's `allowed_roles`."

Iris already runs user queries through `query_as_user(...)` on a
`DatabaseSession`, which sets `currentRoles()` to the user's tier roles for the
lifetime of the CH session. Because `currentRoles()` contains the user's
per-database `*_USER` and their `*_GRP` roles for each IdP group they belong
to, the policy expresses both "users granted DB access individually" and
"anyone in group X" with the same machinery.

Install the policy via `iris.clickhouse.policies.add_row_dict_policy(...)` — no
new authorization machinery needed.

## Required grants (operator follow-up)

Every role attached to the policy needs `GRANT dictGet ON rag_docs.rag_acl_dict`.
Without it CH raises `Code: 497` server-side and the policy fails closed — the
user sees zero rows. This is the same operator concern iris already documents
for any dict-keyed policy; the RAG feature should reuse the existing admin-UI
warning when that lands.

## Query path

1. Embed the user's question (model call — outside ClickHouse).
2. On the user's `DatabaseSession` (so `currentRoles()` is correct), run an ANN
   query against `rag_embeddings` with a `LIMIT k'` chosen larger than the
   desired `k`.
3. CH evaluates the row policy per candidate row; unauthorized rows are
   silently dropped.
4. Take the top `k` of what's returned and hand the content to the model.

**Over-fetching note:** ANN indexes return top-K *before* row-policy filtering.
If a user has access to a small subset of the corpus, the engine may need to
scan deeper to surface enough authorized neighbors. Start with `k' = 2k` and
tune; if a user's allowed slice is tiny, consider a fallback to exhaustive scan
(no ANN) on that user's behalf — measured, not preemptive.

## What iris owns vs. what the operator owns

| Concern | Owner |
|---|---|
| Provisioning `rag_acl_dict` and its `LIFETIME` | Operator (one-time `CREATE DICTIONARY`) |
| Populating / updating `rag_acl` | Ingestion pipeline (out of scope for iris) |
| Enforcing the `_USER` / `_GRP` suffix invariant on `allowed_roles` | Ingestion pipeline |
| Granting `dictGet` on the dict to each tier role | Iris (extend tier-grant helper or admin UI) |
| Attaching the row policy on `rag_embeddings` | Iris, via the Authorization feature's create/update database flow |
| Issuing RAG queries on the user's session | A new feature module (e.g. `src/iris/features/rag/`) |

## Non-goals

- No embedding-generation pipeline (assume an external loader writes
  `rag_embeddings` + `rag_acl`).
- No multi-tenant cross-database RAG; one RAG dataset = one CH database,
  reusing iris's per-database tier model.
- No re-ranking, no hybrid keyword+vector — pure ANN with ACL filtering. Layer
  those later.
- No `*_DBADMIN` / `*_DBREADER` / `*_DBWRITER` membership in `allowed_roles`
  (see invariant above).

## Open questions

1. **ANN index choice + `k'` heuristic.** Needs a small benchmark on
   representative data; defer until a dataset exists.
2. **Admin-UI surfacing of missing `dictGet` grants.** Already an open iris
   operator follow-up (see `CLAUDE.md` → "Operator follow-ups"). The RAG
   feature should consume that warning once it lands; the spec does not
   re-design it.
