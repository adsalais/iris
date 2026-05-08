# CLAUDE.md Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split today's ~514-line `CLAUDE.md` into a thin navigator (~180 lines) plus three topic docs (`docs/auth.md`, `docs/clickhouse.md`, `docs/operations.md`), each topic doc closing with an Evolution pointer to the dated specs.

**Architecture:** Pure docs reorg, no code changes. Three new files extracted from today's CLAUDE.md sections, with redundancy dropped (the `Authorization model` paragraph that duplicates `Authorization (CH-derived rights)`; the overlap between `Auth ↔ ClickHouse bridge` and `Per-database admin tier`). CLAUDE.md gets rewritten last with a new `## Conventions` section that captures patterns an agent must follow.

**Tech Stack:** Markdown only. No tests. Verification is reading-test ("can an agent answer common questions via CLAUDE.md → topic docs?").

**Spec:** `docs/superpowers/specs/2026-05-08-claude-md-refactor-design.md`

---

## File Structure

### New files

- `docs/auth.md` — full auth surface (~250 lines). Today's `## Authentication` section moved here.
- `docs/clickhouse.md` — full CH surface (~200 lines). Today's `## ClickHouse` section moved here, with `### Auth ↔ ClickHouse bridge` and `### Per-database admin tier` merged.
- `docs/operations.md` — operator-facing concerns (~120 lines). Multi-worker deployment, security follow-ups, deferred items, env-var depth.

### Modified files

- `CLAUDE.md` — rewritten as a navigator (~180 lines). Keeps `## Project state`, `## Commands`, the Datastar parts of `## Architecture`, and gains a new `## Conventions` section. Loses the auth/clickhouse/operations content.

### Order of operations

Tasks 1-3 create the three topic docs (each independently committable). Task 4 rewrites CLAUDE.md (depends on the topic docs existing — links into them). Task 5 is the reading-test verification.

The repo stays in a coherent state at every commit: after Task 1, `docs/auth.md` exists alongside the still-monolithic CLAUDE.md (some duplication, no errors). The same after Tasks 2-3. Task 4 closes the duplication by trimming CLAUDE.md.

---

## Task 1: Create `docs/auth.md`

**Files:**
- Create: `docs/auth.md`

- [ ] **Step 1: Read today's CLAUDE.md `## Authentication` section to scope the move**

Run: `awk '/^## Authentication$/,/^## ClickHouse$/' CLAUDE.md | head -300`

The section spans lines 91-355. Includes: alias-deps table, `AuthSession` hierarchy, per-session data, `Authorization (CH-derived rights)`, `Configuration` (env vars), `Multi-worker deployment`, `Module map`, `Authorization model` (duplicate), `Login flows`, `Tests`, `Integration tests`, `Open redirect protection`, `Open security follow-ups`.

We move most of these into `docs/auth.md`, but `Configuration` (env vars), `Multi-worker deployment`, `Open redirect protection`, and `Open security follow-ups` belong in `docs/operations.md` (Task 3). Skip those when copying.

- [ ] **Step 2: Create `docs/auth.md` with this structure**

```markdown
# Authentication

The `iris.auth` package adds session-based authentication and tier-based authorization to all routes. CH RBAC is the single source of truth for authorization; `iris.auth` derives a frozen `Rights` view at login and caches it on the session row. See `CLAUDE.md` for the project overview.

## Public surface

```python
from iris.auth import (
    AuthSession,                       # the dataclass returned by every auth dep
    Rights, EMPTY_RIGHTS,              # the rights view + a useful default
    Session, SessionOptional,          # auth-only aliases
    SessionAdmin,                      # global admin
    SessionDatabaseCreator,            # admin OR can_create_database
    SessionDatabaseAdmin,              # admin of the path's `database` parameter
    SessionWrite, SessionRead,         # tier-scoped checks against `database`
    User, install,
)
\`\`\`

## Alias deps

Routes consume the dep aliases as type annotations — no `= Depends(...)` is needed.

| Alias | Admits when | Raises | Returns |
|---|---|---|---|
| `Session` | any logged-in user | 401 with no session | `AuthSession` |
| `SessionOptional` | any caller (returns `None` if no session) | never | `AuthSession \| None` |
| `SessionAdmin` | `session.rights.is_admin` | 401 / 403 | `AdminSession` |
| `SessionDatabaseCreator` | admin or `can_create_database` | 401 / 403 | `DatabaseCreatorSession` |
| `SessionDatabaseAdmin` | admin or `db_admin[database]` | 401 / 403 | `DatabaseAdminSession` |
| `SessionWrite` | admin or `db_admin[database]` or `db_writer[database]` | 401 / 403 | `DatabaseSession` |
| `SessionRead` | admin or any tier on `database` | 401 / 403 | `DatabaseSession` |

The three database-scoped aliases (`SessionRead`/`SessionWrite`/`SessionDatabaseAdmin`) read `database: str` from the calling route's path or query parameters via FastAPI's normal binding.

## Session class hierarchy

`AuthSession` is the base; subclasses add CH operations. Routes get exactly the methods their tier permits — calling a method outside the tier is a type error, not a runtime 403.

```
AuthSession                  # query_as_user(sql, parameters=None, database=None)
├─ DatabaseSession           # bound to self.database; query_as_user auto-scopes
│   └─ DatabaseAdminSession  # adds grant_reader/writer, add_admin_user, revokes,
│                            # group equivalents, delete_database, list_admin_members,
│                            # list_grants, list_row_policies
├─ DatabaseCreatorSession    # adds create_database(name)
└─ AdminSession              # adds query_as_service, reprovision_user, audit
                             # (user_grants, role_grants, …), add/revoke_row_policy
\`\`\`

CH method implementations live in `iris.clickhouse.handle` as standalone async `*_impl` functions; the Session classes import them at module top level. See `docs/clickhouse.md` for the impl side.

## Per-session server-side data

Each `UserSession` carries a mutable `data: dict[str, Any]` field for arbitrary route-managed state. Every alias dep exposes it via `session.data`.

```python
@app.post("/draft")
async def save_draft(request: Request, session: Session, body: dict):
    session.data["draft"] = body
    await request.app.state.auth_session_store.update_data(
        session.id, session.data
    )
    return {"ok": True}
