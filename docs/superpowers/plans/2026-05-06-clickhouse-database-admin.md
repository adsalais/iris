# Per-database ClickHouse admin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-database admin tier between any-logged-in-user and global `clickhouse_admin`. Some users get `clickhouse_database_creator` and can create databases; the creator becomes the database admin and can grant/revoke read access, manage row policies, and delegate admin to other users or roles.

**Architecture:** New `DatabaseAdminStore` (SQLite, same `AUTH_DB_PATH` file) holds per-`(database, user)` and per-`(database, role)` admin records. Two new handle classes (`ClickHouseDatabaseCreatorHandle`, `ClickHouseDatabaseAdminHandle`) wrap CH operations with iris-friendly identifiers. Two new FastAPI deps gate routes: `require_clickhouse_database_creator` (role-gated) and `require_clickhouse_database_admin` (per-database, takes `database` as a path/query param). Global admins (`clickhouse_admin`) short-circuit to admin-of-everything.

**Tech Stack:** Python 3.13, stdlib `sqlite3`, FastAPI, `clickhouse-connect`, `httpx`, pytest, `testcontainers-python`.

**Spec:** `docs/superpowers/specs/2026-05-06-clickhouse-database-admin-design.md`.

---

## File Structure

NEW files:

| Path | Responsibility |
|---|---|
| `src/iris/clickhouse/database_admins.py` | `DatabaseAdminStore` class: schema + `is_admin` + 6 mutators + `list_*` + `close`. |
| `tests/clickhouse/test_database_admin_store.py` | Unit tests for `DatabaseAdminStore`. |
| `tests/clickhouse/test_database_creator_handle.py` | Unit tests for `ClickHouseDatabaseCreatorHandle`. |
| `tests/clickhouse/test_database_admin_handle.py` | Unit tests for `ClickHouseDatabaseAdminHandle`. |
| `tests/clickhouse/test_database_admin_deps.py` | FastAPI dep unit tests for the two new deps. |
| `tests/clickhouse/test_database_admin_integration.py` | End-to-end against the testcontainer: creator → DB → grant → SELECT. |

MODIFIED files:

| Path | Change |
|---|---|
| `src/iris/auth/authz/bootstrap.py` | Also INSERT `clickhouse_database_creator` into `authz_roles` on first install. |
| `src/iris/clickhouse/grants.py` | Add `revoke_select_from_database(client, *, database, role)`. |
| `src/iris/clickhouse/handle.py` | Add `ClickHouseDatabaseCreatorHandle` and `ClickHouseDatabaseAdminHandle`. |
| `src/iris/clickhouse/deps.py` | Add `CLICKHOUSE_DATABASE_CREATOR_ROLE` constant + `require_clickhouse_database_creator` + `require_clickhouse_database_admin` deps. |
| `src/iris/clickhouse/install.py` | Build `DatabaseAdminStore`; register on `app.state` + close hook. |
| `src/iris/clickhouse/__init__.py` | Re-export the new public surface (handles, deps, store, role constant). |
| `src/iris/auth/routes.py` | Stash `app.state.auth_db_path` so `iris.clickhouse.install` can read it without re-calling `AuthSettings.from_env()`. |
| `src/iris/app.py` | `_lifespan` calls `app.state.clickhouse_close_database_admins`. |
| `tests/clickhouse/test_clickhouse_grants.py` | Add `revoke_select_from_database` test cases. |
| `tests/clickhouse/test_clickhouse_identifiers.py` | Update `test_public_surface_exports_named_symbols` to include the new public symbols. |
| `tests/auth/authz/test_authz_bootstrap.py` | Update the role-set assertions to expect `clickhouse_database_creator` too. |
| `CLAUDE.md` | Document the per-database admin tier. |

---

## Task 1: Bootstrap creates `clickhouse_database_creator` role

**Files:**
- Modify: `src/iris/auth/authz/bootstrap.py`
- Modify: `tests/auth/authz/test_authz_bootstrap.py`

The first-install bootstrap already creates `admin` and `clickhouse_admin`. Add `clickhouse_database_creator` alongside, but do NOT include it in the bootstrap admin role.

- [ ] **Step 1.1: Update the bootstrap test to expect three roles**

In `tests/auth/authz/test_authz_bootstrap.py`, find:

```python
def test_first_install_seeds_admin_with_clickhouse_admin_include(tmp_path: Path):
    conn = _open(tmp_path / "auth.db")
    try:
        install_authz_schema(conn, _StubSettings())

        roles = {
            r["name"] for r in conn.execute("SELECT name FROM authz_roles").fetchall()
        }
        assert roles == {"admin", "clickhouse_admin"}
```

Change the assertion to:

```python
        assert roles == {"admin", "clickhouse_admin", "clickhouse_database_creator"}
```

Also find `test_custom_bootstrap_role_name`:

```python
        assert roles == {"superuser", "clickhouse_admin"}
```

Change to:

```python
        assert roles == {"superuser", "clickhouse_admin", "clickhouse_database_creator"}
```

Add a new test asserting the bootstrap admin does NOT include `clickhouse_database_creator`:

```python
def test_bootstrap_admin_does_not_include_database_creator(tmp_path: Path):
    """Operators decide whether bootstrap admins also create databases."""
    conn = _open(tmp_path / "auth.db")
    try:
        install_authz_schema(conn, _StubSettings())
        includes = conn.execute(
            "SELECT included_role FROM authz_role_includes WHERE role_name = 'admin'"
        ).fetchall()
        included = {r["included_role"] for r in includes}
        assert included == {"clickhouse_admin"}  # NOT clickhouse_database_creator
    finally:
        conn.close()
```

- [ ] **Step 1.2: Run the test to verify it fails**

```
uv run pytest tests/auth/authz/test_authz_bootstrap.py -v
```
Expected: `test_first_install_seeds_admin_with_clickhouse_admin_include` and `test_custom_bootstrap_role_name` fail (set mismatch); `test_bootstrap_admin_does_not_include_database_creator` passes (admin currently has only `clickhouse_admin` as include).

- [ ] **Step 1.3: Update bootstrap.py**

In `src/iris/auth/authz/bootstrap.py`, find:

```python
# Must match iris.clickhouse.deps.CLICKHOUSE_ADMIN_ROLE.
_CLICKHOUSE_ADMIN_ROLE = "clickhouse_admin"
```

Add a sibling constant:

```python
# Must match iris.clickhouse.deps.CLICKHOUSE_ADMIN_ROLE.
_CLICKHOUSE_ADMIN_ROLE = "clickhouse_admin"
# Must match iris.clickhouse.deps.CLICKHOUSE_DATABASE_CREATOR_ROLE.
_CLICKHOUSE_DATABASE_CREATOR_ROLE = "clickhouse_database_creator"
```

Find the seeding block at the bottom of `install_authz_schema`:

```python
    conn.execute("INSERT INTO authz_roles(name) VALUES (?)", (role,))
    conn.execute(
        "INSERT INTO authz_roles(name) VALUES (?)", (_CLICKHOUSE_ADMIN_ROLE,)
    )
    conn.execute(
        "INSERT INTO authz_role_includes(role_name, included_role) VALUES (?, ?)",
        (role, _CLICKHOUSE_ADMIN_ROLE),
    )
    conn.execute(
        "INSERT INTO authz_role_users(role_name, username_lower) VALUES (?, ?)",
        (role, user_lower),
    )
```

Add one INSERT for the creator role between the two existing role INSERTs:

```python
    conn.execute("INSERT INTO authz_roles(name) VALUES (?)", (role,))
    conn.execute(
        "INSERT INTO authz_roles(name) VALUES (?)", (_CLICKHOUSE_ADMIN_ROLE,)
    )
    conn.execute(
        "INSERT INTO authz_roles(name) VALUES (?)",
        (_CLICKHOUSE_DATABASE_CREATOR_ROLE,),
    )
    conn.execute(
        "INSERT INTO authz_role_includes(role_name, included_role) VALUES (?, ?)",
        (role, _CLICKHOUSE_ADMIN_ROLE),
    )
    conn.execute(
        "INSERT INTO authz_role_users(role_name, username_lower) VALUES (?, ?)",
        (role, user_lower),
    )
```

The bootstrap admin's only include is still `clickhouse_admin`. The creator role exists but is empty (no users, no groups, no inclusions).

- [ ] **Step 1.4: Run the bootstrap tests**

```
uv run pytest tests/auth/authz/test_authz_bootstrap.py -v
```
Expected: all tests pass (8: 7 existing + 1 new).

- [ ] **Step 1.5: Run the full suite**

```
uv run pytest --ignore=tests/auth/integration
```
Expected: all 267 + 1 = 268 tests pass.

- [ ] **Step 1.6: Type-check + commit**

```
uv run basedpyright --level error
git add src/iris/auth/authz/bootstrap.py tests/auth/authz/test_authz_bootstrap.py
git commit -m "feat(authz): bootstrap creates clickhouse_database_creator role

First-install bootstrap now creates three roles: admin (the bootstrap
admin), clickhouse_admin, and clickhouse_database_creator. The
bootstrap admin still includes only clickhouse_admin — operators
decide whether to add clickhouse_database_creator to the admin role
via the mutator API."
```

---

## Task 2: `revoke_select_from_database` helper

**Files:**
- Modify: `src/iris/clickhouse/grants.py`
- Modify: `tests/clickhouse/test_clickhouse_grants.py`

The per-DB admin handle's `revoke_select_*` methods need a `revoke_select_from_database` counterpart to the existing `grant_select_to_database`. Add it to the grants module so both are colocated.

