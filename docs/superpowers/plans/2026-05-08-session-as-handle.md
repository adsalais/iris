# Session as Handle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inline ClickHouse handle methods onto a typed Session class hierarchy returned by the existing alias deps, so routes take one parameter per tier and the type system enforces capability bounds.

**Architecture:** Refactor the four `ClickHouse*Handle` classes in `iris.clickhouse.handle` into module-level `*_impl` async functions. Build a Session class hierarchy in `iris.auth.identity` (`AuthSession` base + `DatabaseSession` + `DatabaseAdminSession` + `DatabaseCreatorSession` + `AdminSession`) with frozen dataclass methods that lazy-import and delegate to those `_impl` functions. Update the alias dep resolvers in `iris.auth.deps` to construct the right subclass with CH refs from `request.app.state`. Delete the handle classes and `iris.clickhouse.deps` module entirely. DB-bound sessions auto-scope `query_as_user` via CH's HTTP `?database=` URL parameter.

**Tech Stack:** Python 3.13, FastAPI, frozen dataclasses with `slots=True`, `clickhouse_connect` (sync, wrapped in `asyncio.to_thread`), `httpx.AsyncClient` (for impersonated HTTP queries), pytest with testcontainers.

**Spec:** `docs/superpowers/specs/2026-05-08-session-as-handle-design.md`

---

## File Structure

### New files

None. Everything moves within existing modules.

### Modified files

- `src/iris/clickhouse/handle.py` — replace the four handle classes with module-level async `*_impl` functions taking primitive args. Add `database` URL param to `query_as_user_impl` and pass-through `database` kwarg to `query_as_service_impl`.
- `src/iris/auth/identity.py` — replace the single `AuthSession` dataclass with a hierarchy: `AuthSession` (base, gains `client`/`http_client`/`settings` fields and `query_as_user`), `DatabaseSession` (adds `database`, overrides `query_as_user` to bind self.database), `DatabaseAdminSession` (adds tier-grant/revoke/lifecycle/audit methods), `DatabaseCreatorSession` (adds `create_database`), `AdminSession` (adds `query_as_service`/audit/row-policy/reprovision methods).
- `src/iris/auth/deps.py` — `_require_*` resolvers build the right Session subclass with CH refs injected from `request.app.state`. Add a `_ch_refs(request)` helper. Update alias `Annotated` types to point at the right subclasses.
- `src/iris/auth/__init__.py` — export the new Session subclasses (`DatabaseSession`, `DatabaseAdminSession`, `DatabaseCreatorSession`, `AdminSession`).
- `src/iris/clickhouse/__init__.py` — drop exports of the deleted handle classes (`ClickHouseHandle`, `ClickHouseAdminHandle`, `ClickHouseDatabaseAdminHandle`, `ClickHouseDatabaseCreatorHandle`) and the deleted handle providers (`get_clickhouse_handle`, `require_clickhouse_admin`, `require_clickhouse_database_admin`, `require_clickhouse_database_creator`).
- `tests/auth/test_deps.py` — update assertions to verify the resolver returns the correct Session subclass and that `isinstance(session, DatabaseAdminSession)` holds for `SessionDatabaseAdmin`.
- `tests/auth/test_session_dep.py` — update fixture-construction shape (sessions need CH refs via the dep resolver; tests that construct sessions directly now use the test-only path of mocking `app.state.clickhouse_*`).
- `tests/clickhouse/test_creator_handle.py` — replace `ClickHouseDatabaseCreatorHandle(...)` with `DatabaseCreatorSession(...)` construction; same coverage.
- `tests/clickhouse/test_admin_handle.py` — replace `ClickHouseDatabaseAdminHandle(...)` with `DatabaseAdminSession(...)` construction; same coverage.
- `tests/clickhouse/test_tier_promotion.py` — same refactor.
- `tests/clickhouse/test_handle.py` and `tests/clickhouse/test_handle_integration.py` — replace `ClickHouseHandle(...)`/`ClickHouseAdminHandle(...)` with `AuthSession(...)`/`AdminSession(...)` construction.
- `tests/clickhouse/test_clickhouse_deps.py` — DELETE. The handle-provider deps no longer exist; admission tests for the alias deps already live in `tests/auth/test_deps.py`.
- `tests/clickhouse/test_clickhouse_identifiers.py` — update the `__all__` assertion to reflect the new public surface (no handle classes, no handle providers).
- `CLAUDE.md` — update the "Auth ↔ ClickHouse bridge" and "Per-database admin tier" sections: route examples drop the `handle: ... = Depends(...)` parameter; document that Session subclasses carry the CH methods.

### Deleted files

- `src/iris/clickhouse/deps.py` — the four handle providers go away entirely.

### Order of operations

The refactor has a deliberate breakage window between Tasks 2 and 6 because:
- Task 2 changes `AuthSession`'s field set (adds `client`/`http_client`/`settings`), which forces `iris.auth.deps` resolvers (Task 3), Session-method tests (Tasks 4-5), and handle-class tests (Task 6 deletes them) to all update before the suite is green again.

Plan ordering: Tasks 1-2 land additive changes (compile-clean each). Tasks 3-6 are the breakage window — execute them in one session and commit at the end. Task 7+ are post-breakage cleanup.

---

## Task 1: Add `*_impl` standalone functions to `iris.clickhouse.handle`

Refactor the four handle classes' methods into module-level async functions, while keeping the classes themselves as thin wrappers that delegate. The classes still pass tests; the new functions are now usable from anywhere. This is additive — no callers change yet.

**Files:**
- Modify: `src/iris/clickhouse/handle.py`

- [ ] **Step 1: Read the current file**

Run: `cat src/iris/clickhouse/handle.py`

Skim the four classes. Each method does CH work via `asyncio.to_thread` or via `httpx`. The refactor lifts each method body into a module-level function with primitive arguments.

- [ ] **Step 2: Add module-level standalone functions**

Edit `src/iris/clickhouse/handle.py`. After the existing imports and before the first class definition, insert:

```python
# ---- standalone async functions ----
# Module-level implementations called by Session methods (iris.auth.identity)
# and by the handle classes below (which delegate). The classes are scheduled
# for deletion; the standalone functions are the canonical surface.


async def query_as_user_impl(
    http_client: httpx.AsyncClient,
    *,
    username: str,
    sql: str,
    parameters: Mapping[str, Any] | None = None,
    database: str | None = None,
) -> list[dict[str, Any]]:
    """Run ``sql`` on ClickHouse impersonated as ``username``.

    Sends `EXECUTE AS <username> <sql>` to the CH HTTP endpoint with
    ``default_format=JSONEachRow`` (and ``database=<database>`` when supplied,
    so unqualified table names resolve against that schema).
    """
    body = f"EXECUTE AS {quote_identifier(username, kind='username')} {sql}"
    params: dict[str, str] = {"default_format": "JSONEachRow"}
    if database:
        params["database"] = database
    if parameters:
        for k, v in parameters.items():
            params[f"param_{k}"] = str(v)
    response = await http_client.post("/", params=params, content=body)
    response.raise_for_status()
    text = response.text.strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines() if line]


async def query_as_service_impl(
    client: Client,
    *,
    sql: str,
    parameters: Mapping[str, Any] | None = None,
    database: str | None = None,
) -> QueryResult:
    """Run ``sql`` as the service identity (no impersonation). When
    ``database`` is supplied, clickhouse-connect's ``database=`` kwarg
    sets the default schema for unqualified names."""
    kwargs: dict[str, Any] = {}
    if parameters:
        kwargs["parameters"] = dict(parameters)
    if database:
        kwargs["database"] = database
    return await asyncio.to_thread(client.query, sql, **kwargs)


async def reprovision_user_impl(
    client: Client,
    *,
    username: str,
    groups: list[str],
    settings: ClickHouseSettings,
) -> None:
    await asyncio.to_thread(
        init_user_rights,
        client,
        username=username,
        groups=groups,
        settings=settings,
    )


async def grant_select_to_database_impl(
    client: Client, *, database: str, role: str
) -> None:
    await asyncio.to_thread(
        grant_select_to_database, client, database=database, role=role
    )


async def grant_insert_update_to_table_impl(
    client: Client, *, database: str, table: str, role: str
) -> None:
    await asyncio.to_thread(
        grant_insert_update_to_table,
        client,
        database=database,
        table=table,
        role=role,
    )


async def add_row_policy_impl(
    client: Client,
    *,
    database: str,
    table: str,
    column: str,
    role: str,
    value: str,
    settings: ClickHouseSettings,
) -> None:
    await asyncio.to_thread(
        add_row_policy,
        client,
        database=database,
        table=table,
        column=column,
        role=role,
        value=value,
        settings=settings,
    )


async def revoke_row_policy_impl(
    client: Client,
    *,
    database: str,
    table: str,
    role: str,
    value: str,
) -> None:
    await asyncio.to_thread(
        revoke_row_policy,
        client,
        database=database,
        table=table,
        role=role,
        value=value,
    )


async def user_grants_impl(client: Client, *, username: str) -> list[dict[str, Any]]:
    return await asyncio.to_thread(user_grants, client, username=username)


async def role_grants_impl(client: Client, *, role: str) -> list[dict[str, Any]]:
    return await asyncio.to_thread(role_grants, client, role=role)


async def user_role_memberships_impl(
    client: Client, *, username: str
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(user_role_memberships, client, username=username)


async def user_row_policies_impl(
    client: Client, *, username: str
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(user_row_policies, client, username=username)


async def role_row_policies_impl(
    client: Client, *, role: str
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(role_row_policies, client, role=role)


async def table_row_policies_impl(
    client: Client, *, database: str, table: str
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        table_row_policies, client, database=database, table=table
    )


async def create_database_impl(
    client: Client,
    *,
    settings: ClickHouseSettings,
    name: str,
    creator_username: str,
) -> None:
    """``CREATE DATABASE IF NOT EXISTS`` + tier role lifecycle + grant
    ``DBADMIN`` to the creator's per-user role."""
    validate_identifier(name, kind="database")
    quoted = quote_identifier(name, kind="database")
    await asyncio.to_thread(client.command, f"CREATE DATABASE IF NOT EXISTS {quoted}")
    await asyncio.to_thread(create_tier_roles, client, database=name)
    await asyncio.to_thread(
        grant_tier_to_user,
        client,
        database=name,
        tier=TIER_DBADMIN,
        username=creator_username,
    )


async def grant_reader_impl(
    client: Client, *, database: str, username: str
) -> None:
    await asyncio.to_thread(
        grant_tier_to_user,
        client,
        database=database,
        tier=TIER_DBREADER,
        username=username,
    )


async def grant_writer_impl(
    client: Client, *, database: str, username: str
) -> None:
    await asyncio.to_thread(
        grant_tier_to_user,
        client,
        database=database,
        tier=TIER_DBWRITER,
        username=username,
    )


async def add_admin_user_impl(
    client: Client, *, database: str, username: str
) -> None:
    await asyncio.to_thread(
        grant_tier_to_user,
        client,
        database=database,
        tier=TIER_DBADMIN,
        username=username,
    )


async def revoke_reader_impl(
    client: Client, *, database: str, username: str
) -> None:
    await asyncio.to_thread(
        revoke_tier_from_user,
        client,
        database=database,
        tier=TIER_DBREADER,
        username=username,
    )


async def revoke_writer_impl(
    client: Client, *, database: str, username: str
) -> None:
    await asyncio.to_thread(
        revoke_tier_from_user,
        client,
        database=database,
        tier=TIER_DBWRITER,
        username=username,
    )


async def remove_admin_user_impl(
    client: Client, *, database: str, username: str
) -> None:
    await asyncio.to_thread(
        revoke_tier_from_user,
        client,
        database=database,
        tier=TIER_DBADMIN,
        username=username,
    )


async def grant_reader_to_group_impl(
    client: Client, *, database: str, group: str
) -> None:
    await asyncio.to_thread(
        grant_tier_to_group,
        client,
        database=database,
        tier=TIER_DBREADER,
        group=group,
    )


async def grant_writer_to_group_impl(
    client: Client, *, database: str, group: str
) -> None:
    await asyncio.to_thread(
        grant_tier_to_group,
        client,
        database=database,
        tier=TIER_DBWRITER,
        group=group,
    )


async def add_admin_group_impl(
    client: Client, *, database: str, group: str
) -> None:
    await asyncio.to_thread(
        grant_tier_to_group,
        client,
        database=database,
        tier=TIER_DBADMIN,
        group=group,
    )


async def revoke_reader_from_group_impl(
    client: Client, *, database: str, group: str
) -> None:
    await asyncio.to_thread(
        revoke_tier_from_group,
        client,
        database=database,
        tier=TIER_DBREADER,
        group=group,
    )


async def revoke_writer_from_group_impl(
    client: Client, *, database: str, group: str
) -> None:
    await asyncio.to_thread(
        revoke_tier_from_group,
        client,
        database=database,
        tier=TIER_DBWRITER,
        group=group,
    )


async def remove_admin_group_impl(
    client: Client, *, database: str, group: str
) -> None:
    await asyncio.to_thread(
        revoke_tier_from_group,
        client,
        database=database,
        tier=TIER_DBADMIN,
        group=group,
    )


async def delete_database_impl(client: Client, *, database: str) -> None:
    """``DROP DATABASE IF EXISTS`` then drop the three tier roles."""
    db_q = quote_identifier(database, kind="database")
    await asyncio.to_thread(client.command, f"DROP DATABASE IF EXISTS {db_q}")
    await asyncio.to_thread(drop_tier_roles, client, database=database)


async def list_admin_members_impl(client: Client, *, database: str) -> list[str]:
    """Members of ``<database>_DBADMIN`` — user and group roles."""
    admin_role = tier_role_name(database, TIER_DBADMIN)
    rows = await asyncio.to_thread(
        client.query,
        "SELECT role_name FROM system.role_grants WHERE granted_role_name = {r:String}",
        {"r": admin_role},
    )
    return [cast(str, row["role_name"]) for row in rows.named_results()]


async def list_grants_impl(client: Client, *, database: str) -> list[dict[str, Any]]:
    def _sync() -> list[dict[str, Any]]:
        result = client.query(
            "SELECT * FROM system.grants WHERE database = {d:String}",
            parameters={"d": database},
        )
        return list(result.named_results())

    return await asyncio.to_thread(_sync)


async def list_row_policies_impl(
    client: Client, *, database: str
) -> list[dict[str, Any]]:
    def _sync() -> list[dict[str, Any]]:
        result = client.query(
            "SELECT * FROM system.row_policies WHERE database = {d:String}",
            parameters={"d": database},
        )
        return list(result.named_results())

    return await asyncio.to_thread(_sync)
```

The `cast` and existing helpers (`grant_tier_to_user`, `tier_role_name`, etc.) are already imported in this file. Confirm the imports list at the top still includes them; if anything is missing add the import.

- [ ] **Step 3: Run the existing handle suite to confirm nothing broke**

Run: `uv run pytest tests/clickhouse/test_handle.py tests/clickhouse/test_handle_integration.py tests/clickhouse/test_creator_handle.py tests/clickhouse/test_admin_handle.py tests/clickhouse/test_tier_promotion.py -v`