\`\`\`

`session.data` is a per-request snapshot — a fresh `dict` deserialized from the SQLite session row on every request. Mutations to the dict do **not** auto-persist; routes that want the change to survive call `update_data` explicitly.

Lifecycle: `data` is JSON-encoded into the SQLite row alongside the session. Mutations are persisted by `update_data` and survive process restarts. Values must be JSON-encodable (strings, ints, floats, bools, `None`, lists, dicts) — anything else raises `TypeError` at write time. Read-modify-write across an `await` between two requests for the same session has the standard interleaving race; acceptable at ≤20-user scale.

## Authorization (CH-derived rights)

ClickHouse is the only source of truth for authorization. Iris derives a frozen `Rights` view from CH grants once at login (in the post-login hook chain) and caches it on the session row. Routes gate via the alias deps in `iris.auth.deps`, which inspect `session.rights`.

```python
@dataclass(frozen=True, slots=True)
class Rights:
    is_admin: bool
    can_create_database: bool
    db_admin: frozenset[str]
    db_writer: frozenset[str]
    db_reader: frozenset[str]

    def has_read(self, database: str) -> bool: ...
    def has_write(self, database: str) -> bool: ...
    def has_admin(self, database: str) -> bool: ...
\`\`\`

**How rights are derived.** At login, `iris.clickhouse.rights.derive_rights(client, username, groups)`:

1. Resolves effective role names (`<username>_USER` plus each `<group>_GRP`), walking `system.role_grants` transitively.
2. Splits any role ending in `_DBADMIN`, `_DBWRITER`, `_DBREADER` to recover the database name and populates the corresponding `frozenset`.
3. Queries `system.grants` for global flags. CH always expands `GRANT ALL` into primitive privileges, so the admin marker is `ROLE ADMIN at global scope with grant_option=1`.

Operator changes (new tier-role grants, revocations) take effect on the user's next login. There is no mid-session re-derivation. To force a re-derive operationally: revoke the grant in CH and delete the user's session rows.

## Login flows

- **OAuth (`AUTH_METHOD=oauth`)** — `/login` 302s to the IdP authorize URL with PKCE S256 + state in a signed cookie. The IdP redirects back to `/login/callback`, which exchanges the code, verifies the returned `id_token` (RS256/ES256 signature against the IdP's JWKS, plus `iss`/`aud`/`exp` claims), fetches userinfo, and creates a session. JWKS is fetched once at app construction; rotating IdP keys requires app restart. `next` is preserved across the round-trip via the same signed cookie.
- **LDAP/Mock (`AUTH_METHOD=ldap`/`mock`)** — `/login` renders an HTML form (`templates/auth/ldap_form.html`) with a CSRF token. POST `/login` validates CSRF, calls `provider.authenticate(username, password)`, and creates a session on success. Bad creds redirect back to `/login?error=invalid_credentials&next=...`.
- **Logout** — `POST /logout` (CSRF-required) deletes the session and clears the cookie. Local-only — does not call the IdP's end-session endpoint.

The CSRF cookie is rotated on successful login: the post-auth `/login` redirect (and OAuth callback) clear the `iris_csrf` cookie so any pre-auth token capture becomes useless.

`POST /login` is rate-limited per client IP via an in-process token bucket (capacity 10, refill 0.2/sec — 10-attempt burst then ~12 attempts/minute sustained). Exhausted clients receive a 429 with `Retry-After`. Per-process state, fits the `--workers 1` deploy constraint; multi-worker would need Redis.

## Identity matching

- `groups` — exact, case-sensitive match against `User.groups` (verbatim from the IdP). The `<group>_GRP` role in CH is named after the group string.
- `users` — `<username>_USER` — case-sensitive match against `User.username` (the CH role name uses the literal username).
  - OAuth provider sources `username` from the `preferred_username` claim, falling back to `sub` if absent.
  - LDAP provider sources `username` from the `username` substituted into `LDAP_BIND_DN_TEMPLATE`.
  - Mock provider sources `username` from `MOCK_USERNAME`.

## Tests

`tests/conftest.py` sets `AUTH_METHOD=mock` (and the mock creds) at module scope so `iris.app:build_app` can be imported by the suite without arranging env in a fixture. Available fixtures:

- `client` — unauthenticated `TestClient`. Use for tests that exercise the login flow itself, error pages, etc.
- `authed_client` — pre-creates a session in the in-memory store and attaches the cookie. Use for feature tests of routes that just need "a logged-in user".

Provider tests are offline:
- LDAP: `ldap3.MOCK_SYNC` strategy with an in-memory directory (`tests/auth/test_provider_ldap.py`).
- OAuth: `httpx.MockTransport` mocking discovery / token / userinfo (`tests/auth/test_provider_oauth.py`).

## Integration tests (`tests/auth/integration/`)

A second tier under `tests/auth/integration/` runs the OAuth provider end-to-end against a real `quay.io/keycloak/keycloak:26.0` container via `testcontainers-python`. Covers happy paths and natural failure paths exercisable against a real IdP plus full TLS coverage.

- Run only the integration tier: `uv run pytest tests/auth/integration`
- Skip the integration tier (no Docker required): `uv run pytest --ignore=tests/auth/integration`
- Runtime: ~25s on a warm cache (Keycloak boot ~12s dominates).

The realm seed at `tests/auth/integration/seed/keycloak-realm.json` is committed and declarative — it defines an `iris-test` realm with two users (`alice`/`secret` in `admins`+`users`, `bob`/`hunter2` in `users`) and an `iris` client wired up with an explicit `oidc-group-membership-mapper`. TLS certs are generated at session start via `_tls.py` and not committed.

## Module map

```
src/iris/auth/
├── __init__.py        # public surface
├── session.py         # Rights, EMPTY_RIGHTS, serialization helpers
├── identity.py        # User, UserSession, AuthSession + Session subclass hierarchy
├── config.py          # AuthSettings.from_env()
├── sessions.py        # SessionStore (SQLite)
├── exceptions.py      # AuthRequired, AuthForbidden, AuthError + handlers
├── deps.py            # the seven Annotated alias deps
├── csrf.py            # double-submit CSRF
├── rate_limit.py      # TokenBucket
├── routes.py          # /login, /login/callback, /logout, /api/whoami; install(app)
└── providers/         # mock, ldap, oauth
\`\`\`

The CH-side bootstrap (`bootstrap_admin`) lives in `iris.clickhouse.bootstrap`, not `iris.auth` — see `docs/clickhouse.md`.

## Evolution

The current shape of this surface results from the following design rounds; the dated specs are the authoritative rationale.

- **2026-05-03** — initial auth scaffold + mock provider → `docs/superpowers/specs/2026-05-03-auth-design.md`
- **2026-05-03** — SQLite role-mapping subsystem (later removed) → `docs/superpowers/specs/2026-05-03-roles-authz-design.md`
- **2026-05-04** — session API simplification → `docs/superpowers/specs/2026-05-04-session-api-simplification-design.md`
- **2026-05-05** — auth integration tests via Keycloak testcontainer → `docs/superpowers/specs/2026-05-05-auth-testcontainers-design.md`
- **2026-05-06** — SQLite session store (replaces in-memory) → `docs/superpowers/specs/2026-05-06-sqlite-session-store-design.md`
- **2026-05-06** — authz moved to SQLite (later removed) → `docs/superpowers/specs/2026-05-06-authz-sqlite-design.md`
- **2026-05-08** — CH-only authorization (drops SQLite role mapping) → `docs/superpowers/specs/2026-05-08-clickhouse-only-authz-design.md`
- **2026-05-08** — session-as-handle: one parameter per route → `docs/superpowers/specs/2026-05-08-session-as-handle-design.md`
```