- [ ] **Step 2.1: Inspect the existing test file structure**

```
head -25 tests/clickhouse/test_clickhouse_grants.py
```

Familiarize yourself with the existing fixtures (`ch_client`, `ch_settings`, `prefix` from `tests/clickhouse/conftest.py`).

- [ ] **Step 2.2: Add the failing test**

Append to `tests/clickhouse/test_clickhouse_grants.py`:

```python
def test_revoke_select_from_database_drops_grant(ch_client, ch_settings, prefix) -> None:
    from iris.clickhouse.grants import (
        grant_select_to_database,
        revoke_select_from_database,
    )

    role = f"{prefix}_role"
    db = f"{prefix}_db"
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")

    grant_select_to_database(ch_client, database=db, role=role)
    pre = list(
        ch_client.query(
            "SELECT access_type FROM system.grants WHERE role_name = {r:String} AND database = {d:String}",
            parameters={"r": role, "d": db},
        ).named_results()
    )
    assert any(row["access_type"] == "SELECT" for row in pre), pre

    revoke_select_from_database(ch_client, database=db, role=role)
    post = list(
        ch_client.query(
            "SELECT access_type FROM system.grants WHERE role_name = {r:String} AND database = {d:String}",
            parameters={"r": role, "d": db},
        ).named_results()
    )
    assert not any(row["access_type"] == "SELECT" for row in post), post


def test_revoke_select_from_database_idempotent(ch_client, ch_settings, prefix) -> None:
    from iris.clickhouse.grants import revoke_select_from_database

    role = f"{prefix}_role2"
    db = f"{prefix}_db2"
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")

    # No grant exists; revoke should not raise.
    revoke_select_from_database(ch_client, database=db, role=role)
    revoke_select_from_database(ch_client, database=db, role=role)
```

- [ ] **Step 2.3: Run the test to verify it fails**

```
uv run pytest tests/clickhouse/test_clickhouse_grants.py -v -k revoke
```
Expected: `ImportError: cannot import name 'revoke_select_from_database'`.

- [ ] **Step 2.4: Implement `revoke_select_from_database`**

Append to `src/iris/clickhouse/grants.py`:

```python
def revoke_select_from_database(client: Client, *, database: str, role: str) -> None:
    """``REVOKE SELECT ON <database>.* FROM <role>``. Idempotent (CH no-ops if no grant)."""
    db_q = quote_identifier(database, kind="database")
    role_q = quote_identifier(role, kind="role")
    client.command(f"REVOKE SELECT ON {db_q}.* FROM {role_q}")
```

- [ ] **Step 2.5: Run the tests to verify they pass**

```
uv run pytest tests/clickhouse/test_clickhouse_grants.py -v
```
Expected: all tests pass (existing + 2 new).

- [ ] **Step 2.6: Commit**

```
git add src/iris/clickhouse/grants.py tests/clickhouse/test_clickhouse_grants.py
git commit -m "feat(clickhouse): revoke_select_from_database helper

Counterpart to grant_select_to_database. Idempotent (CH no-ops on
revoke when no grant exists). Used by the upcoming per-database
admin handle."
```

---

## Task 3: `DatabaseAdminStore`

**Files:**
- Create: `src/iris/clickhouse/database_admins.py`
- Create: `tests/clickhouse/test_database_admin_store.py`

The store owns the two new tables (`clickhouse_database_admins_users`, `clickhouse_database_admins_roles`) and exposes the read API + 6 mutators + `list_*`. `is_admin` short-circuits when `clickhouse_admin` is in the role set.

- [ ] **Step 3.1: Write the failing test file**

Create `tests/clickhouse/test_database_admin_store.py`:

```python
"""Unit tests for DatabaseAdminStore.

Tempfile DB. Each test gets a fresh store; teardown closes it.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from iris.clickhouse.database_admins import DatabaseAdminStore


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "auth.db"


@pytest.fixture
def store(store_path):
    s = DatabaseAdminStore(path=str(store_path))
    s.bootstrap()
    yield s
    asyncio.run(s.close())


def test_is_admin_returns_false_on_empty(store):
    assert asyncio.run(
        store.is_admin(database="orders", username_lower="alice", roles=frozenset())
    ) is False


def test_add_admin_user_round_trips(store):
    asyncio.run(store.add_admin_user(database="orders", username="Alice"))
    assert asyncio.run(
        store.is_admin(database="orders", username_lower="alice", roles=frozenset())
    ) is True


def test_add_admin_user_lowercases(store):
    asyncio.run(store.add_admin_user(database="orders", username="ALICE"))
    rows = asyncio.run(store.list_admin_users(database="orders"))
    assert rows == ["alice"]


def test_add_admin_user_idempotent_across_case(store):
    asyncio.run(store.add_admin_user(database="orders", username="Alice"))
    asyncio.run(store.add_admin_user(database="orders", username="ALICE"))
    rows = asyncio.run(store.list_admin_users(database="orders"))
    assert rows == ["alice"]


def test_remove_admin_user(store):
    asyncio.run(store.add_admin_user(database="orders", username="alice"))
    asyncio.run(store.remove_admin_user(database="orders", username="ALICE"))
    rows = asyncio.run(store.list_admin_users(database="orders"))
    assert rows == []


def test_remove_admin_user_unknown_is_noop(store):
    asyncio.run(store.remove_admin_user(database="nope", username="ghost"))


def test_add_admin_role_round_trips(store):
    asyncio.run(store.add_admin_role(database="orders", role="ops"))
    assert asyncio.run(
        store.is_admin(
            database="orders", username_lower="bob", roles=frozenset({"ops"})
        )
    ) is True


def test_add_admin_role_idempotent(store):
    asyncio.run(store.add_admin_role(database="orders", role="ops"))
    asyncio.run(store.add_admin_role(database="orders", role="ops"))
    rows = asyncio.run(store.list_admin_roles(database="orders"))
    assert rows == ["ops"]


def test_remove_admin_role(store):
    asyncio.run(store.add_admin_role(database="orders", role="ops"))
    asyncio.run(store.remove_admin_role(database="orders", role="ops"))
    rows = asyncio.run(store.list_admin_roles(database="orders"))
    assert rows == []


def test_is_admin_role_match(store):
    """Any role in the user's effective set that's listed for the DB grants admin."""
    asyncio.run(store.add_admin_role(database="orders", role="ops"))
    asyncio.run(store.add_admin_role(database="orders", role="leads"))
    assert asyncio.run(
        store.is_admin(
            database="orders", username_lower="x", roles=frozenset({"leads"})
        )
    ) is True


def test_is_admin_short_circuits_clickhouse_admin(store):
    """clickhouse_admin in roles -> admin of every database, no DB query needed."""
    # Note: store has no rows at all for "orders".
    assert asyncio.run(
        store.is_admin(
            database="orders",
            username_lower="x",
            roles=frozenset({"clickhouse_admin"}),
        )
    ) is True


def test_is_admin_isolation_per_database(store):
    asyncio.run(store.add_admin_user(database="orders", username="alice"))
    assert asyncio.run(
        store.is_admin(
            database="reports", username_lower="alice", roles=frozenset()
        )
    ) is False


def test_list_admin_users_per_database(store):
    asyncio.run(store.add_admin_user(database="orders", username="alice"))
    asyncio.run(store.add_admin_user(database="orders", username="bob"))
    asyncio.run(store.add_admin_user(database="reports", username="carol"))
    orders = asyncio.run(store.list_admin_users(database="orders"))
    reports = asyncio.run(store.list_admin_users(database="reports"))
    assert sorted(orders) == ["alice", "bob"]
    assert reports == ["carol"]


def test_close_is_idempotent(store_path):
    s = DatabaseAdminStore(path=str(store_path))
    asyncio.run(s.close())
    asyncio.run(s.close())
```

- [ ] **Step 3.2: Run the test file to verify it fails on import**

```
uv run pytest tests/clickhouse/test_database_admin_store.py -v
```
Expected: `ModuleNotFoundError: No module named 'iris.clickhouse.database_admins'`.

- [ ] **Step 3.3: Create `src/iris/clickhouse/database_admins.py`**

