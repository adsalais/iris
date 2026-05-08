# ClickHouse

`iris.clickhouse` provisions CH users/roles/grants/policies and provides standalone async `*_impl` functions called by Session subclasses in `iris.auth.identity`. The plain-data helpers (`audit.py`, `bootstrap.py`, `client.py`, `grants.py`, `policies.py`, `users.py`, `rights.py`) are independent of `iris.auth`. Only `install.py` imports from auth. Reference `CLAUDE.md` for project overview, `docs/auth.md` for the auth side.

## Public surface

`__all__` in `src/iris/clickhouse/__init__.py` is the source of truth. Notable exports:

- **Settings and client:** `ClickHouseSettings`, `build_client`
- **Bootstrap:** `bootstrap_admin`, `GLOBAL_ADMIN_ROLE`
- **User provisioning:** `init_user_rights`, `derive_rights`
- **Tier-role helpers:** `create_tier_roles`, `drop_tier_roles`, `tier_role_name`, `grant_tier_to_user`, `grant_tier_to_group`, `revoke_tier_from_user`, `revoke_tier_from_group`, `TIER_DBADMIN`, `TIER_DBWRITER`, `TIER_DBREADER`
- **Row policies:** `add_row_policy`, `revoke_row_policy`
- **Audit helpers:** `user_grants`, `role_grants`, `user_role_memberships`, `user_row_policies`, `role_row_policies`, `table_row_policies`
- `grant_select_to_database`, `grant_insert_update_to_table`

`install` is intentionally NOT re-exported from this package. Callers (only `iris.app:build_app`) do `from iris.clickhouse.install import install` to break the module-load cycle.

`build_client(settings)` returns a `clickhouse_connect.driver.client.Client`. Operations take that client as their first argument:

```python
settings = ClickHouseSettings.from_env()
client = build_client(settings)
bootstrap_admin(client, admin_user="alice", admin_group="iris_admin")  # idempotent startup
init_user_rights(client, username="alice", groups=["sales"], settings=settings)
add_row_policy(client, database="orders", table="lines",
               column="region", role="sales_GRP", value="EU")
```

## Conventions

- Per-user role: `<username>_USER` (suffix hardcoded at `users.USER_ROLE_SUFFIX`).
- Per-group role: `<group>_GRP` (suffix hardcoded at `users.GROUP_ROLE_SUFFIX`).
- Tier roles: `<database>_DBADMIN`, `<database>_DBWRITER`, `<database>_DBREADER`.
- Sentinel: `iris_global_admin` — carries no privileges of its own; wildcard row policies attach to it.
- Restrictive row-policy name: `<database>_<table>_<role>_<slug>_<8charhash>` — slug strips non-`[a-zA-Z0-9_]` characters; the 8-character hash disambiguates collisions like `EU/UK` vs `EU UK`.
- Wildcard row-policy names: `<database>_<table>_iris_global_admin` and `<database>_<table>_<database>_DBADMIN`.
- All operations are idempotent: re-running is safe. `init_user_rights` reconciles group memberships (revokes `_GRP` roles no longer in the input, grants the new ones).

## DDL safety

`identifiers.py` is the single safety contract. External-source strings (usernames from auth, db/table/column names from callers) flow through `validate_identifier` (rejects anything outside `[a-zA-Z0-9_]+`) and `quote_identifier` (validates + backticks). Row-policy values use `quote_string` for SQL literal escaping. DDL is built from these helpers; `client.command()` runs it without parameter binding. DML (audit `SELECT`s) uses ClickHouse's native `{name:Type}` placeholder syntax via `client.query(..., parameters=...)`.

## Per-tier methods and route examples

Routes import a Session alias from `iris.auth.deps` (not from `iris.clickhouse`). Each alias admits callers meeting its privilege requirement and returns a Session subclass whose method surface matches the tier. The Session value carries both the admission decision and the CH-method surface — there is no separate handle parameter.

| Alias | Admits | Returns | Selected methods |
|---|---|---|---|
| `Session` | any logged-in user | `AuthSession` | `query_as_user(sql, database=None)` |
| `SessionOptional` | any caller (None if no session) | `AuthSession \| None` | same as `AuthSession`, or `None` |
| `SessionRead` | user has read access to `database` (path param) | `DatabaseSession` | `query_as_user(sql)` auto-scoped to `self.database` |
| `SessionWrite` | user has write access to `database` (path param) | `DatabaseSession` | `query_as_user(sql)` auto-scoped to `self.database` |
| `SessionDatabaseCreator` | `rights.is_admin` or `rights.can_create_database` | `DatabaseCreatorSession` | `create_database(name)` |
| `SessionDatabaseAdmin` | user is admin of `database` (path param) | `DatabaseAdminSession` | `grant_reader/writer`, `add_admin_user`, `revoke_reader/writer`, `remove_admin_user`, `grant_reader_to_group/writer_to_group`, `add_admin_group`, `revoke_reader_from_group/writer_from_group`, `remove_admin_group`, `delete_database()`, `list_admin_members()`, `list_grants()`, `list_row_policies()` |
| `SessionAdmin` | `rights.is_admin` | `AdminSession` | `query_as_service`, `reprovision_user`, `grant_select_to_database`, `grant_insert_update_to_table`, `add_row_policy`, `revoke_row_policy`, `user_grants`, `role_grants`, `user_role_memberships`, `user_row_policies`, `role_row_policies`, `table_row_policies` |

