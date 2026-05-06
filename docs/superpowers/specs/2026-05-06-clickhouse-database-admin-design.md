# Per-database admin (ClickHouse) — design

**Date:** 2026-05-06
**Status:** draft, pending review

## Problem

Today iris has two ClickHouse authorization tiers:

- Any logged-in user can run impersonated read queries via `ClickHouseHandle`.
- The `clickhouse_admin` role grants full power via `ClickHouseAdminHandle` — service-identity queries, all DDL, all audit.

Real deployments need a middle tier. Some users should be able to **create databases** without being global CH admins. The user who creates a database needs to manage **read access** to it: grant or revoke `SELECT` on the database to other users, manage row policies, and delegate the same admin rights to other users or roles. Their authority is **scoped to the databases they own** — they can't touch other databases.

## Non-goals

- Schema-level grants for non-`SELECT` permissions (`INSERT`, `ALTER UPDATE`, `DELETE`). Write grants stay with global `clickhouse_admin` for now.
- Row policies attached to iris roles. Row policies attach to a CH user role (`<username>_USER`) or a CH group role (`<group>_GRP`); iris-role-level row policies aren't in scope.
- Per-table admin (admin of `db.table_a` but not `db.table_b`). The unit of admin authority is the database.
- HTTP routes. The spec ships the deps + handles + storage; routes are example-grade only.

## Architecture

Two separable concepts:

1. **`clickhouse_database_creator` role** — gates the "create a new database" route. New role, modeled in the existing `authz_*` mapping. Global admins can include it in their role definition if they want; the bootstrap doesn't link them.
2. **Per-database admin** — per-`(database, grantee)` records in two new tables. Grantees are usernames or iris roles. The dep checks: is the session's user (or any of their effective roles) listed for the database? If yes → admin. Global admins (`clickhouse_admin`) short-circuit to admin-of-everything.

```
src/iris/auth/authz/
└── bootstrap.py    MODIFIED  also creates the empty clickhouse_database_creator role

src/iris/clickhouse/
├── deps.py                    MODIFIED  + require_clickhouse_database_creator, require_clickhouse_database_admin
├── handle.py                  MODIFIED  + ClickHouseDatabaseCreatorHandle, ClickHouseDatabaseAdminHandle
├── database_admins.py    NEW  DatabaseAdminStore — schema + 8 mutators + is_admin
└── install.py                 MODIFIED  build DatabaseAdminStore + register on app.state + close hook
```

`iris.clickhouse.__init__` re-exports the new public surface.

## Bootstrap addition

`install_authz_schema` (auth/authz/bootstrap.py) already creates `admin` and `clickhouse_admin` on first install. Add `clickhouse_database_creator`:

```python
# Inside install_authz_schema, after the existing INSERTs:
conn.execute("INSERT INTO authz_roles(name) VALUES (?)", ("clickhouse_database_creator",))
```

The bootstrap admin role does NOT include `clickhouse_database_creator` by default — global admin and DB creator are deliberately separable. Operators add the include via the mutator API if they want global admins to also create databases.

The bootstrap test gains one assertion: after first install, `authz_roles` contains `admin`, `clickhouse_admin`, AND `clickhouse_database_creator`.

## DatabaseAdminStore (schema + API)

New tables in `AUTH_DB_PATH`, prefixed `clickhouse_*` to namespace away from auth's `authz_*`:

```sql
CREATE TABLE IF NOT EXISTS clickhouse_database_admins_users (
    database_name  TEXT NOT NULL,
    username_lower TEXT NOT NULL,
    PRIMARY KEY (database_name, username_lower)
);

CREATE TABLE IF NOT EXISTS clickhouse_database_admins_roles (
    database_name  TEXT NOT NULL,
    role_name      TEXT NOT NULL,
    PRIMARY KEY (database_name, role_name)
);

CREATE INDEX IF NOT EXISTS idx_ch_db_admins_users_user ON clickhouse_database_admins_users(username_lower);
CREATE INDEX IF NOT EXISTS idx_ch_db_admins_roles_role ON clickhouse_database_admins_roles(role_name);
```