Replace the escaped backtick fences (`\``) with real triple-backticks when writing the file.

- [ ] **Step 3: Read it back to verify**

Run: `wc -l docs/auth.md`
Expected: roughly 220-260 lines.

- [ ] **Step 4: Sanity check the Evolution links**

Run: `for f in $(grep -oE 'docs/superpowers/specs/[^ ` ']+' docs/auth.md | sort -u); do test -f $f && echo "OK: $f" || echo "MISSING: $f"; done`

Expected: every line says `OK:`. If any are missing, the spec filename in the Evolution section is wrong — fix it against `ls docs/superpowers/specs/`.

- [ ] **Step 5: Commit**

```bash
git add docs/auth.md
git commit -m "docs: extract Authentication section into docs/auth.md"
```

---

## Task 2: Create `docs/clickhouse.md`

**Files:**
- Create: `docs/clickhouse.md`

- [ ] **Step 1: Read today's CLAUDE.md `## ClickHouse` section to scope the move**

Run: `awk '/^## ClickHouse$/,/^EOF$/' CLAUDE.md | head -200`

The section spans lines 356-end. Includes: `Public surface` (drop the import wall, link to `__init__.py`), `Conventions`, `DDL safety`, `Configuration` (env vars), `Auth ↔ ClickHouse bridge`, `Per-database admin tier`, `Tests`, `Deferred (v1.1+)`.

The `Configuration` env vars and `Deferred` items belong in `docs/operations.md` (Task 3). Skip when copying.

The `Auth ↔ ClickHouse bridge` and `Per-database admin tier` sections currently overlap (both explain Session subclasses + handle providers). Merge them into a single `## Per-tier methods and route examples` section in the new doc.

- [ ] **Step 2: Create `docs/clickhouse.md` with this structure**

```markdown
# ClickHouse

The `iris.clickhouse` package provisions ClickHouse users, roles, grants, and row policies, and provides standalone async `*_impl` functions that the Session subclasses in `iris.auth.identity` call into. Plain-data helpers (`audit.py`, `bootstrap.py`, `client.py`, `grants.py`, `policies.py`, `users.py`, `rights.py`) are independent of `iris.auth`. See `CLAUDE.md` for the project overview and `docs/auth.md` for the auth side.

## Public surface

`__all__` in `src/iris/clickhouse/__init__.py` is the source of truth. Notable exports: `ClickHouseSettings`, `bootstrap_admin`, `derive_rights`, `init_user_rights`, the tier-role helpers (`create_tier_roles`, `drop_tier_roles`, `tier_role_name`, `grant_tier_to_user/group`, `revoke_tier_from_user/group`, `TIER_DBADMIN/_DBWRITER/_DBREADER`), `add_row_policy`, `revoke_row_policy`, audit helpers (`user_grants`, `role_grants`, `user_role_memberships`, `user_row_policies`, `role_row_policies`, `table_row_policies`), and `GLOBAL_ADMIN_ROLE`.

`install` lives in `iris.clickhouse.install` and is *not* re-exported — callers do `from iris.clickhouse.install import install` (only `iris.app:build_app`).

## Conventions

- Per-user role: `<username>_USER` (`USER_ROLE_SUFFIX` in `users.py`).
- Per-group role: `<group>_GRP` (`GROUP_ROLE_SUFFIX` in `users.py`).
- Per-database tier roles: `<X>_DBADMIN`, `<X>_DBWRITER`, `<X>_DBREADER`. Created by `create_database`, dropped by `delete_database`.
- Sentinel role: `iris_global_admin` (constant `GLOBAL_ADMIN_ROLE`). Holds no privileges itself; wildcard row policies attach to it.
- Row-policy name (restrictive): `<database>_<table>_<role>_<slug>_<8charhash>` — slug strips non-`[a-zA-Z0-9_]`, hash disambiguates collisions.
- Row-policy wildcards (deterministic, idempotent): `<database>_<table>_iris_global_admin` and `<database>_<table>_<database>_DBADMIN`. Created by `add_row_policy` on every call; *not* dropped by `revoke_row_policy`.
- All operations are idempotent: re-running is safe. `init_user_rights` reconciles group memberships (revokes `_GRP` roles no longer in the input, grants the new ones).

## DDL safety

`identifiers.py` is the single safety contract. External-source strings (usernames from auth, db/table/column names from callers) flow through `validate_identifier` (rejects anything outside `[a-zA-Z0-9_]+`) and `quote_identifier` (validates + backticks). Row-policy values use `quote_string` for SQL literal escaping. DDL is built from these helpers; `client.command()` runs it without parameter binding. DML (audit `SELECT`s) uses ClickHouse's native `{name:Type}` placeholder syntax via `client.query(..., parameters=...)`.

## Per-tier methods and route examples

Routes consume one alias dep per tier. The dep returns a Session subclass (defined in `iris.auth.identity`) whose method surface matches the tier; there is no separate handle parameter. The Session value carries both admission and capability.

```python
from iris.auth import Session, SessionAdmin, SessionRead, SessionDatabaseAdmin

@app.get("/db/{database}/count")
async def count(database: str, session: SessionRead):
    return await session.query_as_user("SELECT count() FROM t")

@app.post("/db/{database}/grants/users/{username}")
async def grant_read(database: str, username: str, session: SessionDatabaseAdmin):
    await session.grant_reader(username)
    return {"granted": True}

@app.get("/admin/users/{username}/grants")
async def audit(username: str, session: SessionAdmin):
    return await session.user_grants(username=username)