Expected: same passing tests as before. The classes still exist and still work; we've just added standalone alternatives.

- [ ] **Step 4: Commit**

```bash
git add src/iris/clickhouse/handle.py
git commit -m "$(cat <<'EOF'
feat(clickhouse): add module-level *_impl async functions

Refactor: extract the body of each handle method into a standalone
async function with primitive args. Adds a `database` URL parameter
to query_as_user_impl and a database= kwarg to query_as_service_impl,
which the handle classes don't yet expose but the upcoming Session
classes will use for auto-scoping.

Additive: existing handle classes still exist and pass their tests.
The Session class hierarchy (next task) will replace them.
EOF
)"
```

---

## Task 2: Add Session class hierarchy in `iris.auth.identity`

**Files:**
- Modify: `src/iris/auth/identity.py`

**Important:** This task changes `AuthSession`'s field set, which is the start of the breakage window. The codebase will not compile until Task 6 closes it. Do not commit between Tasks 2 and 6.

- [ ] **Step 1: Replace `src/iris/auth/identity.py`**

```python
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from iris.auth.session import EMPTY_RIGHTS, Rights


@dataclass(frozen=True, slots=True)
class User:
    subject: str
    username: str
    display_name: str
    groups: tuple[str, ...]


@dataclass(slots=True)
class UserSession:
    """Internal mutable session row from the SQLite store.

    Routes consume the request-scoped immutable :class:`AuthSession` view via
    the alias deps in ``iris.auth.deps``. ``UserSession`` is the row shape that
    sliding-TTL refresh operates on.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    absolute_expires_at: datetime
    data: dict[str, Any] = field(default_factory=dict)
    rights: Rights = EMPTY_RIGHTS


@dataclass(frozen=True, slots=True)
class AuthSession:
    """Request-scoped view of a logged-in session, with the ClickHouse
    operations available to the session's tier.

    Built once per request by the auth dep. Routes receive an ``AuthSession``
    (or one of its subclasses) via the ``Annotated`` alias deps in
    ``iris.auth.deps``.

    Frozen except for ``data``: the dict is a per-request snapshot deserialized
    from the SQLite session store. Mutations to the dict do NOT auto-persist —
    call ``await request.app.state.auth_session_store.update_data(session.id,
    session.data)`` to write changes back.

    The ``client`` / ``http_client`` / ``settings`` fields are CH references
    injected by the dep resolver. They are not part of the persistent identity
    (``compare=False``, ``repr=False``) so two sessions with identical
    ``id``/``user``/``rights``/etc. compare equal regardless of which CH
    connections happen to be wired in.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    data: dict[str, Any]
    rights: Rights
    client: Any = field(repr=False, compare=False)
    http_client: Any = field(repr=False, compare=False)
    settings: Any = field(repr=False, compare=False)

    async def query_as_user(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import query_as_user_impl
        return await query_as_user_impl(
            self.http_client,
            username=self.user.username,
            sql=sql,
            parameters=parameters,
            database=database,
        )


@dataclass(frozen=True, slots=True)
class DatabaseSession(AuthSession):
    """Session bound to a specific database (the path/query parameter that
    drove the alias dep). ``query_as_user`` is auto-scoped to ``self.database``;
    no override is provided — to query a different database from a DB-scoped
    route, use a fully-qualified table name and let CH enforce privileges."""
    database: str

    async def query_as_user(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import query_as_user_impl
        return await query_as_user_impl(
            self.http_client,
            username=self.user.username,
            sql=sql,
            parameters=parameters,
            database=self.database,
        )


@dataclass(frozen=True, slots=True)
class DatabaseAdminSession(DatabaseSession):
    """Per-database admin session. Adds tier-grant/revoke/lifecycle/audit
    methods scoped to ``self.database``."""

    async def grant_reader(self, username: str) -> None:
        from iris.clickhouse.handle import grant_reader_impl
        await grant_reader_impl(self.client, database=self.database, username=username)

    async def grant_writer(self, username: str) -> None:
        from iris.clickhouse.handle import grant_writer_impl
        await grant_writer_impl(self.client, database=self.database, username=username)

    async def add_admin_user(self, username: str) -> None:
        from iris.clickhouse.handle import add_admin_user_impl
        await add_admin_user_impl(
            self.client, database=self.database, username=username
        )

    async def revoke_reader(self, username: str) -> None:
        from iris.clickhouse.handle import revoke_reader_impl
        await revoke_reader_impl(
            self.client, database=self.database, username=username
        )

    async def revoke_writer(self, username: str) -> None:
        from iris.clickhouse.handle import revoke_writer_impl
        await revoke_writer_impl(
            self.client, database=self.database, username=username
        )

    async def remove_admin_user(self, username: str) -> None:
        from iris.clickhouse.handle import remove_admin_user_impl
        await remove_admin_user_impl(
            self.client, database=self.database, username=username
        )

    async def grant_reader_to_group(self, group: str) -> None:
        from iris.clickhouse.handle import grant_reader_to_group_impl
        await grant_reader_to_group_impl(
            self.client, database=self.database, group=group
        )

    async def grant_writer_to_group(self, group: str) -> None:
        from iris.clickhouse.handle import grant_writer_to_group_impl
        await grant_writer_to_group_impl(
            self.client, database=self.database, group=group
        )

    async def add_admin_group(self, group: str) -> None:
        from iris.clickhouse.handle import add_admin_group_impl
        await add_admin_group_impl(
            self.client, database=self.database, group=group
        )

    async def revoke_reader_from_group(self, group: str) -> None:
        from iris.clickhouse.handle import revoke_reader_from_group_impl
        await revoke_reader_from_group_impl(
            self.client, database=self.database, group=group
        )

    async def revoke_writer_from_group(self, group: str) -> None:
        from iris.clickhouse.handle import revoke_writer_from_group_impl
        await revoke_writer_from_group_impl(
            self.client, database=self.database, group=group
        )

    async def remove_admin_group(self, group: str) -> None:
        from iris.clickhouse.handle import remove_admin_group_impl
        await remove_admin_group_impl(
            self.client, database=self.database, group=group
        )

    async def delete_database(self) -> None:
        from iris.clickhouse.handle import delete_database_impl
        await delete_database_impl(self.client, database=self.database)

    async def list_admin_members(self) -> list[str]:
        from iris.clickhouse.handle import list_admin_members_impl
        return await list_admin_members_impl(self.client, database=self.database)

    async def list_grants(self) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import list_grants_impl
        return await list_grants_impl(self.client, database=self.database)

    async def list_row_policies(self) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import list_row_policies_impl
        return await list_row_policies_impl(self.client, database=self.database)


@dataclass(frozen=True, slots=True)
class DatabaseCreatorSession(AuthSession):
    """Session that can create new databases. Admits ``rights.is_admin`` or
    ``rights.can_create_database``."""

    async def create_database(self, name: str) -> None:
        from iris.clickhouse.handle import create_database_impl
        await create_database_impl(
            self.client,
            settings=self.settings,
            name=name,
            creator_username=self.user.username,
        )


@dataclass(frozen=True, slots=True)
class AdminSession(AuthSession):
    """Global-admin session. Adds service-identity queries plus audit and
    row-policy operations. For per-database operations, the route should
    use ``SessionDatabaseAdmin`` (which admits admins via the ``is_admin``
    superset and returns a ``DatabaseAdminSession`` bound to the path's
    database)."""

    async def query_as_service(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> Any:  # QueryResult — typed Any to avoid clickhouse-connect import
        from iris.clickhouse.handle import query_as_service_impl
        return await query_as_service_impl(
            self.client, sql=sql, parameters=parameters, database=database
        )

    async def reprovision_user(self, *, username: str, groups: list[str]) -> None:
        from iris.clickhouse.handle import reprovision_user_impl
        await reprovision_user_impl(
            self.client, username=username, groups=groups, settings=self.settings
        )

    async def grant_select_to_database(self, *, database: str, role: str) -> None:
        from iris.clickhouse.handle import grant_select_to_database_impl
        await grant_select_to_database_impl(
            self.client, database=database, role=role
        )

    async def grant_insert_update_to_table(
        self, *, database: str, table: str, role: str
    ) -> None:
        from iris.clickhouse.handle import grant_insert_update_to_table_impl
        await grant_insert_update_to_table_impl(
            self.client, database=database, table=table, role=role
        )

    async def add_row_policy(
        self, *, database: str, table: str, column: str, role: str, value: str
    ) -> None:
        from iris.clickhouse.handle import add_row_policy_impl
        await add_row_policy_impl(
            self.client,
            database=database,
            table=table,
            column=column,
            role=role,
            value=value,
            settings=self.settings,
        )

    async def revoke_row_policy(
        self, *, database: str, table: str, role: str, value: str
    ) -> None:
        from iris.clickhouse.handle import revoke_row_policy_impl
        await revoke_row_policy_impl(
            self.client, database=database, table=table, role=role, value=value
        )

    async def user_grants(self, *, username: str) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import user_grants_impl
        return await user_grants_impl(self.client, username=username)

    async def role_grants(self, *, role: str) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import role_grants_impl
        return await role_grants_impl(self.client, role=role)

    async def user_role_memberships(self, *, username: str) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import user_role_memberships_impl
        return await user_role_memberships_impl(self.client, username=username)

    async def user_row_policies(self, *, username: str) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import user_row_policies_impl
        return await user_row_policies_impl(self.client, username=username)

    async def role_row_policies(self, *, role: str) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import role_row_policies_impl
        return await role_row_policies_impl(self.client, role=role)

    async def table_row_policies(
        self, *, database: str, table: str
    ) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import table_row_policies_impl
        return await table_row_policies_impl(
            self.client, database=database, table=table
        )
```