```python
"""Per-database admin store for ClickHouse.

Holds two tables in the auth SQLite DB (same file as sessions and authz_*):

    CREATE TABLE clickhouse_database_admins_users (
        database_name  TEXT NOT NULL,
        username_lower TEXT NOT NULL,
        PRIMARY KEY (database_name, username_lower)
    );
    CREATE TABLE clickhouse_database_admins_roles (
        database_name  TEXT NOT NULL,
        role_name      TEXT NOT NULL,
        PRIMARY KEY (database_name, role_name)
    );

is_admin short-circuits to True when ``clickhouse_admin`` is in the
session's effective roles — global admins admin every database
without a per-DB row.
"""
from __future__ import annotations

import asyncio
import sqlite3

# Must match iris.clickhouse.deps.CLICKHOUSE_ADMIN_ROLE — the global
# admin role short-circuits is_admin without needing a per-DB row.
_GLOBAL_ADMIN_ROLE = "clickhouse_admin"

_DB_ADMIN_SCHEMA = """
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

CREATE INDEX IF NOT EXISTS idx_ch_db_admins_users_user
    ON clickhouse_database_admins_users(username_lower);
CREATE INDEX IF NOT EXISTS idx_ch_db_admins_roles_role
    ON clickhouse_database_admins_roles(role_name);
"""


class DatabaseAdminStore:
    def __init__(self, *, path: str) -> None:
        self._lock = asyncio.Lock()
        self._closed = False
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_pragmas()

    def _init_pragmas(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def bootstrap(self) -> None:
        """Create the schema. Idempotent. Synchronous: called from
        iris.clickhouse.install at app construction (before any request
        loop). With :memory: the same connection used for queries must
        also create the schema, so this method is the only place the
        schema is created."""
        self._conn.executescript(_DB_ADMIN_SCHEMA)

    async def is_admin(
        self, *, database: str, username_lower: str, roles: frozenset[str]
    ) -> bool:
        if _GLOBAL_ADMIN_ROLE in roles:
            return True
        async with self._lock:
            return await asyncio.to_thread(
                self._is_admin_sync, database, username_lower, roles
            )

    def _is_admin_sync(
        self, database: str, username_lower: str, roles: frozenset[str]
    ) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM clickhouse_database_admins_users "
            "WHERE database_name = ? AND username_lower = ?",
            (database, username_lower),
        ).fetchone()
        if row is not None:
            return True
        if not roles:
            return False
        placeholders = ",".join("?" * len(roles))
        row = self._conn.execute(
            f"SELECT 1 FROM clickhouse_database_admins_roles "
            f"WHERE database_name = ? AND role_name IN ({placeholders}) LIMIT 1",
            (database, *sorted(roles)),
        ).fetchone()
        return row is not None

    async def add_admin_user(self, *, database: str, username: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "INSERT OR IGNORE INTO clickhouse_database_admins_users"
                "(database_name, username_lower) VALUES (?, ?)",
                (database, username.lower()),
            )

    async def remove_admin_user(self, *, database: str, username: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "DELETE FROM clickhouse_database_admins_users "
                "WHERE database_name = ? AND username_lower = ?",
                (database, username.lower()),
            )

    async def add_admin_role(self, *, database: str, role: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "INSERT OR IGNORE INTO clickhouse_database_admins_roles"
                "(database_name, role_name) VALUES (?, ?)",
                (database, role),
            )

    async def remove_admin_role(self, *, database: str, role: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "DELETE FROM clickhouse_database_admins_roles "
                "WHERE database_name = ? AND role_name = ?",
                (database, role),
            )

    async def list_admin_users(self, *, database: str) -> list[str]:
        async with self._lock:
            return await asyncio.to_thread(
                lambda: [
                    r["username_lower"]
                    for r in self._conn.execute(
                        "SELECT username_lower FROM clickhouse_database_admins_users "
                        "WHERE database_name = ? ORDER BY username_lower",
                        (database,),
                    ).fetchall()
                ]
            )

    async def list_admin_roles(self, *, database: str) -> list[str]:
        async with self._lock:
            return await asyncio.to_thread(
                lambda: [
                    r["role_name"]
                    for r in self._conn.execute(
                        "SELECT role_name FROM clickhouse_database_admins_roles "
                        "WHERE database_name = ? ORDER BY role_name",
                        (database,),
                    ).fetchall()
                ]
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._conn.close)
```

- [ ] **Step 3.4: Run the tests to verify they pass**

```
uv run pytest tests/clickhouse/test_database_admin_store.py -v
```
Expected: all 14 tests pass.

- [ ] **Step 3.5: Type-check**

```
uv run basedpyright --level error src/iris/clickhouse/database_admins.py tests/clickhouse/test_database_admin_store.py
```
Expected: 0 errors.

- [ ] **Step 3.6: Commit**

```
git add src/iris/clickhouse/database_admins.py tests/clickhouse/test_database_admin_store.py
git commit -m "feat(clickhouse): DatabaseAdminStore for per-DB admin records

Two tables — clickhouse_database_admins_users and
clickhouse_database_admins_roles — with INSERT OR IGNORE mutators and
an is_admin lookup that short-circuits on clickhouse_admin in the
caller's roles. Schema creation is in bootstrap() (separate from
__init__) for the same :memory:-shared-connection reason as
RoleMappingStore."
```

---

## Task 4: `ClickHouseDatabaseCreatorHandle`

**Files:**
- Modify: `src/iris/clickhouse/handle.py`
- Create: `tests/clickhouse/test_database_creator_handle.py`

Minimal handle: one method `create_database(name)` that runs `CREATE DATABASE IF NOT EXISTS` and atomically records the calling user as an admin of the new DB. The two operations target different systems (CH and SQLite), so the spec accepts that a partial failure leaves an orphan; the IF NOT EXISTS / INSERT OR IGNORE shapes make retry safe.

- [ ] **Step 4.1: Write the failing tests**

Create `tests/clickhouse/test_database_creator_handle.py`:

```python
"""Unit tests for ClickHouseDatabaseCreatorHandle.

The handle wraps a clickhouse-connect Client and a DatabaseAdminStore.
Both are mocked here.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.handle import ClickHouseDatabaseCreatorHandle


def _settings() -> ClickHouseSettings:
    return ClickHouseSettings(
        host="h",
        port=1,
        user="u",
        password="p",
        secure=False,
        verify=False,
        ca_cert_path=None,
        service_admin_user="iris_svc",
        service_admin_role="service_admin_role",
    )


def _make_handle(*, client: Any = None, store: Any = None, username: str = "alice") -> ClickHouseDatabaseCreatorHandle:
    return ClickHouseDatabaseCreatorHandle(
        client=client or MagicMock(),
        settings=_settings(),
        db_admin_store=store or MagicMock(),
        username=username,
    )


def test_create_database_issues_create_with_if_not_exists() -> None:
    client = MagicMock()
    store = MagicMock()
    store.add_admin_user = AsyncMock()
    handle = _make_handle(client=client, store=store, username="alice")

    asyncio.run(handle.create_database("orders"))

    args, _kwargs = client.command.call_args
    sql = args[0]
    assert sql == "CREATE DATABASE IF NOT EXISTS `orders`"


def test_create_database_records_user_as_admin() -> None:
    client = MagicMock()
    store = MagicMock()
    store.add_admin_user = AsyncMock()
    handle = _make_handle(client=client, store=store, username="alice")

    asyncio.run(handle.create_database("orders"))

    store.add_admin_user.assert_awaited_once_with(database="orders", username="alice")


def test_create_database_validates_name() -> None:
    client = MagicMock()
    store = MagicMock()
    store.add_admin_user = AsyncMock()
    handle = _make_handle(client=client, store=store)

    with pytest.raises(ValueError):
        asyncio.run(handle.create_database("bad name with spaces"))
    # Neither CH nor the store should have been called.
    client.command.assert_not_called()
    store.add_admin_user.assert_not_called()


def test_create_database_idempotent_via_if_not_exists() -> None:
    """Two calls don't add duplicate admin rows (store's INSERT OR IGNORE handles it)."""
    client = MagicMock()
    store = MagicMock()
    store.add_admin_user = AsyncMock()
    handle = _make_handle(client=client, store=store, username="alice")

    asyncio.run(handle.create_database("orders"))
    asyncio.run(handle.create_database("orders"))

    assert client.command.call_count == 2
    assert store.add_admin_user.await_count == 2
```

- [ ] **Step 4.2: Run the tests to verify they fail**

```
uv run pytest tests/clickhouse/test_database_creator_handle.py -v
```
Expected: `ImportError: cannot import name 'ClickHouseDatabaseCreatorHandle'`.

- [ ] **Step 4.3: Add `ClickHouseDatabaseCreatorHandle` to `handle.py`**

Append to `src/iris/clickhouse/handle.py` (after `ClickHouseAdminHandle`):

```python
from iris.clickhouse.database_admins import DatabaseAdminStore
from iris.clickhouse.identifiers import validate_identifier


class ClickHouseDatabaseCreatorHandle:
    """Handle for users with the ``clickhouse_database_creator`` role.

    Exposes only ``create_database`` — creates a CH database and atomically
    records the calling iris user as an admin of the new database.
    """

    def __init__(
        self,
        *,
        client: Client,
        settings: ClickHouseSettings,
        db_admin_store: DatabaseAdminStore,
        username: str,
    ) -> None:
        self._client = client
        self._settings = settings
        self._db_admin_store = db_admin_store
        self._username = username

    async def create_database(self, name: str) -> None:
        """``CREATE DATABASE IF NOT EXISTS <name>``; record the calling user as
        an admin of the new database. The CH ``IF NOT EXISTS`` and the store's
        ``INSERT OR IGNORE`` together make this safe to retry after a partial
        failure."""
        validate_identifier(name, kind="database")
        quoted = quote_identifier(name, kind="database")
        await asyncio.to_thread(
            self._client.command, f"CREATE DATABASE IF NOT EXISTS {quoted}"
        )
        await self._db_admin_store.add_admin_user(
            database=name, username=self._username
        )
```

(`quote_identifier` is already imported at the top of `handle.py`.)

- [ ] **Step 4.4: Run the tests to verify they pass**

```
uv run pytest tests/clickhouse/test_database_creator_handle.py -v
```
Expected: 4 tests pass.

- [ ] **Step 4.5: Type-check**

```
uv run basedpyright --level error src/iris/clickhouse/handle.py
```
Expected: 0 errors.

- [ ] **Step 4.6: Commit**

```
git add src/iris/clickhouse/handle.py tests/clickhouse/test_database_creator_handle.py
git commit -m "feat(clickhouse): ClickHouseDatabaseCreatorHandle.create_database

CREATE DATABASE IF NOT EXISTS plus DatabaseAdminStore.add_admin_user.
The two operations are not transactional (different systems) but both
are idempotent, so a partial failure is recoverable by retry. The
handle has no other methods."
```

---

## Task 5: `ClickHouseDatabaseAdminHandle`