\`\`\`

`SessionRead` returns a `DatabaseSession` bound to the path's `database`. `query_as_user("SELECT count() FROM t")` resolves `t` against `<database>` because the impersonated request includes `?database=<database>` in the URL.

For routes that need to query a specific database from a non-DB-scoped session (`Session` or `SessionAdmin`), `query_as_user` accepts a `database=` kwarg. `SessionAdmin.query_as_service` likewise accepts `database=`.

| Tier | Alias | Returns | Selected methods |
|---|---|---|---|
| Any logged-in user | `Session` | `AuthSession` | `query_as_user(sql, database=None)` |
| Database creator | `SessionDatabaseCreator` | `DatabaseCreatorSession` | `create_database(name)` |
| Per-database admin | `SessionDatabaseAdmin` | `DatabaseAdminSession` (bound to `database`) | `grant_reader/writer`, `add_admin_user`, `revoke_*`, group equivalents, `delete_database`, `list_admin_members`, `list_grants`, `list_row_policies` |
| Global admin | `SessionAdmin` | `AdminSession` | `query_as_service`, `reprovision_user`, `add/revoke_row_policy`, audit (`user_grants`, `role_grants`, `user_role_memberships`, `user_row_policies`, `role_row_policies`, `table_row_policies`) |

`create_database(name)` is the lifecycle entry point: it runs `CREATE DATABASE IF NOT EXISTS`, creates the three tier roles with their privilege grants, and grants `<name>_DBADMIN` to the creator's `<creator>_USER` role. All steps idempotent. `delete_database()` reverses: `DROP DATABASE IF EXISTS` then drops the three tier roles.

A global admin who needs to do per-DB operations writes routes gated by `SessionDatabaseAdmin` (which admits admins via the `is_admin` superset). Routes that need both global ops and per-DB ops compose two Session parameters; this is rare.

**Why two HTTP transports.** `query_as_user` prepends `EXECUTE AS <quoted_username>` to the SQL. ClickHouse's `EXECUTE AS user <SELECT>` body grammar rejects `FORMAT` clauses, but `clickhouse-connect`'s `query()` always appends `FORMAT Native` — incompatible. The Session methods therefore use a separate `httpx.AsyncClient` for impersonated queries, posting to ClickHouse's HTTP endpoint with `?default_format=JSONEachRow` as a URL parameter. Service-identity queries (`query_as_service`) and admin/audit methods keep using `clickhouse-connect`. As a consequence, `query_as_user` returns `list[dict[str, Any]]` (parsed JSON Lines) rather than a `QueryResult`.

## Bootstrap

At iris launch, `bootstrap_admin(client, admin_user=, admin_group=)` (in `iris.clickhouse.bootstrap`) always creates the `iris_global_admin` sentinel role. If `CLICKHOUSE_ADMIN_USER=alice` is set and no `_USER`-suffixed role currently holds the admin marker, iris creates `alice_USER` with `GRANT ALL ON *.* WITH GRANT OPTION` plus `iris_global_admin` granted to it. If `CLICKHOUSE_ADMIN_GROUP=iris_admin` is set and no `_GRP`-suffixed role currently holds admin, iris creates `iris_admin_GRP` the same way. Both channels are independently idempotent.

The `iris_global_admin` sentinel role holds no privileges of its own. Wildcard row policies attach to it (per table, on every `add_row_policy` call) so every global admin sees all rows on tables with restrictive policies. Granting `iris_global_admin` to a role does not make that role admin in any sense iris's authorization layer recognises — the admin marker is still ROLE ADMIN+WGO at global scope.

The admin-detection check is restricted to roles ending in `_USER` (resp. `_GRP`) so iris's own connection identity (which holds ROLE ADMIN+WGO to manage RBAC state) is never mistaken for a bootstrapped admin.

## Row policies

`add_row_policy(database, table, column, role, value)` emits three `CREATE ROW POLICY` statements per call:

1. The restrictive policy for the target role: `... USING <column> = <value> TO <role>`.
2. A wildcard for `iris_global_admin`: `... USING 1 TO iris_global_admin` (every global admin sees all rows).
3. A wildcard for `<database>_DBADMIN`: `... USING 1 TO <database>_DBADMIN` (every per-database admin of that DB sees all rows).

The two wildcards have deterministic names so re-runs are idempotent (`CREATE ROW POLICY IF NOT EXISTS`). They persist after the last restrictive policy is revoked — operators using iris's `revoke_row_policy` ALSO add policies via `add_row_policy`, so the wildcards staying around is the safe default.

**Pre-create-on-grant for username enumeration.** Granting a tier role to a user who has never logged in is supported: tier-grant helpers issue `CREATE ROLE IF NOT EXISTS <target>_USER` before granting, so the CH response is the same whether the target has authenticated or not.

## Post-login hook chain

`iris.clickhouse.install(app)` registers a hook on `app.state.post_login_hooks` that fires on every successful login. The hook does two things in order: `init_user_rights` (provisions the CH user/role/group memberships) and `derive_rights` (computes the `Rights` view), then `set_rights` persists the rights to the session row. Cookie-based session refreshes do NOT re-provision; the cached `Rights` is what every subsequent request sees. Group changes between two logins are reconciled.

**iris's liveness is tied to ClickHouse's.** This is intentional: iris is a thin layer in front of ClickHouse, and a logged-in user with no ability to reach the data backend can't accomplish anything useful. Login fails loud when CH is down — operators see the failure mode in the access logs, monitoring catches it, and users get a real error rather than a half-broken session.

`build_app(install_clickhouse=False)` skips the bridge entirely — used by auth tests that don't need a CH testcontainer. With CH disabled, the post-login hook chain is empty and sessions land with `EMPTY_RIGHTS` and `client=None`/`http_client=None`.

## Tests

The test suite uses `testcontainers-python` to spin up `clickhouse/clickhouse-server:26.3` in Docker. The container is session-scoped (one instance per pytest run); per-test isolation comes from a UUID-derived `prefix` fixture that namespaces every entity name. Docker is required to run `tests/clickhouse/`.

The `chdb` library was originally trialed for in-process testing; `chdb==4.1.6`'s embedded server hardcodes `system.user_directories` to a read-only `users_xml` entry, blocking all RBAC DDL at runtime. See the design spec at `docs/superpowers/specs/2026-05-05-clickhouse-authz-design.md` for the verification.

## Module map

```
src/iris/clickhouse/
├── __init__.py              # public surface (no install re-export)
├── audit.py                 # read-only system.* queries
├── bootstrap.py             # bootstrap_admin + iris_global_admin sentinel
├── client.py                # build_client
├── config.py                # ClickHouseSettings.from_env()
├── grants.py                # tier-role lifecycle + tier-grant helpers
├── handle.py                # standalone *_impl async functions called by Session methods
├── identifiers.py           # validate_identifier, quote_identifier, quote_string
├── install.py               # install(app): wires CH client + post-login hook
├── policies.py              # add_row_policy, revoke_row_policy
├── rights.py                # derive_rights — walks system.role_grants + system.grants
└── users.py                 # init_user_rights — per-user/per-group role provisioning
\`\`\`

## Evolution

The current shape of this surface results from the following design rounds; the dated specs are the authoritative rationale.

- **2026-05-05** — CH RBAC primitives (users, roles, grants, row policies) → `docs/superpowers/specs/2026-05-05-clickhouse-authz-design.md`
- **2026-05-06** — auth↔CH bridge: handles + post-login provisioning → `docs/superpowers/specs/2026-05-06-auth-clickhouse-bridge-design.md`
- **2026-05-06** — per-database admin tier (initially SQLite-backed) → `docs/superpowers/specs/2026-05-06-clickhouse-database-admin-design.md`
- **2026-05-08** — CH-only authorization, tier roles in CH → `docs/superpowers/specs/2026-05-08-clickhouse-only-authz-design.md`
- **2026-05-08** — session-as-handle: handle classes removed → `docs/superpowers/specs/2026-05-08-session-as-handle-design.md`
- **2026-05-08** — bootstrap rework + iris_global_admin sentinel → `docs/superpowers/specs/2026-05-08-bootstrap-rework-design.md`
```