- [ ] **Step 2: Defer commit**

Codebase will not compile yet — `iris.auth.deps._to_view` constructs `AuthSession` with the old field set. Continue to Task 3.

---

## Task 3: Update `iris.auth.deps` resolvers

**Files:**
- Modify: `src/iris/auth/deps.py`

- [ ] **Step 1: Replace the file**

```python
"""FastAPI dependency aliases for the CH-only authorization model.

Routes consume these as type annotations:

    @app.get("/me")
    async def me(session: Session) -> dict: ...

    @app.get("/db/{database}/read")
    async def read_db(database: str, session: SessionRead) -> ...: ...

Each alias resolves to a Session subclass whose method surface matches the
tier. Resolvers inject the ClickHouse client / httpx client / settings from
``request.app.state`` so session methods can talk to CH.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request

from iris.auth.exceptions import AuthForbidden, AuthRequired
from iris.auth.identity import (
    AdminSession,
    AuthSession,
    DatabaseAdminSession,
    DatabaseCreatorSession,
    DatabaseSession,
    UserSession,
)
from iris.auth.sessions import SessionStore


def set_session_store(app: FastAPI, store: SessionStore) -> None:
    app.state.auth_session_store = store


def set_settings(app: FastAPI, *, cookie_name: str, cookie_secure: bool = True) -> None:
    app.state.auth_cookie_name = cookie_name
    app.state.auth_cookie_secure = cookie_secure


def _get_store(request: Request) -> SessionStore:
    return request.app.state.auth_session_store


def _get_cookie_name(request: Request) -> str:
    return request.app.state.auth_cookie_name


def _ch_refs(request: Request) -> tuple[Any, Any, Any]:
    """Return (clickhouse_client, http_client, settings) — or (None, None, None)
    when CH isn't installed (build_app(install_clickhouse=False)). Sessions
    constructed without CH refs raise on any attempt to call a CH method."""
    state = request.app.state
    return (
        getattr(state, "clickhouse_client", None),
        getattr(state, "clickhouse_http_client", None),
        getattr(state, "clickhouse_settings", None),
    )


async def _resolve_stored(request: Request) -> UserSession | None:
    cookie_name = _get_cookie_name(request)
    sid = request.cookies.get(cookie_name)
    if not sid:
        return None
    store = _get_store(request)
    return await store.get_and_refresh(sid)


_StoredSession = Annotated[UserSession | None, Depends(_resolve_stored)]


def _to_auth_session(stored: UserSession, request: Request) -> AuthSession:
    client, http_client, settings = _ch_refs(request)
    return AuthSession(
        id=stored.id,
        user=stored.user,
        created_at=stored.created_at,
        expires_at=stored.expires_at,
        data=stored.data,
        rights=stored.rights,
        client=client,
        http_client=http_client,
        settings=settings,
    )


async def _optional_session(
    request: Request, stored: _StoredSession
) -> AuthSession | None:
    if stored is None:
        return None
    return _to_auth_session(stored, request)


async def _require_session(
    request: Request, stored: _StoredSession
) -> AuthSession:
    if stored is None:
        raise AuthRequired()
    return _to_auth_session(stored, request)


_RequiredAuth = Annotated[AuthSession, Depends(_require_session)]


async def _require_admin(session: _RequiredAuth) -> AdminSession:
    if not session.rights.is_admin:
        raise AuthForbidden(needed=("admin",), have=())
    return AdminSession(
        id=session.id,
        user=session.user,
        created_at=session.created_at,
        expires_at=session.expires_at,
        data=session.data,
        rights=session.rights,
        client=session.client,
        http_client=session.http_client,
        settings=session.settings,
    )


async def _require_database_creator(
    session: _RequiredAuth,
) -> DatabaseCreatorSession:
    r = session.rights
    if not (r.is_admin or r.can_create_database):
        raise AuthForbidden(needed=("admin", "database_creator"), have=())
    return DatabaseCreatorSession(
        id=session.id,
        user=session.user,
        created_at=session.created_at,
        expires_at=session.expires_at,
        data=session.data,
        rights=session.rights,
        client=session.client,
        http_client=session.http_client,
        settings=session.settings,
    )


async def _require_database_admin(
    database: str, session: _RequiredAuth
) -> DatabaseAdminSession:
    if not session.rights.has_admin(database):
        raise AuthForbidden(needed=(f"database_admin[{database}]",), have=())
    return DatabaseAdminSession(
        id=session.id,
        user=session.user,
        created_at=session.created_at,
        expires_at=session.expires_at,
        data=session.data,
        rights=session.rights,
        client=session.client,
        http_client=session.http_client,
        settings=session.settings,
        database=database,
    )


async def _require_write(database: str, session: _RequiredAuth) -> DatabaseSession:
    if not session.rights.has_write(database):
        raise AuthForbidden(needed=(f"database_writer[{database}]",), have=())
    return DatabaseSession(
        id=session.id,
        user=session.user,
        created_at=session.created_at,
        expires_at=session.expires_at,
        data=session.data,
        rights=session.rights,
        client=session.client,
        http_client=session.http_client,
        settings=session.settings,
        database=database,
    )


async def _require_read(database: str, session: _RequiredAuth) -> DatabaseSession:
    if not session.rights.has_read(database):
        raise AuthForbidden(needed=(f"database_reader[{database}]",), have=())
    return DatabaseSession(
        id=session.id,
        user=session.user,
        created_at=session.created_at,
        expires_at=session.expires_at,
        data=session.data,
        rights=session.rights,
        client=session.client,
        http_client=session.http_client,
        settings=session.settings,
        database=database,
    )


# Public Annotated aliases — what routes consume.
Session = Annotated[AuthSession, Depends(_require_session)]
SessionOptional = Annotated[AuthSession | None, Depends(_optional_session)]
SessionAdmin = Annotated[AdminSession, Depends(_require_admin)]
SessionDatabaseCreator = Annotated[
    DatabaseCreatorSession, Depends(_require_database_creator)
]
SessionDatabaseAdmin = Annotated[
    DatabaseAdminSession, Depends(_require_database_admin)
]
SessionWrite = Annotated[DatabaseSession, Depends(_require_write)]
SessionRead = Annotated[DatabaseSession, Depends(_require_read)]
```