No FK to `authz_roles` from `clickhouse_database_admins_roles` — keeps `iris.clickhouse` from pulling in cross-package schema dependencies. App-side validation: `add_admin_role` checks the role exists in the authz mapping (passes through `app.state.authz_store.get_mapping()`) before insert.

The store class lives in `src/iris/clickhouse/database_admins.py`:

```python
class DatabaseAdminStore:
    def __init__(self, *, path: str) -> None: ...

    def bootstrap(self) -> None:
        """Create schema. Idempotent. No content seeding (the creator
        flow seeds individual rows when databases are created)."""

    async def is_admin(
        self, *, database: str, username_lower: str, roles: frozenset[str]
    ) -> bool:
        """Short-circuits True if 'clickhouse_admin' in roles. Otherwise
        looks up the username and intersects roles with the per-DB admin
        roles table."""

    async def add_admin_user(self, *, database: str, username: str) -> None
    async def remove_admin_user(self, *, database: str, username: str) -> None
    async def add_admin_role(self, *, database: str, role: str) -> None
    async def remove_admin_role(self, *, database: str, role: str) -> None
    async def list_admin_users(self, *, database: str) -> list[str]
    async def list_admin_roles(self, *, database: str) -> list[str]

    async def close(self) -> None
```

`add_admin_*` use `INSERT OR IGNORE` for idempotence. `add_admin_user` lowercases the username on storage. Database names and usernames are validated through `validate_identifier` (the existing helper) before reaching the DB. The store opens its own `sqlite3.Connection` in `__init__` (separate from `SessionStore` and `RoleMappingStore`); `bootstrap()` creates the schema. `close()` is registered for lifespan teardown.

`add_admin_role` doesn't validate the role exists in the authz mapping — that check is in the **handle method** (`ClickHouseDatabaseAdminHandle.add_admin_role`), which has access to `RoleMappingStore`. The store-level method is a thin SQL wrapper; cross-package validation lives one layer up.

## Handles

### `ClickHouseDatabaseCreatorHandle`

Minimal surface. Created inside `require_clickhouse_database_creator` after the role gate passes.

```python
class ClickHouseDatabaseCreatorHandle:
    def __init__(
        self,
        *,
        client: Client,
        settings: ClickHouseSettings,
        db_admin_store: DatabaseAdminStore,
        username: str,
    ) -> None: ...

    async def create_database(self, name: str) -> None:
        """CREATE DATABASE IF NOT EXISTS <quoted_name>; record the
        creating user as admin of the new database. Idempotent: if the
        DB already exists in CH and the admin record already exists in
        SQLite, the call is a no-op.
        """
```

Implementation:

```python
validate_identifier(name, kind="database")
quoted = quote_identifier(name, kind="database")
await asyncio.to_thread(self._client.command, f"CREATE DATABASE IF NOT EXISTS {quoted}")
await self._db_admin_store.add_admin_user(database=name, username=self._username)
```

The two operations are NOT a transaction — they hit different systems (CH then SQLite). The CH `CREATE DATABASE IF NOT EXISTS` is naturally idempotent; the SQLite `INSERT OR IGNORE` is too. So a partial failure (CH succeeds, SQLite fails because of e.g. disk full) leaves the system recoverable: operator can retry the call, or a global admin can manually `INSERT` into `clickhouse_database_admins_users`.

### `ClickHouseDatabaseAdminHandle`

Per-database scope. Created inside `require_clickhouse_database_admin` after the dep verifies the session admins the database (or has `clickhouse_admin`).