**Files:**
- Modify: `src/iris/clickhouse/handle.py`
- Create: `tests/clickhouse/test_database_admin_handle.py`

The per-DB admin handle exposes 14 methods: 4 grant/revoke (user/group), 4 row policies (add/revoke × user/group), 4 delegation (add/remove × user/role), 2 listing on the store, and 2 audit queries to CH `system` tables. Iris-friendly identifiers (`username`, `group`) translate to CH role names (`<x>_USER`, `<x>_GRP`) via the existing suffix constants.

- [ ] **Step 5.1: Write the failing test file**

Create `tests/clickhouse/test_database_admin_handle.py`:

```python
"""Unit tests for ClickHouseDatabaseAdminHandle.

The handle wraps a clickhouse-connect Client (mocked), an httpx.AsyncClient
(used only for query_as_user paths inherited from ClickHouseHandle —
unused here), a DatabaseAdminStore, and a RoleMappingStore. The latter
two are mocked.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from iris.auth.authz.mapping import RoleDef, RoleMapping, RoleMappingError
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.handle import ClickHouseDatabaseAdminHandle


def _settings() -> ClickHouseSettings:
    return ClickHouseSettings(
        host="h",
        port=1,
        user="u",
        password="p",
        secure=False,
        verify=False,
        ca_cert_path=None,
        service_admin_user="iris_svc",
        service_admin_role="service_admin_role",
    )


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://h:1",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"")),
    )


def _empty_mapping() -> RoleMapping:
    return RoleMapping(roles={}, closure={})


def _mapping_with(role_name: str) -> RoleMapping:
    role = RoleDef(
        name=role_name,
        groups=frozenset(),
        users_lower=frozenset(),
        includes=(),
    )
    return RoleMapping(
        roles={role_name: role},
        closure={role_name: frozenset({role_name})},
    )


def _make_handle(
    *,
    client: Any = None,
    db_admin_store: Any = None,
    authz_store: Any = None,
    database: str = "orders",
    username: str = "alice",
) -> ClickHouseDatabaseAdminHandle:
    return ClickHouseDatabaseAdminHandle(
        client=client or MagicMock(),
        http_client=_http_client(),
        settings=_settings(),
        db_admin_store=db_admin_store or MagicMock(),
        authz_store=authz_store or MagicMock(),
        database=database,
        username=username,
    )


# ---- grants ----

def test_grant_select_to_user_translates_to_user_role() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    asyncio.run(handle.grant_select_to_user("bob"))
    args, _ = client.command.call_args
    assert args[0] == "GRANT SELECT ON `orders`.* TO `bob_USER`"


def test_grant_select_to_group_translates_to_group_role() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    asyncio.run(handle.grant_select_to_group("editors"))
    args, _ = client.command.call_args
    assert args[0] == "GRANT SELECT ON `orders`.* TO `editors_GRP`"


def test_revoke_select_from_user_translates_to_user_role() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    asyncio.run(handle.revoke_select_from_user("bob"))
    args, _ = client.command.call_args
    assert args[0] == "REVOKE SELECT ON `orders`.* FROM `bob_USER`"


def test_revoke_select_from_group_translates_to_group_role() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    asyncio.run(handle.revoke_select_from_group("editors"))
    args, _ = client.command.call_args
    assert args[0] == "REVOKE SELECT ON `orders`.* FROM `editors_GRP`"


# ---- row policies ----

def test_add_row_policy_for_user_calls_underlying_helper() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    with patch("iris.clickhouse.handle.add_row_policy") as mock_add:
        asyncio.run(
            handle.add_row_policy_for_user(
                table="lines", column="region", username="bob", value="EU"
            )
        )
    mock_add.assert_called_once()
    _, kwargs = mock_add.call_args
    assert kwargs["database"] == "orders"
    assert kwargs["table"] == "lines"
    assert kwargs["column"] == "region"
    assert kwargs["role"] == "bob_USER"
    assert kwargs["value"] == "EU"


def test_add_row_policy_for_group_calls_underlying_helper() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    with patch("iris.clickhouse.handle.add_row_policy") as mock_add:
        asyncio.run(
            handle.add_row_policy_for_group(
                table="lines", column="region", group="editors", value="EU"
            )
        )
    _, kwargs = mock_add.call_args
    assert kwargs["role"] == "editors_GRP"


def test_revoke_row_policy_for_user_calls_underlying_helper() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    with patch("iris.clickhouse.handle.revoke_row_policy") as mock_revoke:
        asyncio.run(
            handle.revoke_row_policy_for_user(
                table="lines", column="region", username="bob", value="EU"
            )
        )
    _, kwargs = mock_revoke.call_args
    assert kwargs["role"] == "bob_USER"


def test_revoke_row_policy_for_group_calls_underlying_helper() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    with patch("iris.clickhouse.handle.revoke_row_policy") as mock_revoke:
        asyncio.run(
            handle.revoke_row_policy_for_group(
                table="lines", column="region", group="editors", value="EU"
            )
        )
    _, kwargs = mock_revoke.call_args
    assert kwargs["role"] == "editors_GRP"


# ---- delegation ----

def test_add_admin_user_delegates_to_store() -> None:
    store = MagicMock()
    store.add_admin_user = AsyncMock()
    handle = _make_handle(db_admin_store=store, database="orders")
    asyncio.run(handle.add_admin_user("bob"))
    store.add_admin_user.assert_awaited_once_with(database="orders", username="bob")


def test_remove_admin_user_delegates_to_store() -> None:
    store = MagicMock()
    store.remove_admin_user = AsyncMock()
    handle = _make_handle(db_admin_store=store, database="orders")
    asyncio.run(handle.remove_admin_user("bob"))
    store.remove_admin_user.assert_awaited_once_with(database="orders", username="bob")


def test_add_admin_role_validates_role_exists_in_authz() -> None:
    db_store = MagicMock()
    db_store.add_admin_role = AsyncMock()
    authz = MagicMock()
    authz.get_mapping = AsyncMock(return_value=_mapping_with("ops"))
    handle = _make_handle(db_admin_store=db_store, authz_store=authz, database="orders")

    asyncio.run(handle.add_admin_role("ops"))

    authz.get_mapping.assert_awaited_once_with()
    db_store.add_admin_role.assert_awaited_once_with(database="orders", role="ops")


def test_add_admin_role_rejects_undefined_role() -> None:
    db_store = MagicMock()
    db_store.add_admin_role = AsyncMock()
    authz = MagicMock()
    authz.get_mapping = AsyncMock(return_value=_empty_mapping())
    handle = _make_handle(db_admin_store=db_store, authz_store=authz, database="orders")

    with pytest.raises(RoleMappingError):
        asyncio.run(handle.add_admin_role("nope"))

    db_store.add_admin_role.assert_not_awaited()


def test_remove_admin_role_does_not_validate() -> None:
    """remove_admin_role can target a role that no longer exists in authz —
    e.g., to clean up a stale mapping after the role was deleted."""
    db_store = MagicMock()
    db_store.remove_admin_role = AsyncMock()
    handle = _make_handle(db_admin_store=db_store, database="orders")
    asyncio.run(handle.remove_admin_role("ops"))
    db_store.remove_admin_role.assert_awaited_once_with(database="orders", role="ops")


# ---- listing ----

def test_list_admin_users_delegates_to_store() -> None:
    store = MagicMock()
    store.list_admin_users = AsyncMock(return_value=["alice", "bob"])
    handle = _make_handle(db_admin_store=store, database="orders")
    rows = asyncio.run(handle.list_admin_users())
    store.list_admin_users.assert_awaited_once_with(database="orders")
    assert rows == ["alice", "bob"]


def test_list_admin_roles_delegates_to_store() -> None:
    store = MagicMock()
    store.list_admin_roles = AsyncMock(return_value=["ops"])
    handle = _make_handle(db_admin_store=store, database="orders")
    rows = asyncio.run(handle.list_admin_roles())
    store.list_admin_roles.assert_awaited_once_with(database="orders")
    assert rows == ["ops"]


def test_list_grants_queries_system_grants_for_database() -> None:
    client = MagicMock()
    result = MagicMock()
    result.named_results.return_value = [{"role_name": "bob_USER", "access_type": "SELECT"}]
    client.query.return_value = result
    handle = _make_handle(client=client, database="orders")

    rows = asyncio.run(handle.list_grants())

    args, kwargs = client.query.call_args
    sql = args[0] if args else kwargs["query"]
    assert "system.grants" in sql
    assert kwargs["parameters"]["d"] == "orders"
    assert rows == [{"role_name": "bob_USER", "access_type": "SELECT"}]


def test_list_row_policies_queries_system_row_policies_for_database() -> None:
    client = MagicMock()
    result = MagicMock()
    result.named_results.return_value = [{"name": "orders_lines_bob_USER_EU_abc12345"}]
    client.query.return_value = result
    handle = _make_handle(client=client, database="orders")

    rows = asyncio.run(handle.list_row_policies())

    args, kwargs = client.query.call_args
    sql = args[0] if args else kwargs["query"]
    assert "system.row_policies" in sql
    assert kwargs["parameters"]["d"] == "orders"
    assert rows == [{"name": "orders_lines_bob_USER_EU_abc12345"}]
```

- [ ] **Step 5.2: Run the tests to verify they fail**

```
uv run pytest tests/clickhouse/test_database_admin_handle.py -v
```
Expected: `ImportError: cannot import name 'ClickHouseDatabaseAdminHandle'`.

- [ ] **Step 5.3: Add `ClickHouseDatabaseAdminHandle` to `handle.py`**

First, add the new imports at the top of `src/iris/clickhouse/handle.py`. The existing file already imports many helpers — confirm and adjust:

```python
# Add at the top with the other imports if missing:
from iris.auth.authz.mapping import RoleMappingError
from iris.clickhouse.grants import revoke_select_from_database
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX

# These are already imported at the top:
# from iris.clickhouse.grants import grant_select_to_database
# from iris.clickhouse.policies import add_row_policy, revoke_row_policy
```

Forward-declare the optional `RoleMappingStore` import via `TYPE_CHECKING` to avoid a hard dependency in the type annotation:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from iris.auth.authz.store import RoleMappingStore
```

Append the class to `src/iris/clickhouse/handle.py`:

```python
class ClickHouseDatabaseAdminHandle:
    """Per-database admin handle.

    Bound to a specific database. Methods translate iris-friendly identifiers
    (username, group) to CH role names (<username>_USER, <group>_GRP) using
    the existing suffix constants. Read grants, row policies, and admin
    delegation are scoped to ``self._database``.
    """

    def __init__(
        self,
        *,
        client: Client,
        http_client: httpx.AsyncClient,
        settings: ClickHouseSettings,
        db_admin_store: DatabaseAdminStore,
        authz_store: "RoleMappingStore",
        database: str,
        username: str,
    ) -> None:
        self._client = client
        self._http_client = http_client
        self._settings = settings
        self._db_admin_store = db_admin_store
        self._authz_store = authz_store
        self._database = database
        self._username = username

    # ---- grants ----

    async def grant_select_to_user(self, username: str) -> None:
        await asyncio.to_thread(
            grant_select_to_database,
            self._client,
            database=self._database,
            role=f"{username}{USER_ROLE_SUFFIX}",
        )

    async def grant_select_to_group(self, group: str) -> None:
        await asyncio.to_thread(
            grant_select_to_database,
            self._client,
            database=self._database,
            role=f"{group}{GROUP_ROLE_SUFFIX}",
        )

    async def revoke_select_from_user(self, username: str) -> None:
        await asyncio.to_thread(
            revoke_select_from_database,
            self._client,
            database=self._database,
            role=f"{username}{USER_ROLE_SUFFIX}",
        )

    async def revoke_select_from_group(self, group: str) -> None:
        await asyncio.to_thread(
            revoke_select_from_database,
            self._client,
            database=self._database,
            role=f"{group}{GROUP_ROLE_SUFFIX}",
        )

    # ---- row policies ----

    async def add_row_policy_for_user(
        self, *, table: str, column: str, username: str, value: str
    ) -> None:
        await asyncio.to_thread(
            add_row_policy,
            self._client,
            database=self._database,
            table=table,
            column=column,
            role=f"{username}{USER_ROLE_SUFFIX}",
            value=value,
            settings=self._settings,
        )

    async def add_row_policy_for_group(
        self, *, table: str, column: str, group: str, value: str
    ) -> None:
        await asyncio.to_thread(
            add_row_policy,
            self._client,
            database=self._database,
            table=table,
            column=column,
            role=f"{group}{GROUP_ROLE_SUFFIX}",
            value=value,
            settings=self._settings,
        )

    async def revoke_row_policy_for_user(
        self, *, table: str, column: str, username: str, value: str
    ) -> None:
        await asyncio.to_thread(
            revoke_row_policy,
            self._client,
            database=self._database,
            table=table,
            role=f"{username}{USER_ROLE_SUFFIX}",
            value=value,
        )

    async def revoke_row_policy_for_group(
        self, *, table: str, column: str, group: str, value: str
    ) -> None:
        await asyncio.to_thread(
            revoke_row_policy,
            self._client,
            database=self._database,
            table=table,
            role=f"{group}{GROUP_ROLE_SUFFIX}",
            value=value,
        )

    # ---- delegation ----

    async def add_admin_user(self, username: str) -> None:
        await self._db_admin_store.add_admin_user(
            database=self._database, username=username
        )

    async def remove_admin_user(self, username: str) -> None:
        await self._db_admin_store.remove_admin_user(
            database=self._database, username=username
        )

    async def add_admin_role(self, role: str) -> None:
        mapping = await self._authz_store.get_mapping()
        if role not in mapping.roles:
            raise RoleMappingError(f"role {role!r} is not defined in the role mapping")
        await self._db_admin_store.add_admin_role(
            database=self._database, role=role
        )

    async def remove_admin_role(self, role: str) -> None:
        # No validation: removing a role from per-DB admin can target a role
        # that has since been deleted from the authz mapping (cleanup case).
        await self._db_admin_store.remove_admin_role(
            database=self._database, role=role
        )

    # ---- listing ----

    async def list_admin_users(self) -> list[str]:
        return await self._db_admin_store.list_admin_users(database=self._database)

    async def list_admin_roles(self) -> list[str]:
        return await self._db_admin_store.list_admin_roles(database=self._database)

    # ---- audit ----

    async def list_grants(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_grants_sync)

    def _list_grants_sync(self) -> list[dict[str, Any]]:
        result = self._client.query(
            "SELECT * FROM system.grants WHERE database = {d:String}",
            parameters={"d": self._database},
        )
        return list(result.named_results())

    async def list_row_policies(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_row_policies_sync)

    def _list_row_policies_sync(self) -> list[dict[str, Any]]:
        result = self._client.query(
            "SELECT * FROM system.row_policies WHERE database = {d:String}",
            parameters={"d": self._database},
        )
        return list(result.named_results())
```

- [ ] **Step 5.4: Run the tests to verify they pass**

```
uv run pytest tests/clickhouse/test_database_admin_handle.py -v
```
Expected: 17 tests pass.

- [ ] **Step 5.5: Type-check**

```
uv run basedpyright --level error src/iris/clickhouse/handle.py
```
Expected: 0 errors.

- [ ] **Step 5.6: Commit**

```
git add src/iris/clickhouse/handle.py tests/clickhouse/test_database_admin_handle.py
git commit -m "feat(clickhouse): ClickHouseDatabaseAdminHandle (per-DB admin)

Bound to one database. Methods translate iris username/group identifiers
to CH role names via USER_ROLE_SUFFIX / GROUP_ROLE_SUFFIX. Surface:
- grant/revoke SELECT for user or group on the database
- add/revoke row policies for user or group on tables in the database
- add/remove admin user, add/remove admin role (with authz-mapping
  validation on add_admin_role)
- list admin users/roles, plus audit queries on system.grants and
  system.row_policies scoped to the database"
```

---

## Task 6: Wire deps + install + lifespan + auth_db_path plumbing

**Files:**
- Modify: `src/iris/auth/routes.py` (stash `app.state.auth_db_path`)
- Modify: `src/iris/clickhouse/install.py` (build `DatabaseAdminStore`)
- Modify: `src/iris/clickhouse/deps.py` (add the two deps + role constant)
- Modify: `src/iris/clickhouse/__init__.py` (re-exports)
- Modify: `src/iris/app.py` (`_lifespan` close)
- Modify: `tests/clickhouse/test_clickhouse_identifiers.py` (public-surface assertion)
- Create: `tests/clickhouse/test_database_admin_deps.py`

This is the cutover task. It wires everything together and adds dep-level tests with FastAPI dependency overrides.

- [ ] **Step 6.1: Stash `app.state.auth_db_path` from auth's install**

In `src/iris/auth/routes.py`, find the `install` function. After:

```python
    settings = AuthSettings.from_env()
```

Add:

```python
    app.state.auth_db_path = settings.auth_db_path
```

This lets `iris.clickhouse.install` read the path without re-calling `AuthSettings.from_env()`.

- [ ] **Step 6.2: Add the role constant + deps to `src/iris/clickhouse/deps.py`**

Append to `src/iris/clickhouse/deps.py`:

```python
from iris.clickhouse.handle import (
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
)
from iris.clickhouse.identifiers import validate_identifier

CLICKHOUSE_DATABASE_CREATOR_ROLE: Final = "clickhouse_database_creator"


async def require_clickhouse_database_creator(
    request: Request,
    session: Session,
    mapping: CurrentMapping,
) -> ClickHouseDatabaseCreatorHandle:
    """Return a database-creator handle. 403 unless the user has
    ``clickhouse_database_creator``. 500 if the role isn't defined."""
    if CLICKHOUSE_DATABASE_CREATOR_ROLE not in mapping.roles:
        raise AuthorizationMisconfigured(CLICKHOUSE_DATABASE_CREATOR_ROLE)
    if CLICKHOUSE_DATABASE_CREATOR_ROLE not in session.roles:
        raise AuthForbidden(
            needed=(CLICKHOUSE_DATABASE_CREATOR_ROLE,),
            have=tuple(sorted(session.roles)),
        )
    return ClickHouseDatabaseCreatorHandle(
        client=request.app.state.clickhouse_client,
        settings=request.app.state.clickhouse_settings,
        db_admin_store=request.app.state.clickhouse_database_admins,
        username=session.user.username,
    )


async def require_clickhouse_database_admin(
    request: Request,
    database: str,
    session: Session,
) -> ClickHouseDatabaseAdminHandle:
    """Return a per-database admin handle. ``database`` is bound from the
    calling route's path/query params by FastAPI. 403 unless the session is
    listed as admin of this database (or has clickhouse_admin)."""
    validate_identifier(database, kind="database")
    db_admin_store = request.app.state.clickhouse_database_admins
    is_admin = await db_admin_store.is_admin(
        database=database,
        username_lower=session.user.username.lower(),
        roles=session.roles,
    )
    if not is_admin:
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

- [ ] **Step 6.3: Update `iris.clickhouse.install` to build `DatabaseAdminStore`**