- [ ] **Step 2: Defer commit** — continue to Task 4.

---

## Task 4: Update `tests/auth/test_session_dep.py`

**Files:**
- Modify: `tests/auth/test_session_dep.py`

The tests construct sessions directly via `SessionStore.create()` (returns `UserSession`, internal). The dep resolvers turn them into `AuthSession` views. Now `AuthSession` requires `client` / `http_client` / `settings` fields — the resolver injects `None` when CH isn't on app.state.

For the routes that the test app declares, the alias deps still work because they construct sessions via `_to_auth_session` which already handles `None` refs. No test code change should be needed for the body-of-the-test routes.

- [ ] **Step 1: Confirm by inspection**

Run: `git grep -n 'AuthSession(\|UserSession(' tests/auth/test_session_dep.py`

Expected: no direct `AuthSession(...)` constructions outside the resolver path. If any test constructs `AuthSession` by hand, update it to pass `client=None, http_client=None, settings=None`.

- [ ] **Step 2: If no manual construction, no edit needed. Defer commit.**

If there ARE manual constructions, add `client=None, http_client=None, settings=None` arguments to each call.

---

## Task 5: Update `tests/auth/test_deps.py`

**Files:**
- Modify: `tests/auth/test_deps.py`

The existing tests build a FastAPI app with the alias deps and verify admission. Since `_build_app` uses the `set_session_store`/`set_settings` helpers and the resolvers fall back to `None` for CH refs, no test body change should be needed. But add new tests that verify the resolver returns the right Session subclass.

- [ ] **Step 1: Read the file**

Run: `cat tests/auth/test_deps.py`

- [ ] **Step 2: Add Session-subclass type assertions**

Append new tests at the end of the file:

```python
from iris.auth.identity import (
    AdminSession,
    AuthSession,
    DatabaseAdminSession,
    DatabaseCreatorSession,
    DatabaseSession,
)


def test_session_admin_alias_returns_admin_session():
    rights = Rights(
        is_admin=True,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    app, sid = _build_app(rights)

    @app.get("/_type")
    def probe_type(session: SessionAdmin):
        return {"type": type(session).__name__}

    r = _client(app, sid).get("/_type")
    assert r.status_code == 200
    assert r.json()["type"] == "AdminSession"


def test_session_database_admin_alias_returns_database_admin_session():
    rights = Rights(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset({"finance"}),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    app, sid = _build_app(rights)

    @app.get("/db/{database}/_type")
    def probe_type(database: str, session: SessionDatabaseAdmin):
        return {"type": type(session).__name__, "database": session.database}

    r = _client(app, sid).get("/db/finance/_type")
    assert r.status_code == 200
    assert r.json() == {"type": "DatabaseAdminSession", "database": "finance"}


def test_session_read_alias_returns_database_session():
    rights = Rights(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset({"hr"}),
    )
    app, sid = _build_app(rights)

    @app.get("/db/{database}/_type")
    def probe_type(database: str, session: SessionRead):
        return {"type": type(session).__name__, "database": session.database}

    r = _client(app, sid).get("/db/hr/_type")
    assert r.status_code == 200
    assert r.json() == {"type": "DatabaseSession", "database": "hr"}


def test_session_alias_returns_plain_auth_session():
    rights = Rights(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    app, sid = _build_app(rights)

    @app.get("/_type")
    def probe_type(session: Session):
        return {"type": type(session).__name__}

    r = _client(app, sid).get("/_type")
    assert r.status_code == 200
    assert r.json()["type"] == "AuthSession"
```