`SessionRead` and `SessionWrite` bind `database` from the route's path parameter. `DatabaseSession.query_as_user` does not accept a `database=` kwarg — the bound `self.database` is the source of truth. To query a different database from a DB-scoped route, use a fully-qualified table name. For routes that need to query a specific database from a non-DB-scoped session (`Session` or `SessionAdmin`), `AuthSession.query_as_user` and `AdminSession.query_as_service` both accept a `database=` kwarg.

Global admins also satisfy `SessionDatabaseAdmin` (via the `is_admin` superset), so routes gated by `SessionDatabaseAdmin` admit both per-DB admins and global admins. Routes that need both global ops and per-DB ops may compose two Session parameters; this is rare.

**Why two HTTP transports.** `query_as_user` prepends `EXECUTE AS <quoted_username>` to the SQL. ClickHouse's `EXECUTE AS user <SELECT>` body grammar rejects `FORMAT` clauses, but `clickhouse-connect`'s `query()` always appends `FORMAT Native` — incompatible. Session methods therefore use a separate `httpx.AsyncClient` for impersonated queries, posting to ClickHouse's HTTP endpoint with `?default_format=JSONEachRow` as a URL parameter. Service-identity queries (`query_as_service`) and admin/audit methods keep using `clickhouse-connect`. As a consequence, `query_as_user` returns `list[dict[str, Any]]` (parsed JSON Lines) rather than a `QueryResult` — JSON encoding preserves value types but column-type metadata is lost. Named parameters work via `param_<name>=<value>` URL params translated from the `parameters=` kwarg.

Example routes:

```python
from iris.auth import Session, SessionRead, SessionDatabaseAdmin, SessionAdmin

@app.get("/db/{database}/count")
async def count(database: str, session: SessionRead):
    return await session.query_as_user("SELECT count() FROM t")

@app.post("/db/{database}/grants/users/{username}")
async def grant_read(database: str, username: str, session: SessionDatabaseAdmin):
    await session.grant_reader(username)
    return {"granted": True}

@app.post("/db/{database}/admins/users/{username}")
async def delegate_admin(database: str, username: str, session: SessionDatabaseAdmin):
    await session.add_admin_user(username)
    return {"ok": True}

@app.get("/admin/users/{username}/grants")
async def audit(username: str, session: SessionAdmin):
    return await session.user_grants(username=username)
```

**Database lifecycle.** `create_database(name)` on a `DatabaseCreatorSession` does three things atomically: `CREATE DATABASE IF NOT EXISTS`, creates the three tier roles (`<name>_DBADMIN`, `<name>_DBWRITER`, `<name>_DBREADER`) with their privilege grants via `create_tier_roles`, and grants `<name>_DBADMIN` to the creator's `<creator>_USER` role. All steps idempotent. `delete_database()` on a `DatabaseAdminSession` reverses: `DROP DATABASE IF EXISTS` then drops the three tier roles via `drop_tier_roles`.

## Bootstrap

`bootstrap_admin(client, *, admin_user=None, admin_group=None)` always creates the `iris_global_admin` sentinel role (no privileges of its own — wildcard row policies attach to it so that every global admin sees all rows).

When `admin_user` is supplied and no role with the `_USER` suffix already holds the admin marker (ROLE ADMIN at global scope with grant_option=1), it creates `<admin_user>_USER`, grants it `ALL ON *.* WITH GRANT OPTION`, and grants `iris_global_admin` to it. The same applies independently for `admin_group` and the `_GRP` suffix.

Both channels are independently idempotent: re-running when an admin role with the matching suffix already exists is a no-op. Detection is scoped to the `_USER`/`_GRP` suffixes so iris's connection identity (the service user) is never mistaken for a bootstrapped admin user.

In production, `CLICKHOUSE_ADMIN_USER` and `CLICKHOUSE_ADMIN_GROUP` env vars drive the bootstrap. Wiping CH and restarting re-triggers both channels.

## Row policies

`add_row_policy(client, database, table, column, role, value)` emits three statements for each call:

1. A restrictive policy on `column` for `role` with `USING column = 'value'` — name is `<database>_<table>_<role>_<slug>_<8charhash>`.
2. A wildcard `USING 1` policy for `iris_global_admin` — name is `<database>_<table>_iris_global_admin`. Created via `CREATE ROW POLICY IF NOT EXISTS` so subsequent calls for the same table are no-ops.
3. A wildcard `USING 1` policy for `<database>_DBADMIN` — name is `<database>_<table>_<database>_DBADMIN`. Same idempotency.

The wildcard policies persist after the last restrictive policy on the table is revoked — they may apply to other restrictive policies on the same table and are intentionally not cleaned up by `revoke_row_policy`.

## Pre-create-on-grant

Tier-grant helpers (`grant_tier_to_user`, `revoke_tier_from_user`, etc.) issue `CREATE ROLE IF NOT EXISTS <target>_USER` before granting. This closes a username enumeration channel: the CH error response is identical whether the target user has logged in or not. Once the target eventually authenticates, `init_user_rights` reuses the existing role and `derive_rights` picks up the tier membership.

## Post-login hook chain

`iris.clickhouse.install(app)` registers a hook on `app.state.post_login_hooks`. The hook fires on every successful login (form submit or OAuth callback) and does three things in order:

1. `init_user_rights` — provisions the CH user/role/group memberships.
2. `derive_rights` — computes the `Rights` view from CH state (transitive role walk + grant inspection).
3. `store.set_rights(session_id, rights)` — persists the `Rights` to the SQLite session row.

Cookie-based session refreshes do NOT re-provision; the cached `Rights` is what every subsequent request sees. Group changes between two logins are reconciled on the next login.

**iris's liveness is tied to ClickHouse's.** This is intentional: iris is a thin layer in front of ClickHouse, and a logged-in user with no ability to reach the data backend can't accomplish anything useful. Rather than hide that with best-effort provisioning, login fails loud when CH is down — operators see the exact failure mode in the access logs, and users get a real error rather than a half-broken session that errors on every subsequent query.

`build_app(install_clickhouse=False)` skips the bridge entirely — used by auth tests that don't need a CH testcontainer. With CH disabled, the post-login hook chain is empty, sessions land with `EMPTY_RIGHTS`, and `client=None`/`http_client=None`. Calling a CH method on such a session raises. Production launches via uvicorn factory mode (`uvicorn.run("iris.app:build_app", factory=True, ...)`), so importing `build_app` is side-effect-free for tests.

## Tests

`testcontainers-python` spins up `clickhouse/clickhouse-server:26.3` in Docker. The container is session-scoped (one instance per pytest run); per-test isolation comes from a UUID-derived `prefix` fixture that namespaces every entity name. Docker is required to run `tests/clickhouse/`.

The `chdb` library was originally trialed for in-process testing; `chdb==4.1.6`'s embedded server hardcodes `system.user_directories` to a read-only `users_xml` entry, blocking all RBAC DDL at runtime. See `docs/superpowers/specs/2026-05-05-clickhouse-authz-design.md` for verification.

## Module map

```
src/iris/clickhouse/
├── __init__.py      # re-exports __all__; install NOT included
├── audit.py         # user_grants, role_grants, user_role_memberships, *_row_policies
├── bootstrap.py     # bootstrap_admin, GLOBAL_ADMIN_ROLE
├── client.py        # build_client
├── config.py        # ClickHouseSettings.from_env()
├── grants.py        # tier constants, create/drop_tier_roles, grant/revoke_tier_*, tier_role_name
├── handle.py        # standalone async *_impl functions called by identity.py Session methods
├── identifiers.py   # validate_identifier, quote_identifier, quote_string
├── install.py       # iris.clickhouse.install(app) — wires post-login hook; NOT re-exported
├── policies.py      # add_row_policy, revoke_row_policy
├── rights.py        # derive_rights (walks system.role_grants + system.grants)
└── users.py         # init_user_rights, USER_ROLE_SUFFIX, GROUP_ROLE_SUFFIX
```

## Evolution

- 2026-05-05 — CH RBAC primitives (users, roles, grants, row policies) → `docs/superpowers/specs/2026-05-05-clickhouse-authz-design.md`
- 2026-05-06 — auth↔CH bridge: handles + post-login provisioning → `docs/superpowers/specs/2026-05-06-auth-clickhouse-bridge-design.md`
- 2026-05-06 — per-database admin tier (initially SQLite-backed) → `docs/superpowers/specs/2026-05-06-clickhouse-database-admin-design.md`
- 2026-05-08 — CH-only authorization, tier roles in CH → `docs/superpowers/specs/2026-05-08-clickhouse-only-authz-design.md`
- 2026-05-08 — session-as-handle: handle classes removed → `docs/superpowers/specs/2026-05-08-session-as-handle-design.md`
- 2026-05-08 — bootstrap rework + iris_global_admin sentinel → `docs/superpowers/specs/2026-05-08-bootstrap-rework-design.md`