In `src/iris/clickhouse/install.py`, after the existing http_client + state assignments:

```python
    # ... existing code that sets app.state.clickhouse_client,
    # app.state.clickhouse_settings, app.state.clickhouse_http_client,
    # and app.state.clickhouse_close_http ...

    from iris.clickhouse.database_admins import DatabaseAdminStore

    auth_db_path = app.state.auth_db_path
    db_admin_store = DatabaseAdminStore(path=auth_db_path)
    db_admin_store.bootstrap()
    app.state.clickhouse_database_admins = db_admin_store
    app.state.clickhouse_close_database_admins = db_admin_store.close
```

- [ ] **Step 6.4: Update `_lifespan` in `src/iris/app.py`**

Append the new closer in the chain:

```python
    db_admin_closer = getattr(app.state, "clickhouse_close_database_admins", None)
    if db_admin_closer is not None:
        await db_admin_closer()
```

Place it after the existing `authz_closer` block.

- [ ] **Step 6.5: Re-export the new public surface from `src/iris/clickhouse/__init__.py`**

Add to imports:

```python
from iris.clickhouse.database_admins import DatabaseAdminStore
from iris.clickhouse.deps import (
    CLICKHOUSE_ADMIN_ROLE,
    CLICKHOUSE_DATABASE_CREATOR_ROLE,
    get_clickhouse_handle,
    require_clickhouse_admin,
    require_clickhouse_database_admin,
    require_clickhouse_database_creator,
)
from iris.clickhouse.handle import (
    ClickHouseAdminHandle,
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
    ClickHouseHandle,
)
```

(Adjust to merge with the existing imports — replace existing single-import lines with the multi-symbol versions.)

Update `__all__` to include the new symbols:

```python
__all__ = [
    "CLICKHOUSE_ADMIN_ROLE",
    "CLICKHOUSE_DATABASE_CREATOR_ROLE",
    "ClickHouseAdminHandle",
    "ClickHouseDatabaseAdminHandle",
    "ClickHouseDatabaseCreatorHandle",
    "ClickHouseHandle",
    "ClickHouseSettings",
    "DatabaseAdminStore",
    "add_row_policy",
    "build_client",
    "ensure_service_admin",
    "get_clickhouse_handle",
    "grant_insert_update_to_table",
    "grant_select_to_database",
    "init_user_rights",
    "install",
    "require_clickhouse_admin",
    "require_clickhouse_database_admin",
    "require_clickhouse_database_creator",
    "revoke_row_policy",
    "role_grants",
    "role_row_policies",
    "table_row_policies",
    "user_grants",
    "user_role_memberships",
    "user_row_policies",
]
```

- [ ] **Step 6.6: Update the public-surface test**

In `tests/clickhouse/test_clickhouse_identifiers.py`, find `test_public_surface_exports_named_symbols` and update the expected set:

```python
    expected = {
        "CLICKHOUSE_ADMIN_ROLE",
        "CLICKHOUSE_DATABASE_CREATOR_ROLE",
        "ClickHouseAdminHandle",
        "ClickHouseDatabaseAdminHandle",
        "ClickHouseDatabaseCreatorHandle",
        "ClickHouseHandle",
        "ClickHouseSettings",
        "DatabaseAdminStore",
        "add_row_policy",
        "build_client",
        "ensure_service_admin",
        "get_clickhouse_handle",
        "grant_insert_update_to_table",
        "grant_select_to_database",
        "init_user_rights",
        "install",
        "require_clickhouse_admin",
        "require_clickhouse_database_admin",
        "require_clickhouse_database_creator",
        "revoke_row_policy",
        "role_grants",
        "role_row_policies",
        "table_row_policies",
        "user_grants",
        "user_role_memberships",
        "user_row_policies",
    }
```

- [ ] **Step 6.7: Create `tests/clickhouse/test_database_admin_deps.py`**

```python
"""Unit tests for require_clickhouse_database_creator and
require_clickhouse_database_admin. Mirrors the structure of the existing
tests/clickhouse/test_clickhouse_deps.py — fast, no testcontainer, uses
FastAPI dependency overrides.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from iris.auth.authz.mapping import RoleDef, RoleMapping
from iris.auth.identity import User
from iris.auth.session import Session
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.deps import (
    CLICKHOUSE_DATABASE_CREATOR_ROLE,
    require_clickhouse_database_admin,
    require_clickhouse_database_creator,
)
from iris.clickhouse.handle import (
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
)


def _settings() -> ClickHouseSettings:
    return ClickHouseSettings(
        host="h",
        port=1,
        user="u",
        password="p",
        secure=False,
        verify=False,
        ca_cert_path=None,
        service_admin_user="iris_svc",
        service_admin_role="service_admin_role",
    )


def _session(*, username: str = "alice", roles: frozenset[str] = frozenset()) -> Session:
    user = User(
        subject="mock:" + username,
        username=username,
        display_name=username.title(),
        groups=(),
    )
    now = datetime.now(UTC)
    return Session(
        id="sid",
        user=user,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        data={},
        roles=roles,
    )


def _mapping(roles: list[str]) -> RoleMapping:
    role_defs = {
        r: RoleDef(name=r, groups=frozenset(), users_lower=frozenset(), includes=())
        for r in roles
    }
    closure = {r: frozenset({r}) for r in roles}
    return RoleMapping(roles=role_defs, closure=closure)


def _make_app(*, db_admin_store=None, authz_store=None) -> FastAPI:
    app = FastAPI()
    app.state.clickhouse_client = MagicMock()
    app.state.clickhouse_http_client = httpx.AsyncClient(
        base_url="http://h:1",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"")),
    )
    app.state.clickhouse_settings = _settings()
    app.state.clickhouse_database_admins = db_admin_store or MagicMock()
    app.state.authz_store = authz_store or MagicMock()
    return app


# ---- require_clickhouse_database_creator ----


def test_require_creator_500s_when_role_missing_from_yaml() -> None:
    from iris.auth.authz.core import current_mapping
    from iris.auth.deps import _build_required
    from iris.auth.exceptions import install_exception_handlers

    app = _make_app()
    app.state.templates = MagicMock()
    install_exception_handlers(app, cookie_name="iris_session")

    async def fake_session() -> Session:
        return _session(roles=frozenset())

    async def fake_mapping():
        return _mapping([])  # no roles defined

    app.dependency_overrides[_build_required] = fake_session
    app.dependency_overrides[current_mapping] = fake_mapping

    @app.get("/create")
    async def create(
        handle: ClickHouseDatabaseCreatorHandle = Depends(
            require_clickhouse_database_creator
        ),
    ) -> dict[str, Any]:
        return {"ok": True}

    response = TestClient(app, raise_server_exceptions=False).get("/create")
    assert response.status_code == 500


def test_require_creator_403s_when_user_lacks_role() -> None:
    from iris.auth.authz.core import current_mapping
    from iris.auth.deps import _build_required
    from iris.auth.exceptions import install_exception_handlers

    app = _make_app()
    app.state.templates = MagicMock()
    install_exception_handlers(app, cookie_name="iris_session")

    async def fake_session() -> Session:
        return _session(roles=frozenset({"reader"}))

    async def fake_mapping():
        return _mapping([CLICKHOUSE_DATABASE_CREATOR_ROLE])

    app.dependency_overrides[_build_required] = fake_session
    app.dependency_overrides[current_mapping] = fake_mapping

    @app.get("/create")
    async def create(
        handle: ClickHouseDatabaseCreatorHandle = Depends(
            require_clickhouse_database_creator
        ),
    ) -> dict[str, Any]:
        return {"ok": True}

    response = TestClient(app).get("/create", headers={"accept": "application/json"})
    assert response.status_code == 403


def test_require_creator_returns_handle_on_success() -> None:
    from iris.auth.authz.core import current_mapping
    from iris.auth.deps import _build_required

    db_store = MagicMock()
    db_store.add_admin_user = AsyncMock()
    app = _make_app(db_admin_store=db_store)

    async def fake_session() -> Session:
        return _session(roles=frozenset({CLICKHOUSE_DATABASE_CREATOR_ROLE}))

    async def fake_mapping():
        return _mapping([CLICKHOUSE_DATABASE_CREATOR_ROLE])

    app.dependency_overrides[_build_required] = fake_session
    app.dependency_overrides[current_mapping] = fake_mapping

    @app.get("/create")
    async def create(
        handle: ClickHouseDatabaseCreatorHandle = Depends(
            require_clickhouse_database_creator
        ),
    ) -> dict[str, Any]:
        return {"username": handle._username}

    response = TestClient(app).get("/create")
    assert response.status_code == 200
    assert response.json() == {"username": "alice"}


# ---- require_clickhouse_database_admin ----


def test_require_db_admin_403s_for_non_admin() -> None:
    from iris.auth.deps import _build_required
    from iris.auth.exceptions import install_exception_handlers

    db_store = MagicMock()
    db_store.is_admin = AsyncMock(return_value=False)
    app = _make_app(db_admin_store=db_store)
    app.state.templates = MagicMock()
    install_exception_handlers(app, cookie_name="iris_session")

    async def fake_session() -> Session:
        return _session(username="dave", roles=frozenset())

    app.dependency_overrides[_build_required] = fake_session

    @app.get("/db/{database}")
    async def admin_route(
        database: str,
        handle: ClickHouseDatabaseAdminHandle = Depends(
            require_clickhouse_database_admin
        ),
    ) -> dict[str, Any]:
        return {"ok": True}

    response = TestClient(app).get(
        "/db/orders", headers={"accept": "application/json"}
    )
    assert response.status_code == 403


def test_require_db_admin_admits_listed_user() -> None:
    from iris.auth.deps import _build_required

    db_store = MagicMock()
    db_store.is_admin = AsyncMock(return_value=True)
    app = _make_app(db_admin_store=db_store)

    async def fake_session() -> Session:
        return _session(username="alice", roles=frozenset())

    app.dependency_overrides[_build_required] = fake_session

    @app.get("/db/{database}")
    async def admin_route(
        database: str,
        handle: ClickHouseDatabaseAdminHandle = Depends(
            require_clickhouse_database_admin
        ),
    ) -> dict[str, Any]:
        return {"db": handle._database, "user": handle._username}

    response = TestClient(app).get("/db/orders")
    assert response.status_code == 200
    assert response.json() == {"db": "orders", "user": "alice"}
    db_store.is_admin.assert_awaited_once_with(
        database="orders", username_lower="alice", roles=frozenset()
    )


def test_require_db_admin_short_circuits_for_clickhouse_admin() -> None:
    """Global admin: is_admin sees clickhouse_admin in roles and returns True
    without consulting the per-DB tables."""
    from iris.auth.deps import _build_required

    db_store = MagicMock()
    # Make is_admin behave like the real implementation's short-circuit.
    async def fake_is_admin(*, database, username_lower, roles):
        return "clickhouse_admin" in roles

    db_store.is_admin = AsyncMock(side_effect=fake_is_admin)
    app = _make_app(db_admin_store=db_store)

    async def fake_session() -> Session:
        return _session(username="globaladmin", roles=frozenset({"clickhouse_admin"}))

    app.dependency_overrides[_build_required] = fake_session

    @app.get("/db/{database}")
    async def admin_route(
        database: str,
        handle: ClickHouseDatabaseAdminHandle = Depends(
            require_clickhouse_database_admin
        ),
    ) -> dict[str, Any]:
        return {"db": handle._database}

    response = TestClient(app).get("/db/secret_db")
    assert response.status_code == 200
    assert response.json() == {"db": "secret_db"}


def test_require_db_admin_rejects_invalid_database_name() -> None:
    """Path/query strings that can't be CH identifiers fail fast with 500
    (InvalidIdentifierError isn't a 4xx — it's a programming error in the
    route, not a user-input validation failure)."""
    from iris.auth.deps import _build_required

    app = _make_app()

    async def fake_session() -> Session:
        return _session(roles=frozenset({"clickhouse_admin"}))

    app.dependency_overrides[_build_required] = fake_session

    @app.get("/db/{database}")
    async def admin_route(
        database: str,
        handle: ClickHouseDatabaseAdminHandle = Depends(
            require_clickhouse_database_admin
        ),
    ) -> dict[str, Any]:
        return {"ok": True}

    response = TestClient(app, raise_server_exceptions=False).get(
        "/db/bad name with spaces"
    )
    assert response.status_code == 500
```