The existing imports at the top of the file already include `Session`, `SessionAdmin`, etc. and `Rights`. Add the new ones from `iris.auth.identity` to that block (just `AdminSession`, `DatabaseAdminSession`, `DatabaseSession` — the others aren't used yet).

- [ ] **Step 3: Defer commit**

---

## Task 6: Update CH handle tests + delete `iris.clickhouse.deps` + update `__init__.py`

**Files:**
- Modify: `tests/clickhouse/test_creator_handle.py`
- Modify: `tests/clickhouse/test_admin_handle.py`
- Modify: `tests/clickhouse/test_tier_promotion.py`
- Modify: `tests/clickhouse/test_handle.py`
- Modify: `tests/clickhouse/test_handle_integration.py`
- Modify: `tests/clickhouse/test_clickhouse_identifiers.py`
- Delete: `tests/clickhouse/test_clickhouse_deps.py`
- Delete: `src/iris/clickhouse/deps.py`
- Modify: `src/iris/clickhouse/__init__.py`
- Modify: `src/iris/clickhouse/handle.py` (remove the four classes; standalone functions stay)
- Modify: `src/iris/auth/__init__.py`

This task closes the breakage window. Execute in one session and commit at the end.

- [ ] **Step 1: Update `tests/clickhouse/test_creator_handle.py`**

Replace `ClickHouseDatabaseCreatorHandle(...)` constructions with `DatabaseCreatorSession(...)`. The test fixture's `ch_client` and `ch_settings` are wired identically.

```python
"""Tests for DatabaseCreatorSession.create_database against the CH testcontainer.

Verifies that ``create_database`` provisions the database, the three tier
roles, and grants DBADMIN to the calling user — the lifecycle spelled out
in the spec under "CH-side state".
"""
import asyncio
from datetime import UTC, datetime, timedelta

from iris.auth.identity import DatabaseCreatorSession, User
from iris.auth.session import EMPTY_RIGHTS


def _session_for(user: str, *, ch_client, ch_settings) -> DatabaseCreatorSession:
    now = datetime.now(UTC)
    u = User(subject=f"mock:{user}", username=user, display_name=user, groups=())
    return DatabaseCreatorSession(
        id="sid",
        user=u,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        data={},
        rights=EMPTY_RIGHTS,
        client=ch_client,
        http_client=None,  # creator session doesn't use http_client
        settings=ch_settings,
    )


def test_create_database_creates_db_and_tier_roles(ch_client, ch_settings, prefix):
    user = f"{prefix}_creator"
    db = f"{prefix}_owned"
    session = _session_for(user, ch_client=ch_client, ch_settings=ch_settings)
    asyncio.run(session.create_database(db))

    # database exists
    rows = ch_client.query(
        "SELECT count() FROM system.databases WHERE name = {n:String}",
        parameters={"n": db},
    ).result_rows
    assert rows[0][0] == 1

    # three tier roles exist
    role_rows = ch_client.query(
        "SELECT name FROM system.roles WHERE name LIKE {p:String}",
        parameters={"p": f"{db}\\_DB%"},
    ).result_rows
    role_names = {r[0] for r in role_rows}
    assert role_names == {f"{db}_DBADMIN", f"{db}_DBWRITER", f"{db}_DBREADER"}

    # creator's _USER role got the DBADMIN tier
    user_role = f"{user}_USER"
    granted = ch_client.query(
        """
        SELECT granted_role_name FROM system.role_grants
        WHERE role_name = {r:String}
        """,
        parameters={"r": user_role},
    ).result_rows
    assert any(g[0] == f"{db}_DBADMIN" for g in granted)


def test_create_database_idempotent(ch_client, ch_settings, prefix):
    user = f"{prefix}_creator2"
    db = f"{prefix}_idemp"
    session = _session_for(user, ch_client=ch_client, ch_settings=ch_settings)
    asyncio.run(session.create_database(db))
    asyncio.run(session.create_database(db))  # must not raise
```

- [ ] **Step 2: Update `tests/clickhouse/test_admin_handle.py`**

Replace `ClickHouseDatabaseAdminHandle(...)` constructions with `DatabaseAdminSession(...)`:

```python
"""Tests for DatabaseAdminSession: tier grants/revokes plus delete_database."""
import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from iris.auth.identity import DatabaseAdminSession, DatabaseCreatorSession, User
from iris.auth.session import EMPTY_RIGHTS
from iris.clickhouse.rights import derive_rights


def _admin_session(
    ch_client, ch_settings, *, database: str, username: str
) -> DatabaseAdminSession:
    now = datetime.now(UTC)
    u = User(subject=f"mock:{username}", username=username, display_name=username, groups=())
    http_client = httpx.AsyncClient(
        base_url="http://stub",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"")),
    )
    return DatabaseAdminSession(
        id="sid",
        user=u,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        data={},
        rights=EMPTY_RIGHTS,
        client=ch_client,
        http_client=http_client,
        settings=ch_settings,
        database=database,
    )


def _creator_session(
    ch_client, ch_settings, *, username: str
) -> DatabaseCreatorSession:
    now = datetime.now(UTC)
    u = User(subject=f"mock:{username}", username=username, display_name=username, groups=())
    return DatabaseCreatorSession(
        id="sid",
        user=u,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        data={},
        rights=EMPTY_RIGHTS,
        client=ch_client,
        http_client=None,
        settings=ch_settings,
    )


def test_grant_reader_writer_admin_propagate_to_rights(ch_client, ch_settings, prefix):
    creator = f"{prefix}_creator"
    target = f"{prefix}_target"
    db = f"{prefix}_admin_grants"
    asyncio.run(
        _creator_session(ch_client, ch_settings, username=creator).create_database(db)
    )
    admin = _admin_session(ch_client, ch_settings, database=db, username=creator)

    asyncio.run(admin.grant_reader(target))
    r = derive_rights(ch_client, username=target, groups=[])
    assert db in r.db_reader

    asyncio.run(admin.grant_writer(target))
    r = derive_rights(ch_client, username=target, groups=[])
    assert db in r.db_writer

    asyncio.run(admin.add_admin_user(target))
    r = derive_rights(ch_client, username=target, groups=[])
    assert db in r.db_admin


def test_revoke_clears_label(ch_client, ch_settings, prefix):
    creator = f"{prefix}_c"
    target = f"{prefix}_t"
    db = f"{prefix}_revoke_admin"
    asyncio.run(
        _creator_session(ch_client, ch_settings, username=creator).create_database(db)
    )
    admin = _admin_session(ch_client, ch_settings, database=db, username=creator)
    asyncio.run(admin.grant_reader(target))
    asyncio.run(admin.revoke_reader(target))
    r = derive_rights(ch_client, username=target, groups=[])
    assert db not in r.db_reader


def test_delete_database_drops_tier_roles_and_db(ch_client, ch_settings, prefix):
    creator = f"{prefix}_c"
    db = f"{prefix}_to_drop"
    asyncio.run(
        _creator_session(ch_client, ch_settings, username=creator).create_database(db)
    )
    admin = _admin_session(ch_client, ch_settings, database=db, username=creator)
    asyncio.run(admin.delete_database())

    db_rows = ch_client.query(
        "SELECT count() FROM system.databases WHERE name = {n:String}",
        parameters={"n": db},
    ).result_rows
    assert db_rows[0][0] == 0

    role_rows = ch_client.query(
        "SELECT count() FROM system.roles WHERE name LIKE {p:String}",
        parameters={"p": f"{db}\\_DB%"},
    ).result_rows
    assert role_rows[0][0] == 0


def test_list_admin_members_returns_creator(ch_client, ch_settings, prefix):
    creator = f"{prefix}_c"
    db = f"{prefix}_members"
    asyncio.run(
        _creator_session(ch_client, ch_settings, username=creator).create_database(db)
    )
    admin = _admin_session(ch_client, ch_settings, database=db, username=creator)
    members = asyncio.run(admin.list_admin_members())
    assert f"{creator}_USER" in members
```

- [ ] **Step 3: Update `tests/clickhouse/test_tier_promotion.py`**

```python
"""End-to-end tier promotion."""
import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from iris.auth.identity import DatabaseAdminSession, DatabaseCreatorSession, User
from iris.auth.session import EMPTY_RIGHTS
from iris.clickhouse.rights import derive_rights
from iris.clickhouse.users import init_user_rights


def _stub_http():
    return httpx.AsyncClient(
        base_url="http://stub",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"")),
    )


def _user(name: str) -> User:
    return User(subject=f"mock:{name}", username=name, display_name=name, groups=())


def test_creator_grants_writer_promotes_target(ch_client, ch_settings, prefix):
    creator = f"{prefix}_creator"
    bob = f"{prefix}_bob"
    db = f"{prefix}_promo"
    now = datetime.now(UTC)

    creator_s = DatabaseCreatorSession(
        id="sid", user=_user(creator), created_at=now,
        expires_at=now + timedelta(hours=1), data={}, rights=EMPTY_RIGHTS,
        client=ch_client, http_client=None, settings=ch_settings,
    )
    asyncio.run(creator_s.create_database(db))

    init_user_rights(ch_client, username=bob, groups=[], settings=ch_settings)
    bob_rights_before = derive_rights(ch_client, username=bob, groups=[])
    assert db not in bob_rights_before.db_writer

    admin_s = DatabaseAdminSession(
        id="sid", user=_user(creator), created_at=now,
        expires_at=now + timedelta(hours=1), data={}, rights=EMPTY_RIGHTS,
        client=ch_client, http_client=_stub_http(), settings=ch_settings,
        database=db,
    )
    asyncio.run(admin_s.grant_writer(bob))

    bob_rights_after = derive_rights(ch_client, username=bob, groups=[])
    assert db in bob_rights_after.db_writer
    assert db not in bob_rights_after.db_admin
    assert bob_rights_after.has_read(db)
    assert bob_rights_after.has_write(db)
    assert not bob_rights_after.has_admin(db)


def test_creator_is_immediately_db_admin(ch_client, ch_settings, prefix):
    creator = f"{prefix}_solo_creator"
    db = f"{prefix}_solo"
    now = datetime.now(UTC)

    creator_s = DatabaseCreatorSession(
        id="sid", user=_user(creator), created_at=now,
        expires_at=now + timedelta(hours=1), data={}, rights=EMPTY_RIGHTS,
        client=ch_client, http_client=None, settings=ch_settings,
    )
    asyncio.run(creator_s.create_database(db))
    init_user_rights(ch_client, username=creator, groups=[], settings=ch_settings)
    rights = derive_rights(ch_client, username=creator, groups=[])
    assert db in rights.db_admin
    assert rights.has_admin(db)
    assert rights.has_write(db)
    assert rights.has_read(db)
```

- [ ] **Step 4: Update `tests/clickhouse/test_handle.py`**

Read it first: `cat tests/clickhouse/test_handle.py`

For each test that constructs `ClickHouseHandle(...)` or `ClickHouseAdminHandle(...)`, replace with `AuthSession(...)` or `AdminSession(...)` constructions. Field shape (and the Session method names like `query_as_user`/`query_as_service`) match. The CH refs (`client`/`http_client`/`settings`) are positional fields after the standard auth fields.

Pattern:

```python
# OLD
handle = ClickHouseHandle(client=ch_client, http_client=stub_http, username="alice")

# NEW
session = AuthSession(
    id="sid", user=User("mock:alice", "alice", "Alice", ()),
    created_at=now, expires_at=now + timedelta(hours=1), data={}, rights=EMPTY_RIGHTS,
    client=ch_client, http_client=stub_http, settings=ch_settings,
)
# call session.query_as_user(...) instead of handle.query_as_user(...)
```

Same for `ClickHouseAdminHandle` → `AdminSession`. The standalone `query_as_user_impl` / `query_as_service_impl` functions are also testable directly (without constructing a session) — for the lower-level tests, switch to calling those:

```python
from iris.clickhouse.handle import query_as_user_impl

rows = await query_as_user_impl(
    stub_http_client, username="alice", sql="SELECT 1",
)
```

Choose the form that maps cleanly per test. Aim for minimal mechanical change.

- [ ] **Step 5: Update `tests/clickhouse/test_handle_integration.py`**

Same pattern as Step 4: handle classes → Session subclasses, or call standalone `*_impl` functions directly when they fit better.

- [ ] **Step 6: Delete `tests/clickhouse/test_clickhouse_deps.py`**

```bash
git rm tests/clickhouse/test_clickhouse_deps.py
```

The handle-provider deps (which this file tested) no longer exist.

- [ ] **Step 7: Update `tests/clickhouse/test_clickhouse_identifiers.py`**

The expected `__all__` set drops the four handle classes and the four handle providers, drops `init_user_rights` if it was there only via deps. Read the current expected set; remove these names if present:

```
"ClickHouseAdminHandle",
"ClickHouseDatabaseAdminHandle",
"ClickHouseDatabaseCreatorHandle",
"ClickHouseHandle",
"get_clickhouse_handle",
"require_clickhouse_admin",
"require_clickhouse_database_admin",
"require_clickhouse_database_creator",
```

The remaining `__all__` is the new public surface listed in the next step.

- [ ] **Step 8: Delete `src/iris/clickhouse/deps.py`**

```bash
git rm src/iris/clickhouse/deps.py
```

- [ ] **Step 9: Update `src/iris/clickhouse/handle.py` — drop the four classes**

Edit the file: keep all the standalone `*_impl` functions added in Task 1, plus the existing imports. Delete the `class ClickHouseHandle`, `class ClickHouseAdminHandle`, `class ClickHouseDatabaseCreatorHandle`, `class ClickHouseDatabaseAdminHandle` blocks entirely.

The module's docstring should be updated to reflect the new shape:

```python
"""Standalone async ClickHouse operations.

Each ``*_impl`` function takes primitive arguments (``client``, ``http_client``,
``username``, etc.) and runs one CH operation. The Session classes in
``iris.auth.identity`` are the only callers; lazy-importing the functions
inside method bodies avoids an import cycle (``iris.auth → iris.clickhouse``
would cycle if these were imported at module load).

Two transport stories: ``query_as_user_impl`` posts to ClickHouse's HTTP
endpoint via ``httpx`` so we can prepend ``EXECUTE AS <user>`` without
clickhouse-connect rewriting the body with ``FORMAT Native``. Everything
else uses ``clickhouse-connect`` via ``asyncio.to_thread``.
"""
```

- [ ] **Step 10: Update `src/iris/clickhouse/__init__.py`**

Replace:

```python
"""ClickHouse provisioning, audit helpers, and FastAPI bridge.

Public surface — see ``CLAUDE.md`` for usage. ``iris.clickhouse`` no longer
hosts FastAPI handle providers; the Session subclasses in ``iris.auth.identity``
carry the per-tier method surface.
"""

from iris.clickhouse.audit import (
    role_grants,
    role_row_policies,
    table_row_policies,
    user_grants,
    user_role_memberships,
    user_row_policies,
)
from iris.clickhouse.bootstrap import ensure_service_admin
from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.grants import (
    TIER_DBADMIN,
    TIER_DBREADER,
    TIER_DBWRITER,
    create_tier_roles,
    drop_tier_roles,
    grant_insert_update_to_table,
    grant_select_to_database,
    grant_tier_to_group,
    grant_tier_to_user,
    revoke_tier_from_group,
    revoke_tier_from_user,
    tier_role_name,
)
from iris.clickhouse.install import install
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
from iris.clickhouse.rights import derive_rights
from iris.clickhouse.users import init_user_rights

__all__ = [
    "ClickHouseSettings",
    "TIER_DBADMIN",
    "TIER_DBREADER",
    "TIER_DBWRITER",
    "add_row_policy",
    "build_client",
    "create_tier_roles",
    "derive_rights",
    "drop_tier_roles",
    "ensure_service_admin",
    "grant_insert_update_to_table",
    "grant_select_to_database",
    "grant_tier_to_group",
    "grant_tier_to_user",
    "init_user_rights",
    "install",
    "revoke_row_policy",
    "revoke_tier_from_group",
    "revoke_tier_from_user",
    "role_grants",
    "role_row_policies",
    "table_row_policies",
    "tier_role_name",
    "user_grants",
    "user_role_memberships",
    "user_row_policies",
]
```

- [ ] **Step 11: Update `src/iris/auth/__init__.py`**

Replace:

```python
from iris.auth.bootstrap import bootstrap_admin
from iris.auth.deps import (
    Session,
    SessionAdmin,
    SessionDatabaseAdmin,
    SessionDatabaseCreator,
    SessionOptional,
    SessionRead,
    SessionWrite,
)
from iris.auth.identity import (
    AdminSession,
    AuthSession,
    DatabaseAdminSession,
    DatabaseCreatorSession,
    DatabaseSession,
    User,
)
from iris.auth.routes import install
from iris.auth.session import EMPTY_RIGHTS, Rights

__all__ = [
    "AdminSession",
    "AuthSession",
    "DatabaseAdminSession",
    "DatabaseCreatorSession",
    "DatabaseSession",
    "EMPTY_RIGHTS",
    "Rights",
    "Session",
    "SessionAdmin",
    "SessionDatabaseAdmin",
    "SessionDatabaseCreator",
    "SessionOptional",
    "SessionRead",
    "SessionWrite",
    "User",
    "bootstrap_admin",
    "install",
]
```

- [ ] **Step 12: Verify**

Run:

```bash
uv run pytest --ignore=tests/auth/integration
uv run basedpyright --level error
```

Both should pass / show 0 errors.

If any failures point at test files that still construct handle classes by name, update them to construct the matching Session subclass.

- [ ] **Step 13: Commit the breakage-window batch**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(auth+clickhouse): inline CH handle methods onto Session subclasses

AuthSession is now the base of a five-class hierarchy
(AuthSession, DatabaseSession, DatabaseAdminSession,
DatabaseCreatorSession, AdminSession). Each class exposes exactly
the CH operations its tier permits. The ClickHouseHandle classes
and iris.clickhouse.deps handle providers are deleted.

Routes drop the second `handle: ... = Depends(...)` parameter — the
session value carries both admission and capability. DB-bound
sessions auto-scope query_as_user via CH's HTTP ?database= URL
parameter.
EOF
)"
```

---

## Task 7: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the `Auth ↔ ClickHouse bridge` section**

Find the existing section under `## ClickHouse → Auth ↔ ClickHouse bridge` (around line 426 after the recent migration). Replace its body. The new content:

