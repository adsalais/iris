# CLAUDE.md refactor

Split the monolithic `CLAUDE.md` (~514 lines, 3 audiences crammed into one doc) into a thin navigator + three topic docs. The navigator stays the entry point an agent reads at task start. The topic docs hold the depth that's reference-only when you're touching that surface. Design rationale stays in the dated spec docs, with each topic doc closing with a small "Evolution" pointer.

## Why

`CLAUDE.md` currently does three jobs at once:

- **Agent reference** — what an LLM working in this repo needs to know to act safely (commands, conventions, key patterns).
- **Operator runbook** — env vars, deployment notes, security follow-ups.
- **Design rationale archive** — "why we did X", migration runbooks, deferred items, open risks.

These three jobs pull the doc in different directions. The agent-reference parts want to be short and action-oriented; the rationale parts accumulate as the codebase evolves (we've already added five spec-shaped sections inside CLAUDE.md across recent migrations); the operator parts want stable env-var tables. Five recent refactors each rewrote a few hundred lines of CLAUDE.md to keep it accurate — that rewriting churn is the symptom.

Splitting by audience makes each doc edit smaller and lets the entry-point doc stay terse:
- The agent reads CLAUDE.md to learn the project shape and conventions, follows links into a topic doc only when working on that surface.
- The operator reads `docs/operations.md` for deployment specifics.
- The contributor curious about "why is auth shaped this way" reads the topic doc's Evolution section, then opens the relevant dated spec.

## Scope

In:
- Rewrite `CLAUDE.md` as a ~180-line navigator.
- Create `docs/auth.md`, `docs/clickhouse.md`, `docs/operations.md` by extracting today's content from `CLAUDE.md`, removing redundancy, and adding an "Evolution" pointer section per doc.
- Add a new "Conventions" section to `CLAUDE.md` capturing patterns an agent must follow (currently scattered or implicit).
- Drop redundancy already in CLAUDE.md (`### Authorization (CH-derived rights)` and `### Authorization model` cover the same ground; `### Auth ↔ ClickHouse bridge` and `### Per-database admin tier` overlap).

Out:
- Code changes (none — pure docs reorg).
- Renaming files or restructuring `docs/superpowers/` (specs and plans live where they live).
- Translating older specs into one-page summaries (the Evolution sections only point; readers click through).
- Auto-generating any of the new docs from code (everything is hand-edited markdown).
- Splitting per-package docs further (`docs/auth/sessions.md`, etc.) — the three topic docs stay flat.

## Decisions

### Target shape of `CLAUDE.md`

Approximately 180 lines, structured for fast lookup:

```
# CLAUDE.md
<2-sentence project description>

## Project state                   (5 lines — kept verbatim from today)
## Commands                         (15 lines — tightened, drops verbose explanations)
  ### Lint & type-check
  ### Tests
## Architecture                     (3-4 paragraphs — points to docs/auth.md, docs/clickhouse.md)
## Conventions                      (NEW — patterns the agent must follow; ~30 lines)
## Module map                       (terse — top-level packages, one line each, links to topic docs)
## Env vars (quick reference)       (compact list of all env vars; full descriptions in docs/operations.md)
## See also                         (links to docs/, docs/superpowers/)
```

Headings stay scannable. No section exceeds ~30 lines. Each section answers "what should I do today"; the *why* lives elsewhere.

### `docs/auth.md`

The full auth surface. Today's `## Authentication` section moved here, edited for redundancy and structure. Contents:

1. Public surface (the import block from today's intro).
2. The seven alias deps as a single table — admission rule, return type, raises.
3. `AuthSession` class hierarchy — `AuthSession` base, `DatabaseSession`, `DatabaseAdminSession`, `DatabaseCreatorSession`, `AdminSession`. Code shape, what each adds.
4. Session data lifecycle — the per-request snapshot semantics, when to call `update_data`.
5. `Rights` derivation — at login, post-login hook, `derive_rights` walks `system.role_grants` + `system.grants`. (Brief; the implementation lives in `iris.clickhouse.rights`.)
6. Login flows — OAuth (PKCE, callback), LDAP/Mock (form, CSRF), logout. Reference to provider tests.
7. Tests setup — fixtures, mock provider, integration tier (Keycloak testcontainer).
8. **Evolution** — 5-line bullet list of dated specs that shaped this surface (see template below).

The `### Authorization model` block (which today appears as a one-paragraph summary AFTER the more-detailed `### Authorization (CH-derived rights)` block) is dropped — pure duplication.

### `docs/clickhouse.md`

The full CH surface. Today's `## ClickHouse` section moved here. Contents:

1. Public surface — drops the today's hand-typed import wall; links to `iris/clickhouse/__init__.py` (`__all__` is the source of truth).
2. Conventions — per-user/per-group role naming, row-policy naming, tier-role naming, idempotency.
3. DDL safety contract — `identifiers.py` validation + quoting rules.
4. Bridge with auth — `iris.auth.identity` Session subclasses carry CH methods that delegate to `iris.clickhouse.handle.*_impl` standalone async functions; one parameter per route.
5. Per-database admin tier — `<X>_DBADMIN/_DBWRITER/_DBREADER` roles, `create_database` lifecycle, `delete_database`, route examples for each tier.
6. Bootstrap — `bootstrap_admin(client, admin_user=, admin_group=)`, `iris_global_admin` sentinel, `CLICKHOUSE_ADMIN_USER` / `CLICKHOUSE_ADMIN_GROUP` env vars.
7. Row policies — `add_row_policy` emits restrictive + two wildcards (`iris_global_admin` and `<database>_DBADMIN`).
8. Tests setup — testcontainer, `prefix` fixture, what runs against real CH vs mocked.
9. **Evolution** — 5-line bullet list of dated specs.

The today's `### Auth ↔ ClickHouse bridge` and `### Per-database admin tier` sections collapse — currently they say similar things in different orders. The merged section in `docs/clickhouse.md` is shorter than either.

### `docs/operations.md`

Operator-facing concerns scattered through CLAUDE.md today. Contents:

1. **Deployment** — uvicorn factory mode, multi-worker setup, `--workers N` and the SQLite WAL story, cross-host filesystem requirements.
2. **`.env` handling** — what `python-dotenv` does at import, override behavior, file permissions.
3. **Open redirect protection** — `_safe_next` rules, where it's applied.
4. **Open security follow-ups** — the v1.1 list (rate-limiting behind a proxy, JWKS rotation, OIDC discovery latency, `derive_rights` query cost).
5. **Deferred items** — current `### Deferred (v1.1+)` lists from auth and clickhouse sections combined.
6. **Migration runbooks** — operator runbook for the recent CH-only-authz, session-as-handle, and bootstrap-rework migrations is already in their respective specs; a one-paragraph index here points to each.

This doc has no Evolution section — its content IS the operational state. Items get added/dropped as the deploy story changes.

### Conventions section in `CLAUDE.md` (new)

The most useful net-new content. Patterns an agent must follow that aren't obvious from reading code, ordered by edit-frequency:

```markdown
## Conventions

- **DDL safety**: external strings flow through `validate_identifier` + `quote_identifier` (`iris.clickhouse.identifiers`). Never f-string-concat raw user input into SQL. DML uses CH's `{name:Type}` placeholder syntax.
- **Pre-create-on-grant**: tier-grant helpers issue `CREATE ROLE IF NOT EXISTS <target>_USER` before granting. Required for username-enumeration defence; don't shortcut.
- **Session `data` is a per-request snapshot**: mutations don't auto-persist. Routes that want to write through call `await request.app.state.auth_session_store.update_data(session.id, session.data)`.
- **Session methods use top-level imports of `iris.clickhouse.handle.*_impl`**: lazy method-body imports were a workaround for a now-removed cycle. Don't regress.
- **One parameter per route**: `session: SessionRead` / `SessionDatabaseAdmin` / etc. carry both admission and capability. Don't pair an alias with a separate handle dep — the handle classes are gone.
- **Refactor pattern**: spec → plan → atomic commit. Big renames go through a deliberate breakage window with one big-bang commit at the end. Don't try to incrementally split refactors that need to be atomic.
- **Tests don't mock the database**: `tests/clickhouse/` uses a real CH testcontainer (session-scoped). Per-test isolation is the `prefix` fixture (UUID-prefixed entity names).
```

This section is the most likely to actually shape future edits and is the only entirely-new prose in the refactor.

### Cross-referencing rules

- `CLAUDE.md` → topic docs: explicit links in the module map and a "See also" section at the bottom.
- Topic docs → specs: the Evolution section at the end of each topic doc.
- Topic docs ← `CLAUDE.md`: each topic doc's intro paragraph mentions "see CLAUDE.md for the project overview".
- Specs → topic docs: **no**. Specs are immutable historical records and shouldn't link forward to documents that will change. (They already link to other dated specs in the same directory, which is fine because those are also frozen.)
- Topic docs → other topic docs: minimal, only when behavior crosses surfaces (e.g., `docs/auth.md`'s Rights section briefly notes `derive_rights` lives in `iris.clickhouse.rights` and links to `docs/clickhouse.md`).

### Evolution section template

Every topic doc closes with this format:

```markdown
## Evolution

The current shape of this surface results from the following design rounds; the dated specs are the authoritative rationale.

- **YYYY-MM-DD** — one-line summary → `docs/superpowers/specs/<spec>.md`
- **YYYY-MM-DD** — one-line summary → `docs/superpowers/specs/<spec>.md`
```

For `docs/auth.md`:

- 2026-05-03 — initial auth scaffold + mock provider → `docs/superpowers/specs/2026-05-03-auth-design.md`
- 2026-05-03 — SQLite role mapping subsystem (later removed) → `2026-05-03-roles-authz-design.md`
- 2026-05-04 — session API simplification → `2026-05-04-session-api-simplification-design.md`
- 2026-05-05 — auth integration tests via Keycloak testcontainer → `2026-05-05-auth-testcontainers-design.md`
- 2026-05-06 — SQLite session store (replaces in-memory) → `2026-05-06-sqlite-session-store-design.md`
- 2026-05-06 — authz moved to SQLite (later removed) → `2026-05-06-authz-sqlite-design.md`
- 2026-05-08 — CH-only authorization (drops SQLite role mapping) → `2026-05-08-clickhouse-only-authz-design.md`
- 2026-05-08 — session-as-handle: one parameter per route → `2026-05-08-session-as-handle-design.md`

For `docs/clickhouse.md`:

- 2026-05-05 — CH RBAC primitives (users, roles, grants, row policies) → `2026-05-05-clickhouse-authz-design.md`
- 2026-05-06 — auth↔CH bridge: handles + post-login provisioning → `2026-05-06-auth-clickhouse-bridge-design.md`
- 2026-05-06 — per-database admin tier (initially SQLite-backed) → `2026-05-06-clickhouse-database-admin-design.md`
- 2026-05-08 — CH-only authorization, tier roles in CH → `2026-05-08-clickhouse-only-authz-design.md`
- 2026-05-08 — session-as-handle: handle classes removed → `2026-05-08-session-as-handle-design.md`
- 2026-05-08 — bootstrap rework + iris_global_admin sentinel → `2026-05-08-bootstrap-rework-design.md`

`docs/operations.md` has no Evolution — its content IS the current operational state. Operators don't need migration archaeology.

### What CLAUDE.md loses (moved to topic docs)

After the topic docs absorb the moved sections, the following sub-sections leave CLAUDE.md (they're either redundant within today's CLAUDE.md, fold into a topic doc, or fold into operations.md):

- `### Authorization model` (one-paragraph summary that duplicates `### Authorization (CH-derived rights)`)
- `### Login flows` (moves to `docs/auth.md`)
- `### Tests` x2 (one in auth, one in clickhouse — both move to topic docs)
- `### Integration tests (tests/auth/integration/)` (moves to `docs/auth.md`)
- `### Open redirect protection` (moves to `docs/operations.md`)
- `### Open security follow-ups (v1.1)` (moves to `docs/operations.md`)
- `### Multi-worker deployment` (moves to `docs/operations.md`)
- `### DDL safety` (full text moves to `docs/clickhouse.md`; one-line reminder kept under Conventions in CLAUDE.md)
- `### Module map` x2 (collapsed into the new terse Module map at CLAUDE.md top, with detail in topic docs)
- `### Conventions` from today's clickhouse section (full text moves to `docs/clickhouse.md`; the agent-relevant patterns get hoisted into CLAUDE.md's new Conventions section)
- `### Configuration` x2 (env-var tables go to `docs/operations.md`; CLAUDE.md keeps a one-table quick reference)
- `### Per-database admin tier` (moves to `docs/clickhouse.md`)
- `### Auth ↔ ClickHouse bridge` (moves to `docs/clickhouse.md`)
- `### Per-session server-side data` (moves to `docs/auth.md`; the snapshot semantics is in the new Conventions section)
- `### Authorization (CH-derived rights)` (moves to `docs/auth.md`)
- `### Deferred (v1.1+)` (moves to `docs/operations.md`)
- `### SDK gotchas (already worked around in app.py)` (moves to `docs/clickhouse.md` if Datastar-related is kept; actually it's Datastar-specific, stays in CLAUDE.md's Architecture section since it directly affects route writing)
- `### Datastar attribute cheatsheet` (kept in CLAUDE.md's Architecture section — it's a quick-reference table, exactly the kind of thing CLAUDE.md is good for)

### File layout after the refactor

```
CLAUDE.md                                  # ~180 lines, navigator + conventions
docs/
├── auth.md                                # full auth surface, ~250 lines
├── clickhouse.md                          # full CH surface, ~200 lines
├── operations.md                          # deployment + security follow-ups, ~120 lines
└── superpowers/
    ├── specs/
    │   └── *.md                           # unchanged — dated rationale
    └── plans/
        └── *.md                           # unchanged
```

## Migration / rollout

Single PR, mechanical:

1. Create `docs/auth.md` by cutting today's `## Authentication` section, editing for redundancy (drop the duplicate `Authorization model` block), adding the Evolution section.
2. Create `docs/clickhouse.md` by cutting today's `## ClickHouse` section, dropping the import-wall, merging the bridge + per-DB-admin sections, adding the Evolution section.
3. Create `docs/operations.md` by collecting operational content from both auth + clickhouse sections (multi-worker, security follow-ups, deferred items, env-var depth).
4. Rewrite `CLAUDE.md` to the shape in `### Target shape of CLAUDE.md` above. Keep Datastar Architecture and Datastar attribute cheatsheet (they're route-writing reference, not auth/CH-specific).
5. Add `## Conventions` section in `CLAUDE.md` with the bullet list above.
6. Smoke check: read CLAUDE.md and verify each section answers a question an agent might ask. Try one trace ("how do I add a route gated on database admin?") and verify the agent finds it via CLAUDE.md → docs/clickhouse.md.

No code changes, no test changes. Single docs commit.

## Open risks

- **Documentation drift in two places**: when the codebase changes, both CLAUDE.md (for the convention/quick-reference parts) and the topic doc (for the depth) need to be updated. Mitigation: the Conventions section in CLAUDE.md captures patterns at a coarser grain than the topic docs, so most edits touch only one. When both must change, the PR title makes it obvious.
- **Evolution sections drift**: when a new spec lands, someone must add a line to the topic doc's Evolution section. Forgetting it means the doc lists stale rationale. Mitigation: include "update topic-doc Evolution" as a step in future spec→plan→implementation cycles. The five-line cost is small.
- **`docs/operations.md` has no Evolution by design** — operators don't read migration archaeology. But that means it has no link-out to historical context. If an operator needs to understand "why is the JWKS-rotation caveat here" they have to find the auth or auth-testcontainers spec themselves. Acceptable; document the trade-off in the doc itself.
- **The "See also" section at CLAUDE.md's bottom is the only path into the docs/** hierarchy for a new contributor opening the repo. If we delete or rename CLAUDE.md, the docs/ tree becomes orphaned. Mitigation: keep CLAUDE.md as the entry point; if Claude Code conventions change to expect a different filename, port the doc.