```python
class ClickHouseDatabaseAdminHandle:
    def __init__(
        self,
        *,
        client: Client,
        http_client: httpx.AsyncClient,
        settings: ClickHouseSettings,
        db_admin_store: DatabaseAdminStore,
        authz_store: RoleMappingStore,
        database: str,
        username: str,
    ) -> None: ...

    # Read grants on this database
    async def grant_select_to_user(self, username: str) -> None
    async def revoke_select_from_user(self, username: str) -> None
    async def grant_select_to_group(self, group: str) -> None
    async def revoke_select_from_group(self, group: str) -> None

    # Row policies on tables in this database
    async def add_row_policy_for_user(
        self, *, table: str, column: str, username: str, value: str
    ) -> None
    async def revoke_row_policy_for_user(
        self, *, table: str, column: str, username: str, value: str
    ) -> None
    async def add_row_policy_for_group(
        self, *, table: str, column: str, group: str, value: str
    ) -> None
    async def revoke_row_policy_for_group(
        self, *, table: str, column: str, group: str, value: str
    ) -> None

    # Delegate admin to others
    async def add_admin_user(self, username: str) -> None
    async def remove_admin_user(self, username: str) -> None
    async def add_admin_role(self, role: str) -> None
    async def remove_admin_role(self, role: str) -> None

    # Audit
    async def list_admin_users(self) -> list[str]
    async def list_admin_roles(self) -> list[str]
    async def list_grants(self) -> list[dict[str, Any]]
    async def list_row_policies(self) -> list[dict[str, Any]]
```

**Translation from iris terms to CH role names:**

- `grant_select_to_user(username)` → `GRANT SELECT ON <db>.* TO <username>{USER_ROLE_SUFFIX}` (i.e., `<username>_USER`).
- `grant_select_to_group(group)` → `GRANT SELECT ON <db>.* TO <group>{GROUP_ROLE_SUFFIX}` (i.e., `<group>_GRP`).

Both delegate to the existing `iris.clickhouse.grants.grant_select_to_database(client, database=self._database, role=...)`. The handle methods are thin wrappers that compute the CH role name from the iris-friendly identifier.

**Row policies** delegate to the existing `iris.clickhouse.policies.add_row_policy(...)` with `database=self._database`. The role is computed from the iris-friendly identifier: `f"{username}{USER_ROLE_SUFFIX}"` for the per-user variants, `f"{group}{GROUP_ROLE_SUFFIX}"` for the per-group variants. Mirrors the `grant_select_to_user` / `grant_select_to_group` split. Iris-role-scoped row policies aren't supported (operators wanting that map roles to groups in the authz mapping and use the group variant).

**Admin delegation:**

- `add_admin_user(username)` calls `db_admin_store.add_admin_user(database=self._database, username=username)`.
- `add_admin_role(role)` first verifies the role exists in the authz mapping by calling `authz_store.get_mapping()` — raises `RoleMappingError` if the role isn't defined — then calls `db_admin_store.add_admin_role(database=self._database, role=role)`.

**Audit methods** read from CH's `system.grants` and `system.row_policies` filtered by `database = self._database`. They return raw `list[dict]` (consistent with the existing audit functions on `ClickHouseAdminHandle`). Service-identity queries via the existing `clickhouse-connect` client.

**Pre-existing target user constraint.** When `grant_select_to_user(alice)` runs and alice has never logged in, `<alice>_USER` doesn't exist in CH and the `GRANT` fails. The handle catches `clickhouse_connect.driver.exceptions.DatabaseError` and re-raises as `RoleMappingError(f"target user {alice!r} has not logged in yet — they must authenticate at least once before grants can be applied")`. Documented; same error class iris already uses.

The same constraint applies to `grant_select_to_group(group)` — the `<group>_GRP` role only exists if at least one user with that group has logged in. The error message is parallel.

## Deps