Replace the escaped backtick fences with real triple-backticks when writing the file.

- [ ] **Step 3: Read it back to verify**

Run: `wc -l docs/clickhouse.md`
Expected: roughly 180-220 lines.

- [ ] **Step 4: Sanity check the Evolution links**

Run: `for f in $(grep -oE 'docs/superpowers/specs/[^ ` ']+' docs/clickhouse.md | sort -u); do test -f $f && echo "OK: $f" || echo "MISSING: $f"; done`

Expected: every line says `OK:`. Fix any missing entries by checking `ls docs/superpowers/specs/`.

- [ ] **Step 5: Commit**

```bash
git add docs/clickhouse.md
git commit -m "docs: extract ClickHouse section into docs/clickhouse.md"
```

---

## Task 3: Create `docs/operations.md`

**Files:**
- Create: `docs/operations.md`

- [ ] **Step 1: Identify the operational content scattered through CLAUDE.md**

Run: `grep -nE '^### Multi-worker deployment|^### Open redirect protection|^### Open security follow-ups|^### Configuration|^### Deferred' CLAUDE.md`

Expected output identifies six sub-sections to relocate.

- [ ] **Step 2: Create `docs/operations.md` with this structure**

```markdown
# Operations

Operator-facing concerns for deploying and running iris. See `CLAUDE.md` for the project overview, `docs/auth.md` for auth internals, `docs/clickhouse.md` for CH internals.

## Configuration

Env vars are loaded at `import iris` time via `python-dotenv`. If a `.env` file exists at the project root (gitignored), its values populate `os.environ` for any keys not already set. Real shell env vars take precedence over `.env` (`load_dotenv` is called with `override=False`), so a CI / production deploy can override individual values without editing `.env`. Tests inherit the same loader; `tests/conftest.py` sets `os.environ.setdefault(...)` defaults at module scope before iris is imported.

### Auth env vars

```
AUTH_METHOD=oauth | ldap | mock
SESSION_COOKIE_NAME=iris_session
SESSION_TTL_SECONDS=43200            # 12h, sliding TTL refreshed on each request
SESSION_ABSOLUTE_TTL_SECONDS=2592000 # 30d, hard cap on top of sliding TTL
SESSION_MAX_PER_USER=10              # cap concurrent sessions per User.subject (oldest evicted)
AUTH_DB_PATH=./iris-auth.db          # SQLite file backing the session store; :memory: for tests
COOKIE_SECURE=true                   # set false for local dev over http

# OAuth (OIDC discovery)
OIDC_ISSUER_URL=https://keycloak.example.com/realms/iris
OIDC_CLIENT_ID=iris
OIDC_CLIENT_SECRET=...
OIDC_SCOPES=openid profile email groups
OIDC_CA_CERT_PATH=                   # optional: PEM bundle for IdP cert validation (private CA)

# LDAP
LDAP_URL=ldaps://ldap.example.com:636
LDAP_BIND_DN_TEMPLATE=uid={username},ou=people,dc=corp,dc=local
LDAP_GROUP_BASE_DN=ou=groups,dc=corp,dc=local
LDAP_REQUIRE_TLS=true                # reject ldap:// at startup
LDAP_CA_CERT_PATH=                   # optional: PEM bundle for cert validation

# Mock (for tests; AUTH_METHOD=mock)
MOCK_USERNAME=alice
MOCK_PASSWORD=secret
MOCK_GROUPS=admins,users
MOCK_DISPLAY_NAME=Alice
\`\`\`

`AuthSettings.from_env()` runs at app construction; missing required vars or unrecognized values fail loudly. `_get_bool` raises on typos (`COOKIE_SECURE=ture` is rejected, not silently false).

### ClickHouse env vars

```
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=iris_service          # CH login iris connects as; also the IMPERSONATE grantee
CLICKHOUSE_PASSWORD=replace-me
CLICKHOUSE_SECURE=true                # https
CLICKHOUSE_VERIFY=true                # TLS verification
# CLICKHOUSE_CA_CERT_PATH=/etc/ssl/certs/ca-bundle.crt
\`\`\`

### Bootstrap admin env vars

```
CLICKHOUSE_ADMIN_USER=                # IdP username of bootstrap admin (e.g. alice)
CLICKHOUSE_ADMIN_GROUP=               # IdP group name of bootstrap admins (e.g. iris_admin)
\`\`\`

When `CLICKHOUSE_ADMIN_USER=alice` is set and no `_USER`-suffixed CH role currently holds the admin marker, iris creates `alice_USER` with full admin grants at boot. Same for `CLICKHOUSE_ADMIN_GROUP` against `_GRP`-suffixed roles. Both channels are independently idempotent. See `docs/clickhouse.md` for the bootstrap behavior.

### `.env` permissions

The file may contain secrets (`OIDC_CLIENT_SECRET`, `MOCK_PASSWORD`, etc.). On a multi-user host, `chmod 600 .env` so it's only readable by the iris service user. The file is gitignored; check that your container/build pipeline doesn't bake it into images.

## Multi-worker deployment

Sessions live in a SQLite file; multiple uvicorn workers share state by pointing at the same `AUTH_DB_PATH`. The store opens its connection in WAL mode (`PRAGMA journal_mode=WAL`) so concurrent readers don't block on a writer, and `PRAGMA synchronous=NORMAL` keeps writes cheap. Workers can scale freely on a single host (e.g., `uvicorn --workers 4`) as long as the DB path is on local disk reachable by every worker. Cross-host deploys still need a shared filesystem — or swap the store backend.

Sessions also survive process restarts.

iris launches via uvicorn factory mode: `uvicorn.run("iris.app:build_app", factory=True, ...)`. Importing `build_app` is side-effect-free for tests.

## Open redirect protection

`_safe_next(url)` accepts only same-origin relative paths. Rejects empty, non-`/`-prefixed, `//`-prefixed (protocol-relative), absolute URLs, and backslash-containing strings (browsers normalize `\` → `/` before same-origin checks). Applied at `POST /login` and `GET /login/callback`. Failure-redirect URLs are constructed via `urllib.parse.urlencode` so error tokens or path components can't break query parsing.