- [ ] **Step 6.8: Run the full suite**

```
uv run pytest --ignore=tests/auth/integration
```
Expected: all tests pass, including the 7 new dep tests.

- [ ] **Step 6.9: Type-check at error and warning levels**

```
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: 0 errors, 0 warnings.

- [ ] **Step 6.10: Commit**

```
git add -A
git commit -m "feat(clickhouse): wire database creator/admin deps + DB admin store

- iris.auth.routes.install stashes app.state.auth_db_path so
  iris.clickhouse.install can read the path without re-calling
  AuthSettings.from_env().
- iris.clickhouse.install builds DatabaseAdminStore against the auth
  DB file, runs bootstrap, registers app.state.clickhouse_database_admins
  + the close hook on lifespan.
- iris.clickhouse.deps adds CLICKHOUSE_DATABASE_CREATOR_ROLE plus
  require_clickhouse_database_creator (role-gated) and
  require_clickhouse_database_admin (per-database, takes 'database'
  as a path/query param FastAPI binds from the calling route).
- iris.clickhouse.__init__ re-exports the new public surface; the
  public-surface assertion in test_clickhouse_identifiers updates to
  match.
- _lifespan closes the new store alongside the existing closers.

Includes test_database_admin_deps.py with 7 dep-level tests using
FastAPI dependency overrides."
```

---

## Task 7: End-to-end testcontainer test

**Files:**
- Create: `tests/clickhouse/test_database_admin_integration.py`

Real CH testcontainer. Verifies the full flow: a creator user creates a database, grants alice read access, alice (separately) successfully `SELECT`s from it. A non-admin user gets 403 for any admin operation.

- [ ] **Step 7.1: Create the integration test file**

```python
"""End-to-end integration tests: database creation + per-DB admin grants.

Reuses the session-scoped CH testcontainer in tests/clickhouse/conftest.py.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from iris.auth.authz.store import RoleMappingStore
from iris.clickhouse.database_admins import DatabaseAdminStore
from iris.clickhouse.handle import (
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
    ClickHouseHandle,
)
from iris.clickhouse.users import init_user_rights


def _http_client(ch_settings) -> httpx.AsyncClient:
    scheme = "https" if ch_settings.secure else "http"
    return httpx.AsyncClient(
        base_url=f"{scheme}://{ch_settings.host}:{ch_settings.port}",
        auth=(ch_settings.user, ch_settings.password),
        verify=ch_settings.verify,
        timeout=httpx.Timeout(30.0),
    )


def test_create_database_then_grant_then_read(
    ch_client, ch_settings, tmp_path: Path, prefix
) -> None:
    db_path = str(tmp_path / "auth.db")
    db_admin_store = DatabaseAdminStore(path=db_path)
    db_admin_store.bootstrap()
    authz_store = RoleMappingStore(path=db_path)
    authz_store.bootstrap(_NoSeedSettings())

    creator_username = f"{prefix}_creator"
    target_username = f"{prefix}_target"
    new_db = f"{prefix}_db"

    # Both users need CH accounts (init_user_rights would normally fire on login).
    init_user_rights(ch_client, username=creator_username, groups=[], settings=ch_settings)
    init_user_rights(ch_client, username=target_username, groups=[], settings=ch_settings)

    async def run():
        async with _http_client(ch_settings) as http_client:
            # Step 1: creator creates the database.
            creator_handle = ClickHouseDatabaseCreatorHandle(
                client=ch_client,
                settings=ch_settings,
                db_admin_store=db_admin_store,
                username=creator_username,
            )
            await creator_handle.create_database(new_db)

            # The creator should now be admin of this DB.
            assert await db_admin_store.is_admin(
                database=new_db,
                username_lower=creator_username.lower(),
                roles=frozenset(),
            )

            # Step 2: creator (now admin) grants read to the target user.
            admin_handle = ClickHouseDatabaseAdminHandle(
                client=ch_client,
                http_client=http_client,
                settings=ch_settings,
                db_admin_store=db_admin_store,
                authz_store=authz_store,
                database=new_db,
                username=creator_username,
            )
            await admin_handle.grant_select_to_user(target_username)

            # Step 3: a sample table for the target to read.
            await asyncio.to_thread(
                ch_client.command,
                f"CREATE TABLE IF NOT EXISTS `{new_db}`.t (n UInt32) "
                "ENGINE = MergeTree ORDER BY n",
            )
            await asyncio.to_thread(
                ch_client.command,
                f"INSERT INTO `{new_db}`.t VALUES (1), (2), (3)",
            )

            # Step 4: target user runs an impersonated SELECT.
            target_handle = ClickHouseHandle(
                client=ch_client, http_client=http_client, username=target_username
            )
            rows = await target_handle.query_as_user(
                f"SELECT n FROM `{new_db}`.t ORDER BY n"
            )
            assert rows == [{"n": 1}, {"n": 2}, {"n": 3}]

    try:
        asyncio.run(run())
    finally:
        asyncio.run(db_admin_store.close())
        asyncio.run(authz_store.close())


def test_non_admin_user_cannot_admin_database(
    ch_client, ch_settings, tmp_path: Path, prefix
) -> None:
    """A user not listed in the per-DB admins table can't grant or list admins."""
    db_path = str(tmp_path / "auth.db")
    db_admin_store = DatabaseAdminStore(path=db_path)
    db_admin_store.bootstrap()
    authz_store = RoleMappingStore(path=db_path)
    authz_store.bootstrap(_NoSeedSettings())

    db = f"{prefix}_other_db"
    other_user = f"{prefix}_outsider"

    # Set up: somebody else owns this DB.
    asyncio.run(db_admin_store.add_admin_user(database=db, username="ownerlee"))

    try:
        admitted = asyncio.run(
            db_admin_store.is_admin(
                database=db,
                username_lower=other_user.lower(),
                roles=frozenset(),
            )
        )
        assert admitted is False
    finally:
        asyncio.run(db_admin_store.close())
        asyncio.run(authz_store.close())


def test_pre_existing_target_user_constraint(
    ch_client, ch_settings, tmp_path: Path, prefix
) -> None:
    """grant_select_to_user against a username that has never logged in
    fails because <username>_USER doesn't exist in CH yet."""
    db_path = str(tmp_path / "auth.db")
    db_admin_store = DatabaseAdminStore(path=db_path)
    db_admin_store.bootstrap()
    authz_store = RoleMappingStore(path=db_path)
    authz_store.bootstrap(_NoSeedSettings())

    db = f"{prefix}_pretest"
    creator = f"{prefix}_creator2"
    init_user_rights(ch_client, username=creator, groups=[], settings=ch_settings)
    asyncio.run(ch_client_create_db(ch_client, db_admin_store, creator, db))

    not_yet_logged_in = f"{prefix}_unborn"  # no CH user provisioned

    async def run():
        async with _http_client(ch_settings) as http_client:
            handle = ClickHouseDatabaseAdminHandle(
                client=ch_client,
                http_client=http_client,
                settings=ch_settings,
                db_admin_store=db_admin_store,
                authz_store=authz_store,
                database=db,
                username=creator,
            )
            with pytest.raises(Exception):
                # CH raises a DatabaseError; we don't translate it into a
                # RoleMappingError in this minimal slice — just verify it fails.
                await handle.grant_select_to_user(not_yet_logged_in)

    try:
        asyncio.run(run())
    finally:
        asyncio.run(db_admin_store.close())
        asyncio.run(authz_store.close())