```python
async def require_clickhouse_database_creator(
    request: Request, session: Session, mapping: CurrentMapping
) -> ClickHouseDatabaseCreatorHandle:
    if "clickhouse_database_creator" not in mapping.roles:
        raise AuthorizationMisconfigured("clickhouse_database_creator")
    if "clickhouse_database_creator" not in session.roles:
        raise AuthForbidden(
            needed=("clickhouse_database_creator",),
            have=tuple(sorted(session.roles)),
        )
    return ClickHouseDatabaseCreatorHandle(
        client=request.app.state.clickhouse_client,
        settings=request.app.state.clickhouse_settings,
        db_admin_store=request.app.state.clickhouse_database_admins,
        username=session.user.username,
    )


async def require_clickhouse_database_admin(
    request: Request, database: str, session: Session
) -> ClickHouseDatabaseAdminHandle:
    """`database` is bound from the calling route's path/query params by FastAPI."""
    validate_identifier(database, kind="database")
    db_admin_store: DatabaseAdminStore = request.app.state.clickhouse_database_admins
    if not await db_admin_store.is_admin(
        database=database,
        username_lower=session.user.username.lower(),
        roles=session.roles,
    ):
        raise AuthForbidden(
            needed=(f"admin of database {database!r}",),
            have=tuple(sorted(session.roles)),
        )
    return ClickHouseDatabaseAdminHandle(
        client=request.app.state.clickhouse_client,
        http_client=request.app.state.clickhouse_http_client,
        settings=request.app.state.clickhouse_settings,
        db_admin_store=db_admin_store,
        authz_store=request.app.state.authz_store,
        database=database,
        username=session.user.username,
    )
```

`require_clickhouse_database_admin` declares `database: str` as a regular FastAPI parameter; FastAPI binds it from the calling route's path or query params automatically. The dep does NOT take `mapping: CurrentMapping` — `is_admin` works off `session.roles` (the closure-resolved role set already on the session view) plus the username, no mapping fetch needed.

`is_admin`'s `clickhouse_admin` short-circuit (inside `DatabaseAdminStore.is_admin`) gives global admins admin of every database without any per-DB row.

### Example routes

Illustrative only — the spec doesn't ship these.

```python
@app.post("/clickhouse/databases/{database}")
async def create_database(
    database: str,
    handle: ClickHouseDatabaseCreatorHandle = Depends(require_clickhouse_database_creator),
):
    await handle.create_database(database)
    return {"created": database}


@app.post("/clickhouse/databases/{database}/grants/users/{username}")
async def grant_read(
    database: str,
    username: str,
    handle: ClickHouseDatabaseAdminHandle = Depends(require_clickhouse_database_admin),
):
    await handle.grant_select_to_user(username)
    return {"granted": True}


@app.post("/clickhouse/databases/{database}/admins/users/{username}")
async def delegate_admin(
    database: str,
    username: str,
    handle: ClickHouseDatabaseAdminHandle = Depends(require_clickhouse_database_admin),
):
    await handle.add_admin_user(username)
    return {"ok": True}
```

The dep's `database: str` parameter shares its name with the route's path param — FastAPI binds them by name.

## Wiring

`iris.clickhouse.install(app)` (`src/iris/clickhouse/install.py`) gains:

```python
from iris.clickhouse.database_admins import DatabaseAdminStore

# After clickhouse_client / http_client / settings are stashed:
db_admin_store = DatabaseAdminStore(path=settings.auth_db_path)  # NEW field — see below
db_admin_store.bootstrap()
app.state.clickhouse_database_admins = db_admin_store
app.state.clickhouse_close_database_admins = db_admin_store.close
```

Wait — `iris.clickhouse.ClickHouseSettings` doesn't currently have an `auth_db_path` field. The path comes from `iris.auth.config.AuthSettings.auth_db_path`. Two options:

1. Have `iris.clickhouse.install(app)` reach back into `iris.auth.config.AuthSettings.from_env()` (re-reads env). Cheap; ~consistent with how iris.auth re-reads inside its own install.
2. Plumb the path through `app.state` from auth's install. `app.state.auth_db_path` set in `iris.auth.routes.install`; read here.

Recommendation: option 2. Stash `app.state.auth_db_path` once during `iris.auth.routes.install`, and `iris.clickhouse.install` reads it. Avoids duplicate `from_env()` calls. Adds one line to auth's install:

```python
# In iris.auth.routes.install, after settings = AuthSettings.from_env():
app.state.auth_db_path = settings.auth_db_path
```

`_lifespan` in `src/iris/app.py` adds the close call at the end of the existing teardown chain:

```python
db_admin_closer = getattr(app.state, "clickhouse_close_database_admins", None)
if db_admin_closer is not None:
    await db_admin_closer()
```

## Configuration

No new env vars. Operators control everything via:

- The authz mutator API: `add_user_to_role("clickhouse_database_creator", username)` to grant creator powers.
- The DB-admin mutator API (per-DB handle methods, or direct `DatabaseAdminStore` calls): `add_admin_user(database, username)` to grant per-DB admin.

The bootstrap admin (configured via `AUTHZ_BOOTSTRAP_USER`) does NOT get `clickhouse_database_creator` automatically — operators decide.

## Error handling

| Failure | Behavior |
|---|---|
| Route declares `database: str`, request omits it | FastAPI 422 (validation error) |
| `database` not a valid CH identifier | `InvalidIdentifierError` from `validate_identifier`; routes 500 unless they catch |
| `clickhouse_database_creator` role not defined in authz | `AuthorizationMisconfigured` → 500 |
| User lacks `clickhouse_database_creator` | `AuthForbidden` → 403 |
| User isn't admin of the requested database | `AuthForbidden` → 403 |
| `add_admin_role(role)` for an undefined authz role | `RoleMappingError` raised by the handle — route 500s unless caught |
| `grant_select_to_user(alice)` when alice has never logged in | `RoleMappingError` with a clear "user has not logged in yet" message |
| CH `CREATE DATABASE` fails (e.g., disk full) | clickhouse-connect exception propagates; route 500s. SQLite admin row not written, so retry is safe. |
| SQLite `INSERT OR IGNORE` fails after CH `CREATE DATABASE` succeeds | clickhouse-connect exception bubbles up. The DB exists in CH but no admin record. Operator (global admin) recovers manually. Documented. |

## Public surface

`iris.clickhouse.__init__` adds:

```python
from iris.clickhouse.database_admins import DatabaseAdminStore
from iris.clickhouse.deps import (
    require_clickhouse_database_admin,
    require_clickhouse_database_creator,
)
from iris.clickhouse.handle import (
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
)
```

Plus the constants `CLICKHOUSE_DATABASE_CREATOR_ROLE = "clickhouse_database_creator"` (mirrors the existing `CLICKHOUSE_ADMIN_ROLE`).

## Testing

Six new test files plus a one-line addition to the bootstrap test.

### `tests/clickhouse/test_database_admin_store.py`

Tempfile DB, no testcontainer. Covers the eight `DatabaseAdminStore` methods:

- `add_admin_user` round-trip + idempotence + lowercasing.
- `remove_admin_user` is a no-op for unknown rows.
- `add_admin_role` round-trip + idempotence.
- `remove_admin_role` is a no-op for unknown rows.
- `is_admin` returns True for direct username match (case-insensitive).
- `is_admin` returns True for role match (intersects `roles` parameter with admin-roles table).
- `is_admin` returns True if `clickhouse_admin` is in `roles` (short-circuit, no DB lookup).
- `is_admin` returns False otherwise.
- `list_admin_users` / `list_admin_roles` return the rows.
- `close` is idempotent.

### `tests/clickhouse/test_database_creator_handle.py`

Mocked Client + mocked `DatabaseAdminStore`. Covers:

- `create_database` issues `CREATE DATABASE IF NOT EXISTS \`name\`` to the client.
- `create_database` calls `db_admin_store.add_admin_user(database=name, username=session_user)`.
- `create_database` rejects invalid names via `validate_identifier`.
- Idempotence: calling `create_database` twice doesn't add duplicate admin rows (via store's `INSERT OR IGNORE`).

### `tests/clickhouse/test_database_admin_handle.py`