## Open security follow-ups

These are accepted residual risks for the ≤20-user / `--workers 1` deploy profile; revisit when scaling out or relocating behind a load balancer.

- **Rate-limiting behind a proxy.** Rate limiting on `POST /login` keys on `request.client.host`. Behind a reverse proxy this is the proxy's IP — the bucket becomes effectively global. Mitigation: run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy>` so `request.client.host` reflects the `X-Forwarded-For` value.
- **JWKS rotation.** `OAuthProvider` caches the IdP's JWKS once on first discovery. If the IdP rotates signing keys, all logins fail until iris is restarted. Acceptable at ≤20-user / multi-month rotation cadence; tighten by re-fetching on `kid`-not-in-set if rotation matters.
- **OIDC discovery latency.** Discovery is now lazy: the *first* login attempt after restart pays the discovery latency. Acceptable for v1, but means a slow IdP shifts startup latency to a request boundary instead of failing loud at boot.
- **`derive_rights` query cost.** Runs a small handful of CH queries at login (role-grants walk + a single grants enumeration). Sub-millisecond at ≤20-user scale; for higher request volumes, profile and consider caching the effective role set per user.
- **Out-of-band admin promotion.** If an operator runs raw `GRANT ALL ON *.* TO foo_USER WITH GRANT OPTION` outside iris's bootstrap path, `foo` gets admin grants but not `iris_global_admin`. `derive_rights` still returns `is_admin=True`, but row-policy wildcards keyed on `iris_global_admin` don't apply, so foo can't see rows on tables that have any restrictive policy. Mitigation: run `GRANT iris_global_admin TO foo_USER` alongside the admin grant.

## Deferred

- **Connection pooling.** `clickhouse-connect`'s `Client` is per-process today; multi-worker deploys rely on per-process pools, but a shared pool would lower memory.
- **Streaming impersonated queries.** A streaming variant of `query_as_user` for routes that need to stream large result sets back through Datastar SSE without buffering the whole response in memory.

## Migration runbooks

The recent CH-only-authz, session-as-handle, and bootstrap-rework migrations each shipped with an operator runbook in their respective specs:

- `docs/superpowers/specs/2026-05-08-clickhouse-only-authz-design.md` — wiping `AUTH_DB_PATH` + `IRIS_BOOTSTRAP_USER` (now `CLICKHOUSE_ADMIN_USER`).
- `docs/superpowers/specs/2026-05-08-session-as-handle-design.md` — no operator-facing change; route-author concern only.
- `docs/superpowers/specs/2026-05-08-bootstrap-rework-design.md` — replacing `IRIS_BOOTSTRAP_USER` and `CLICKHOUSE_SERVICE_ADMIN_*` env vars with `CLICKHOUSE_ADMIN_USER` and `CLICKHOUSE_ADMIN_GROUP`.

Read the relevant spec when planning an upgrade across one of these versions.
```

Replace the escaped backtick fences with real triple-backticks.

- [ ] **Step 3: Read it back to verify**

Run: `wc -l docs/operations.md`
Expected: roughly 100-130 lines.

- [ ] **Step 4: Commit**

```bash
git add docs/operations.md
git commit -m "docs: collect operator-facing concerns into docs/operations.md"
```

---

## Task 4: Rewrite `CLAUDE.md` as a navigator

**Files:**
- Modify: `CLAUDE.md`

This task replaces CLAUDE.md's content with a thinner navigator. The Datastar-specific parts of `## Architecture & Datastar integration` stay (route-writing reference); auth/clickhouse/operations content moves out (now in topic docs).

- [ ] **Step 1: Replace `CLAUDE.md` with the new structure**