```markdown
### Auth ↔ ClickHouse bridge

Routes consume one alias dep per tier. The dep returns a Session subclass whose
methods match the tier exactly:

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
```

`SessionRead` returns a `DatabaseSession` bound to the path's `database` —
`query_as_user("SELECT count() FROM t")` resolves `t` against `<database>`
because the impersonated request includes `?database=<database>` in the URL.
There is no separate handle parameter; the Session value carries both the
admission decision and the CH-method surface for the tier.

For routes that need to query a specific database from a non-DB-scoped
session (`Session` or `SessionAdmin`), `query_as_user` accepts a `database=`
kwarg. `SessionAdmin.query_as_service` likewise accepts `database=`.
```

- [ ] **Step 2: Update the `Per-database admin tier` section**

Find the existing Per-database admin tier section. Update the route examples to use Session subclasses without separate handles:

```markdown
### Per-database admin tier

Per-database admin is a CH role membership: a user is admin of database `X` iff
their effective role set includes `<X>_DBADMIN`. The Session aliases map to
tier-typed Session subclasses:

| Tier | Alias | Returns | Selected methods |
|---|---|---|---|
| Any logged-in user | `Session` | `AuthSession` | `query_as_user(sql, database=None)` |
| Database creator | `SessionDatabaseCreator` | `DatabaseCreatorSession` | `create_database(name)` |
| Per-database admin | `SessionDatabaseAdmin` | `DatabaseAdminSession` (bound to `database` from path) | `grant_reader/writer`, `add_admin_user`, `revoke_*`, `delete_database`, `list_admin_members`, `list_grants`, `list_row_policies` |
| Global admin | `SessionAdmin` | `AdminSession` | `query_as_service`, `reprovision_user`, `add/revoke_row_policy`, audit (`user_grants`, `role_grants`, …) |