# ----- helpers -----


class _NoSeedSettings:
    bootstrap_role = "admin"
    bootstrap_user = None


async def ch_client_create_db(ch_client, db_admin_store, creator: str, db: str) -> None:
    # CREATE DATABASE + record creator as admin (parallel to ClickHouseDatabaseCreatorHandle).
    await asyncio.to_thread(
        ch_client.command, f"CREATE DATABASE IF NOT EXISTS `{db}`"
    )
    await db_admin_store.add_admin_user(database=db, username=creator)
```

- [ ] **Step 7.2: Run the integration tests**

```
uv run pytest tests/clickhouse/test_database_admin_integration.py -v
```
Expected: 3 tests pass.

If `test_pre_existing_target_user_constraint` fails because the `pytest.raises(Exception)` is too broad and catches something else, narrow the expected exception to whatever clickhouse-connect raises (likely `clickhouse_connect.driver.exceptions.DatabaseError`). The test serves to document the constraint, not to assert the exact wrapped error class.

- [ ] **Step 7.3: Commit**

```
git add tests/clickhouse/test_database_admin_integration.py
git commit -m "test(clickhouse): end-to-end per-DB admin flow against testcontainer

- create database via creator handle; verify creator is now admin
- grant read to target user via admin handle; verify target's
  query_as_user actually returns rows
- non-admin user is_admin=False for a database owned by someone else
- grant_select_to_user against a never-logged-in target fails (the
  documented 'user must have logged in once' constraint)"
```

---

## Task 8: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

Document the per-database admin tier in the ClickHouse section.

- [ ] **Step 8.1: Add a subsection to the ClickHouse section**

Find the heading `### Auth ↔ ClickHouse bridge` in `CLAUDE.md`. Right after that subsection's body (just before the next `###` heading), add:

```markdown
### Per-database admin tier

Three tiers of CH authorization, in increasing privilege:

1. **Any logged-in user** — `get_clickhouse_handle` returns a `ClickHouseHandle` that runs impersonated SELECTs.
2. **Per-database admin** — `require_clickhouse_database_admin` returns a `ClickHouseDatabaseAdminHandle` scoped to one database. Methods cover `grant_select_to_user/group`, `revoke_select_from_user/group`, `add_row_policy_for_user/group`, `revoke_row_policy_for_user/group`, `add/remove_admin_user`, `add/remove_admin_role` (delegation), plus listing/audit. The dep takes `database: str` as a regular FastAPI parameter that gets bound from the calling route's path/query. A user is admin of a database if they're listed in `clickhouse_database_admins_users` for that DB, or any of their effective roles is listed in `clickhouse_database_admins_roles`. Global admins (`clickhouse_admin`) short-circuit to admin-of-everything.
3. **Global admin** — `require_clickhouse_admin` returns the existing `ClickHouseAdminHandle`. Strict superset of the per-DB tier.

A separate role gates **database creation**: `require_clickhouse_database_creator` returns a `ClickHouseDatabaseCreatorHandle` whose only method, `create_database(name)`, runs `CREATE DATABASE IF NOT EXISTS` and atomically records the calling user in `clickhouse_database_admins_users`. The bootstrap creates the empty `clickhouse_database_creator` role on first install but does NOT include it in the bootstrap admin role — operators decide via the mutator API.

Two new SQLite tables live in the same `AUTH_DB_PATH` file:

```sql
CREATE TABLE clickhouse_database_admins_users (
    database_name  TEXT NOT NULL,
    username_lower TEXT NOT NULL,
    PRIMARY KEY (database_name, username_lower)
);
CREATE TABLE clickhouse_database_admins_roles (
    database_name  TEXT NOT NULL,
    role_name      TEXT NOT NULL,
    PRIMARY KEY (database_name, role_name)
);
```

The `DatabaseAdminStore` class wraps these tables. It's installed by `iris.clickhouse.install` and exposed on `app.state.clickhouse_database_admins`.

Example routes:

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

**Pre-existing target user constraint.** Grants and row policies target `<username>_USER` (or `<group>_GRP`) roles. These exist only after the user/group has been provisioned, which happens at login time via the existing `init_user_rights` post-login hook. Granting access to a user who has never logged in raises a CH error; the user must authenticate at least once first.
```

Also update the env-var block. Find:

```
AUTH_DB_PATH=./iris-auth.db          # SQLite file backing both sessions and authz tables; :memory: for tests
```

Change `authz tables` to `authz + per-database admin tables`:

```
AUTH_DB_PATH=./iris-auth.db          # SQLite file backing sessions, authz, and per-database admin tables; :memory: for tests
```

- [ ] **Step 8.2: Run the full suite + type-check + ruff**

```
uv run pytest --ignore=tests/auth/integration
uv run basedpyright --level error
uv run basedpyright --level warning
uv run ruff check
```
Expected: all clean.

- [ ] **Step 8.3: Commit**

```
git add CLAUDE.md
git commit -m "docs: per-database ClickHouse admin tier in CLAUDE.md

Adds a subsection under 'ClickHouse' describing the three-tier
authorization (any user, per-DB admin, global admin), the separate
clickhouse_database_creator role gate for CREATE DATABASE, the two
new SQLite tables, the DatabaseAdminStore + handle/dep public
surface, the example routes, and the pre-existing target user
constraint."
```

---

## Self-review

**Spec coverage:**

- [x] `clickhouse_database_creator` role bootstrapped on first install — Task 1.
- [x] Bootstrap admin does NOT auto-include creator role — Task 1 (new test).
- [x] Two new SQLite tables (`clickhouse_database_admins_users`, `clickhouse_database_admins_roles`) — Task 3 (`_DB_ADMIN_SCHEMA`).
- [x] `DatabaseAdminStore` API: `is_admin`, `add/remove_admin_user`, `add/remove_admin_role`, `list_admin_users/roles`, `close` — Task 3.
- [x] `is_admin` short-circuits on `clickhouse_admin` — Task 3.
- [x] `revoke_select_from_database` helper — Task 2.
- [x] `ClickHouseDatabaseCreatorHandle.create_database` — Task 4.
- [x] Atomic CH create + admin record (with documented partial-failure recovery) — Task 4.
- [x] `ClickHouseDatabaseAdminHandle` 14 methods — Task 5.
- [x] User/group identifier translation via `USER_ROLE_SUFFIX` / `GROUP_ROLE_SUFFIX` — Task 5.
- [x] `add_admin_role` validates the role exists in authz — Task 5.
- [x] Audit methods scoped to `database` — Task 5.
- [x] `CLICKHOUSE_DATABASE_CREATOR_ROLE` constant — Task 6.
- [x] `require_clickhouse_database_creator` dep — Task 6.
- [x] `require_clickhouse_database_admin` dep with `database: str` path/query binding — Task 6.
- [x] Wiring in `iris.clickhouse.install` and lifespan — Task 6.
- [x] Re-exports from `iris.clickhouse.__init__` — Task 6.
- [x] `app.state.auth_db_path` plumbing from auth — Task 6.
- [x] Public-surface test updated — Task 6.
- [x] End-to-end testcontainer test — Task 7.
- [x] CLAUDE.md update — Task 8.

**Placeholder scan:** No "TBD" / "add validation" / "similar to Task N" patterns. Every test case shows the assertion code; every implementation step shows the file content.

**Type consistency:**
- `DatabaseAdminStore.add_admin_user(*, database, username)` — used identically in Task 4 (`ClickHouseDatabaseCreatorHandle.create_database`), Task 5 (`ClickHouseDatabaseAdminHandle.add_admin_user`), and Task 6 (creator dep + admin dep).
- `DatabaseAdminStore.is_admin(*, database, username_lower, roles)` — used identically in Task 5, Task 6 (`require_clickhouse_database_admin`), and Task 7.
- `app.state.clickhouse_database_admins` — set in Task 6's install change, read by both deps in Task 6, and by Task 7's wiring.
- `app.state.clickhouse_close_database_admins` — set in Task 6, read by `_lifespan` in Task 6.
- `app.state.auth_db_path` — set in Task 6 (auth's install), read in Task 6 (clickhouse's install).
- `CLICKHOUSE_DATABASE_CREATOR_ROLE` value matches the literal string in `bootstrap.py` (Task 1) and `deps.py` (Task 6) — both are `"clickhouse_database_creator"`. The bootstrap's hardcoded string lives next to a sibling comment that flags the drift; if it ever changes, the deps test in Task 6 fails because the bootstrap doesn't seed the role anymore.
- Handle method names: `grant_select_to_user/group`, `revoke_select_from_user/group`, `add_row_policy_for_user/group`, `revoke_row_policy_for_user/group`, `add/remove_admin_user`, `add/remove_admin_role`, `list_admin_users/roles`, `list_grants`, `list_row_policies`. Used identically in Task 5 tests, Task 5 implementation, Task 7 integration test, and Task 8 docs.