```markdown
# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

iris is a thin FastAPI + Datastar layer in front of ClickHouse. The auth layer authenticates users via OAuth/LDAP/mock providers, derives a frozen `Rights` view from CH grants at login, and gates routes via tier-typed Session subclasses. The ClickHouse layer provisions per-user / per-group / per-database CH roles, enforces tier-scoped operations, and runs impersonated queries via a separate httpx client.

## Project state

Python web app scaffolded with `uv` / hatchling: **FastAPI + Jinja2** server, **Datastar** (https://data-star.dev/) on the frontend. `src/iris/__init__.py:main()` boots a uvicorn dev server. The home page demonstrates two end-to-end Datastar patterns. Pytest is wired up; no linter or formatter yet.

`requires-python` is currently `>=3.13` — bumped down from 3.14 because the only 3.14 build `uv` could fetch was `3.14.0a6`, on which `pydantic-core` (a FastAPI dep) segfaults. Re-evaluate when a stable 3.14 build is reachable AND pydantic publishes 3.14 wheels.

## Commands

- Run the dev server: `uv run iris` (binds 127.0.0.1:8000) or `uv run uvicorn iris.app:app --reload` for hot-reload.
- Install/sync after editing `pyproject.toml`: `uv sync`
- Add a runtime dep: `uv add <pkg>` — and `uv add --dev <pkg>` for dev-only.

### Lint & type-check

- `uv run ruff check` — currently produces one intentional `E402` in `src/iris/__init__.py` (the `from iris.app import app` must follow `load_dotenv()` so `.env` populates env first).
- `uv run basedpyright --level error` — gate. Must stay at zero errors.
- `uv run basedpyright --level warning` — also at zero. The `[tool.basedpyright]` config in `pyproject.toml` disables a handful of noisy categories that fire on intentional FastAPI/pytest patterns.

### Tests

Pytest is the test runner. Config lives under `[tool.pytest.ini_options]` in `pyproject.toml` (`testpaths = ["tests"]`, `--import-mode=importlib`).

- Run the full suite: `uv run pytest`
- Run a single file: `uv run pytest tests/test_app.py`
- Run a single test by node id: `uv run pytest tests/test_app.py::test_index_renders`
- Filter by name: `uv run pytest -k <substring>`
- Stop at first failure with verbose tracebacks: `uv run pytest -x -vv`

Conventions for new tests:
- Tests live under `tests/` at the repo root (sibling to `src/`), not inside the package.
- **Do not add `__init__.py` under `tests/`** — `--import-mode=importlib` requires `tests/` to *not* be a package; in exchange every test file must have a unique basename across the suite.
- Import the package as `from iris.app import app` (or `from iris import …`). FastAPI's `TestClient(app)` is the standard fixture; use `from fastapi.testclient import TestClient`.

## Conventions

Patterns an agent must follow that aren't obvious from reading code:

- **DDL safety**: external strings flow through `validate_identifier` + `quote_identifier` (`iris.clickhouse.identifiers`). Never f-string-concat raw user input into SQL. DML uses CH's `{name:Type}` placeholder syntax via `client.query(..., parameters=...)`.
- **Pre-create-on-grant**: tier-grant helpers issue `CREATE ROLE IF NOT EXISTS <target>_USER` before granting. Required for username-enumeration defence; don't shortcut.
- **Session `data` is a per-request snapshot**: mutations don't auto-persist. Routes that want to write through call `await request.app.state.auth_session_store.update_data(session.id, session.data)`.
- **Session methods use top-level imports of `iris.clickhouse.handle.*_impl`**: lazy method-body imports were a workaround for a now-removed cycle. Don't regress.
- **One parameter per route**: `session: SessionRead` / `SessionDatabaseAdmin` / etc. carry both admission and capability. Don't pair an alias with a separate handle dep — the handle classes are gone.
- **Refactor pattern**: spec → plan → atomic commit. Big renames go through a deliberate breakage window with one big-bang commit at the end. Don't try to incrementally split refactors that need to be atomic.
- **Tests don't mock the database**: `tests/clickhouse/` uses a real CH testcontainer (session-scoped). Per-test isolation is the `prefix` fixture (UUID-prefixed entity names).

## Architecture & Datastar integration

### Layout

- `src/iris/__init__.py` — re-exports `app` and defines `main()` (uvicorn launcher for the `iris` script).
- `src/iris/app.py` — FastAPI app, routes, and `Jinja2Templates` initialization.
- `src/iris/templates/` — Jinja2 templates packaged with the wheel.
- `src/iris/auth/` — auth subsystem (see `docs/auth.md`).
- `src/iris/clickhouse/` — CH subsystem (see `docs/clickhouse.md`).
- `tests/test_app.py` — route-level tests via FastAPI's `TestClient`.

### How Datastar talks to the backend

Datastar is hypermedia-first with reactive *signals*. Two flavors of interaction in this repo:

1. **Pure-client reactivity.** A section declares signals via `data-signals="{count: 0}"` and references them with `$count` inside `data-on:click`, `data-text`, `data-show`, etc. No round-trip; the browser handles it.
2. **Server-driven via SSE.** A `data-on:click="@get('/api/greet')"` triggers a fetch. Datastar attaches a `Datastar-Request: true` header and serializes signals into a `datastar` query param (for GET/DELETE) or JSON body (for POST/PUT/PATCH). The server consumes them via the `Signals` annotated dep and returns a `text/event-stream` response carrying `datastar-patch-elements` events that morph into the DOM by element id.

#### The `Signals` dependency

The SDK's `read_signals(request)` returns `dict | None` (None when the `Datastar-Request` header is absent or the payload is empty). To avoid `or {}` boilerplate in every route, `app.py` defines a thin wrapper:

```python
async def _signals(request: Request) -> dict[str, Any]:
    return await read_signals(request) or {}

Signals = Annotated[dict[str, Any], Depends(_signals)]
\`\`\`

Routes then take `signals: Signals` and get a guaranteed dict — no None handling.

### SDK gotchas (already worked around in `app.py`)

- Imports that compose correctly: `from datastar_py.fastapi import DatastarResponse, read_signals, ServerSentEventGenerator as SSE`. Construct responses as `return DatastarResponse(SSE.patch_elements("<div id='x'>...</div>"))`.
- **Avoid `@datastar_response` on FastAPI routes.** FastAPI 0.136's generator-detection mis-classifies the wrapper and routes it through the JSONL streamer, raising `'async for' requires an object with __aiter__ method, got coroutine`. Returning `DatastarResponse(...)` directly sidesteps this.
- When testing the SSE endpoint, requests must include `headers={"Datastar-Request": "true"}` and pass signals as `params={"datastar": json.dumps({...})}` for GET/DELETE.
- Always HTML-escape any signal value before interpolating it into a `patch_elements` payload (use `html.escape`); Datastar inserts the bytes as-is.

### Examples currently in `index.html`

- **Counter** — `data-signals="{count: 0}"`, `data-on:click="$count++"`, `data-text="$count"`. Pure client.
- **Greeting** — `<input data-bind="name">` two-way-bound to a `name` signal; the button calls `@get('/api/greet')`; the server returns an `id="greeting"` fragment that morphs into the placeholder.
- **Server clock** — long-lived SSE stream demonstrating `async def` generators. The `_clock_stream` generator `yield`s a `SSE.patch_signals({"now": ...})` event every second. TestClient (sync) deadlocks on infinite SSE responses, so the generator is unit-tested directly via `asyncio.run(_clock_stream().__anext__())` rather than through the route.

### Datastar attribute cheatsheet

- `data-signals="{...}"` declares signals; reference them with `$name` in expressions.
- `data-bind="name"` two-way binds a form element to a signal.
- `data-text="$expr"`, `data-show="$expr"`, `data-class="{cls: $expr}"`, `data-attr:foo="$expr"`.
- `data-on:click="..."` (note the colon, not hyphen). Inside the expression, server actions are `@get('/url')`, `@post('/url')`, `@put`, `@delete`, `@patch`.
- Server SSE events: `datastar-patch-elements` (HTML morph by id) and `datastar-patch-signals` (JSON signals patch). The SDK's `SSE.patch_elements()` / `SSE.patch_signals()` formats these correctly.

## Module map

```
src/iris/
├── __init__.py        # main() + load_dotenv, re-exports app
├── app.py             # build_app(), Datastar routes, /, /api/greet, /api/clock
├── middleware.py      # SecurityHeadersMiddleware (CSP)
├── templates/         # Jinja2 — base.html + index.html
├── auth/              # auth subsystem — full surface in docs/auth.md
└── clickhouse/        # CH subsystem — full surface in docs/clickhouse.md
\`\`\`

## Env vars (quick reference)

Full descriptions, `.env` semantics, and operator runbooks live in `docs/operations.md`. Quick reference:

| Var | Purpose |
|---|---|
| `AUTH_METHOD` | `oauth` / `ldap` / `mock` |
| `SESSION_*` | session TTLs, cookie name, max-per-user |
| `AUTH_DB_PATH` | SQLite session store path; `:memory:` for tests |
| `COOKIE_SECURE` | set `false` for local dev over http |
| `OIDC_*` | OAuth/OIDC discovery (when `AUTH_METHOD=oauth`) |
| `LDAP_*` | LDAP bind + group search (when `AUTH_METHOD=ldap`) |
| `MOCK_*` | mock provider (when `AUTH_METHOD=mock`) |
| `CLICKHOUSE_HOST` / `_PORT` / `_USER` / `_PASSWORD` | CH connection |
| `CLICKHOUSE_SECURE` / `_VERIFY` / `_CA_CERT_PATH` | TLS settings |
| `CLICKHOUSE_ADMIN_USER` | bootstrap admin's IdP username |
| `CLICKHOUSE_ADMIN_GROUP` | bootstrap admin group's IdP name |

## See also

- `docs/auth.md` — full auth surface (alias deps, Session hierarchy, providers, login flows, tests)
- `docs/clickhouse.md` — full CH surface (tier roles, bootstrap, row policies, the bridge with auth)
- `docs/operations.md` — deployment, env-var depth, security follow-ups, migration runbooks
- `docs/superpowers/specs/` — dated design specs (the *why* behind the current shape)
- `docs/superpowers/plans/` — implementation plans (paired with each spec)
```