Routes:

```python
@app.post("/clickhouse/databases/{database}")
async def create_database(database: str, session: SessionDatabaseCreator):
    await session.create_database(database)
    return {"created": database}

@app.post("/clickhouse/databases/{database}/grants/users/{username}")
async def grant_read(database: str, username: str, session: SessionDatabaseAdmin):
    await session.grant_reader(username)
    return {"granted": True}

@app.delete("/clickhouse/databases/{database}")
async def delete_db(database: str, session: SessionDatabaseAdmin):
    await session.delete_database()
    return {"deleted": database}
```

A global admin who needs to do per-DB operations writes routes gated by
`SessionDatabaseAdmin` (which admits admins via the `is_admin` superset and
returns a `DatabaseAdminSession` bound to the path's database). Routes that
need both global ops and per-DB ops compose two parameters; this is rare.
```

- [ ] **Step 3: Verify CLAUDE.md doesn't leak deleted symbols**

Run:

```bash
git grep -n 'ClickHouseHandle\|ClickHouseAdminHandle\|ClickHouseDatabaseAdminHandle\|ClickHouseDatabaseCreatorHandle\|get_clickhouse_handle\|require_clickhouse_admin\|require_clickhouse_database_admin\|require_clickhouse_database_creator\|clickhouse\.deps' CLAUDE.md
```

Expected: empty (no surviving references). If any remain, edit them away.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE): session-as-handle: routes drop the handle parameter"
```

---

## Task 8: Final verification

**Files:** none.

- [ ] **Step 1: Full test suite**

Run: `uv run pytest -vv`
Expected: all tests pass, including the integration tier (`tests/auth/integration/`).

- [ ] **Step 2: Type check (errors)**

Run: `uv run basedpyright --level error`
Expected: 0 errors.

- [ ] **Step 3: Type check (warnings)**

Run: `uv run basedpyright --level warning`
Expected: 0 warnings (project gate).

- [ ] **Step 4: Lint**

Run: `uv run ruff check`
Expected: only the documented `E402` in `src/iris/__init__.py`.

- [ ] **Step 5: Smoke-test via TestClient**

Run:

```bash
uv run python <<'EOF'
import os
os.environ.setdefault("AUTH_METHOD", "mock")
os.environ.setdefault("MOCK_USERNAME", "alice")
os.environ.setdefault("MOCK_PASSWORD", "secret")
os.environ.setdefault("MOCK_GROUPS", "admins,users")
os.environ.setdefault("AUTH_DB_PATH", ":memory:")

from fastapi.testclient import TestClient
from iris.app import build_app
from iris.auth.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD

app = build_app(install_clickhouse=False)
c = TestClient(app)

r = c.get("/login")
csrf = r.cookies[CSRF_COOKIE_NAME]
r = c.post("/login", data={CSRF_FORM_FIELD: csrf, "username": "alice", "password": "secret", "next": "/"})
assert r.status_code == 200, r.status_code

r = c.get("/api/whoami")
assert r.status_code == 200
print("whoami OK:", r.json()["display_name"])
EOF
```

Expected: `whoami OK: Alice`. Routes still work after the refactor.

- [ ] **Step 6: Final commit if any fixes were made**

```bash
git add -A
git commit -m "chore: final cleanup after session-as-handle refactor"
```

If no changes are needed, skip this step.

---

## Self-Review

- **Spec coverage check:**
  - "Class hierarchy" — Task 2.
  - "Alias-to-type mapping" — Tasks 2, 3.
  - "Database scoping for query_as_user" — Tasks 1 (impl), 2 (DatabaseSession override).
  - "Module placement" — Tasks 2, 6 (deletes, exports).
  - "Refactoring the CH handle implementations" — Task 1.
  - "Session class shape" — Task 2.
  - "Dep resolver shape" — Task 3.
  - "Route examples" — Task 7 (CLAUDE.md).
  - "Failure modes" — covered by inheriting the existing `AuthRequired`/`AuthForbidden` exception flow; no task adds new exception handlers.
  - "Module map" — Task 6 deletes match the spec's "Deleted" list.
  - "Tests" — Tasks 4, 5, 6 cover the test refactor; old `test_clickhouse_deps.py` is deleted.
  - "Migration / rollout" — N/A (no code).
  - "Open risks" — operator-facing, not a task.

- **Placeholder scan:** Step 4 in Task 6 says "Choose the form that maps cleanly per test. Aim for minimal mechanical change" — that's prescriptive guidance, not a placeholder, and the patterns above show both forms with code. Step 1/2/3 in Task 7 reference "around line 426" for orientation but the full replacement text is provided. No "TBD"/"TODO" left.

- **Type/method consistency:**
  - Method names: `query_as_user`, `query_as_service`, `grant_reader`, `grant_writer`, `add_admin_user`, `revoke_reader`, `revoke_writer`, `remove_admin_user`, group equivalents (`grant_reader_to_group`, etc.), `delete_database`, `list_admin_members`, `list_grants`, `list_row_policies`, `create_database`, `reprovision_user`, `grant_select_to_database`, `grant_insert_update_to_table`, `add_row_policy`, `revoke_row_policy`, `user_grants`, `role_grants`, `user_role_memberships`, `user_row_policies`, `role_row_policies`, `table_row_policies` — used consistently across Tasks 1, 2, 6.
  - Standalone function names: each method has a matching `*_impl` (`query_as_user_impl`, `grant_reader_impl`, etc.) — used consistently between Task 1 (definition) and Task 2 (callers).
  - Field names on Session classes: `id`, `user`, `created_at`, `expires_at`, `data`, `rights`, `client`, `http_client`, `settings` (and `database` on DB-scoped subclasses) — used consistently across Tasks 2, 3, 6.

- **Order check:** Tasks 1, 7, 8 commit cleanly. Tasks 2-6 form the breakage window; commit only at end of Task 6. Plan flags this in the "Order of operations" preamble and at each affected task.