Mocked Client + httpx + DatabaseAdminStore + RoleMappingStore. Covers all handle methods:

- `grant_select_to_user("alice")` issues `GRANT SELECT ON \`db\`.* TO \`alice_USER\``.
- `grant_select_to_group("admins")` issues `GRANT SELECT ON \`db\`.* TO \`admins_GRP\``.
- `revoke_select_*` issues `REVOKE`.
- `add_row_policy_for_user(table=t, column=c, username=u, value=v)` calls the underlying `iris.clickhouse.policies.add_row_policy` with `database=self._database` and `role=f"{u}_USER"`.
- `add_row_policy_for_group(table=t, column=c, group=g, value=v)` calls the underlying `iris.clickhouse.policies.add_row_policy` with `database=self._database` and `role=f"{g}_GRP"`.
- `revoke_row_policy_for_*` mirror the add path through `iris.clickhouse.policies.revoke_row_policy`.
- `add_admin_user` / `add_admin_role` delegate to the store.
- `add_admin_role` verifies the role exists in the authz mapping; raises `RoleMappingError` if not.
- `list_*` audit methods round-trip.

### `tests/clickhouse/test_database_admin_deps.py`

FastAPI-app-level unit tests with dep overrides (similar to `tests/clickhouse/test_clickhouse_deps.py`). Covers:

- `require_clickhouse_database_creator` 500s when the role isn't defined.
- `require_clickhouse_database_creator` 403s when the user lacks it.
- `require_clickhouse_database_creator` returns a `ClickHouseDatabaseCreatorHandle` for an admitted user.
- `require_clickhouse_database_admin` 403s when the user isn't admin of the requested database.
- `require_clickhouse_database_admin` admits a user listed in the per-DB admins table.
- `require_clickhouse_database_admin` admits a user via a role listed in the per-DB admin-roles table.
- `require_clickhouse_database_admin` admits a user with the `clickhouse_admin` role for ANY database (short-circuit).
- `require_clickhouse_database_admin` 422s when the route doesn't supply a `database` parameter.

### `tests/clickhouse/test_database_admin_integration.py`

End-to-end against the testcontainer:

- A non-admin user has `clickhouse_database_creator`. They `POST /clickhouse/databases/foo` → DB created, admin row recorded.
- The same user then `POST /clickhouse/databases/foo/grants/users/alice` → alice gets `SELECT` on `foo.*`.
- Alice (logged in separately) successfully `SELECT * FROM foo.t1` (via `query_as_user`).
- A different user (no admin rights on `foo`) gets 403 attempting any of the admin operations.

### `tests/clickhouse/test_database_admin_login_provisioning.py`

Bridge test: verifies that `grant_select_to_user(alice)` BEFORE alice logs in fails with the documented `RoleMappingError`; after alice logs in once, the same call succeeds.

### Bootstrap test addition (`tests/auth/authz/test_authz_bootstrap.py`)

Existing `test_first_install_seeds_admin_with_clickhouse_admin_include` asserts:

```python
assert roles == {"admin", "clickhouse_admin"}
```

Updated to:

```python
assert roles == {"admin", "clickhouse_admin", "clickhouse_database_creator"}
```

Plus a new test confirming the bootstrap admin does NOT include `clickhouse_database_creator` (operators opt in).

## Open follow-ups (not in this spec)

- HTTP routes exposing the creator and per-DB admin handles. The example routes above are illustrative only.
- Pre-provisioning of CH users (creating `<username>_USER` ahead of the first login). Useful for "set up access before the user joins" workflows; deferred until a real use case appears.
- Per-DB admin grants for write operations (`INSERT`, `ALTER UPDATE`). Currently global admins only.
- A `list_databases_i_admin()` audit method on the bare `Session`. Useful for UIs that want to show "your databases" navigation; not in scope.
- A `transfer_database(name, new_creator_username)` helper for handing over admin ownership. Trivially scriptable via `add_admin_user` + `remove_admin_user`; deferred unless it becomes a recurring need.