Replace the escaped backtick fences with real triple-backticks when writing the file.

- [ ] **Step 2: Verify CLAUDE.md is the right size**

Run: `wc -l CLAUDE.md`
Expected: 150-200 lines (target was ~180).

If it's significantly larger, find the bloat and trim. If significantly smaller, double-check the Architecture/Datastar section is intact.

- [ ] **Step 3: Verify cross-references**

Run: `grep -E 'docs/(auth|clickhouse|operations)\.md' CLAUDE.md`

Expected: at least one reference to each, in the See also section and in the Module map / Env vars sections.

- [ ] **Step 4: Reading-test**

Read CLAUDE.md as if you've never seen the project. Trace these three questions:

1. "How do I add a route gated on database admin?" — should land you in `docs/clickhouse.md`'s "Per-tier methods and route examples".
2. "What env var configures the bootstrap admin?" — should land you in `docs/operations.md`'s Bootstrap admin env vars section.
3. "Why is auth shaped this way?" — should land you at one of the dated specs via the Evolution sections in `docs/auth.md`.

If any of these don't work cleanly, fix the navigation (links, See also, Module map descriptions).

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE): rewrite as navigator + add Conventions section"
```

---

## Task 5: Final reading-test verification

**Files:** none.

- [ ] **Step 1: Confirm the full doc set**

Run: `ls docs/auth.md docs/clickhouse.md docs/operations.md && wc -l CLAUDE.md docs/auth.md docs/clickhouse.md docs/operations.md`

Expected:
- `docs/auth.md`: ~250 lines
- `docs/clickhouse.md`: ~200 lines
- `docs/operations.md`: ~120 lines
- `CLAUDE.md`: ~180 lines

- [ ] **Step 2: Verify no orphaned content**

Run: `grep -nE '^### Authorization model|^### Auth ↔ ClickHouse bridge|^### Per-database admin tier|^### Open redirect protection|^### Open security follow-ups' CLAUDE.md`

Expected: no matches. These sections moved out — if any persists, that's a bug from Task 4.

- [ ] **Step 3: Verify topic-doc Evolution sections point at real spec files**

Run:

```bash
for doc in docs/auth.md docs/clickhouse.md; do
  echo "=== $doc ==="
  for f in $(grep -oE 'docs/superpowers/specs/[^ ` ']+' "$doc" | sort -u); do
    test -f "$f" && echo "OK: $f" || echo "MISSING: $f"
  done
done
```

Expected: every line says `OK:`. Any `MISSING:` indicates a wrong filename in an Evolution section — fix it.

- [ ] **Step 4: Smoke check the agent UX**

Open `CLAUDE.md` and confirm the See also block links to all four resources (`docs/auth.md`, `docs/clickhouse.md`, `docs/operations.md`, `docs/superpowers/specs/`). Open each topic doc and confirm it has an Evolution section (except `docs/operations.md`, which deliberately doesn't).

- [ ] **Step 5: No commit needed**

If all checks pass, the refactor is complete. If any failed in step 1-3, fix the underlying issue (probably in Task 1, 2, or 4) and re-run the relevant check.

---

## Self-Review

- **Spec coverage check:**
  - Target shape of CLAUDE.md (~180 lines, navigator + Conventions) — Task 4.
  - `docs/auth.md` content (8-section structure ending in Evolution) — Task 1.
  - `docs/clickhouse.md` content (9-section structure, bridge+per-db-admin merged) — Task 2.
  - `docs/operations.md` content (deployment, env-vars, security, deferred, migration runbooks) — Task 3.
  - Evolution section template + per-doc bullet lists — Tasks 1, 2 (operations.md deliberately has none, per spec).
  - Conventions section in CLAUDE.md (the new content) — Task 4 step 1 includes the bullet list.
  - Cross-reference rules (CLAUDE.md → topic docs, topic docs → specs, specs ← nobody) — Tasks 1, 2, 4.
  - "What CLAUDE.md loses" list (sections to drop in Task 4) — covered implicitly: Task 4's CLAUDE.md content omits everything in that list.

- **Placeholder scan:**
  - Tasks 1, 2, 3 each give the full document content as a fenced markdown block. The note "Replace the escaped backtick fences (\`\`) with real triple-backticks when writing the file" appears in each — that's prescriptive instruction, not a placeholder.
  - No "TBD" / "TODO" / "implement later" anywhere.
  - Task 4 step 4's reading-test gives three concrete questions to trace; the engineer answers them by reading their own doc, not by following a placeholder.

- **Type/method consistency:**
  - Filenames: `docs/auth.md`, `docs/clickhouse.md`, `docs/operations.md` — used consistently across Tasks 1-5.
  - The Evolution section's bullet format (`- **YYYY-MM-DD** — summary → docs/superpowers/specs/<spec>.md`) — used consistently in Tasks 1, 2.
  - Spec filenames in Evolution sections cross-checked against `ls docs/superpowers/specs/`. Tasks 1 step 4 and Task 2 step 4 each include a verification pass.
  - The "See also" block in CLAUDE.md (Task 4) lists exactly the topic docs created in Tasks 1-3.

- **Order check:** Tasks 1, 2, 3 are independent and can be reordered or parallelized; each commits cleanly. Task 4 depends on Tasks 1-3 (it links to them). Task 5 is verification. Plan calls this out in "Order of operations" preamble.
