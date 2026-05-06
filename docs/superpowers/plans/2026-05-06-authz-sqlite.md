# Authz mapping in SQLite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the authz role mapping from `authz.yaml` into SQLite. Add a `RoleMappingStore` with read + 8 fine-grained mutators. Bootstrap a single admin user on first install via two env vars. Big-bang cutover — the YAML loader and `pyyaml` runtime dep go away.

**Architecture:** New `RoleMappingStore` class in `src/iris/auth/authz/store.py` opens its own `sqlite3.Connection` against the same DB file as `SessionStore` (renamed env var `AUTH_DB_PATH`, default `./iris-auth.db`). Four-table schema with FK CASCADE/RESTRICT. Per-request `get_mapping()` (no in-memory cache). Bootstrap in `bootstrap.py` runs only when `authz_roles` doesn't yet exist; seeds the configured admin role with `clickhouse_admin` as an include and the configured user as a member.

**Tech Stack:** Python 3.13, stdlib `sqlite3`, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-05-06-authz-sqlite-design.md`.

---

## File Structure

NEW files:

| Path | Responsibility |
|---|---|
| `src/iris/auth/authz/store.py` | `RoleMappingStore`: schema setup, `get_mapping`, 8 mutators, `close`. |
| `src/iris/auth/authz/bootstrap.py` | `install_authz_schema(conn, settings)`: detects first install via `sqlite_master`; seeds bootstrap role + user. |
| `tests/auth/authz/test_role_mapping_store.py` | Mutator + read tests against tempfile DB. |
| `tests/auth/authz/test_authz_bootstrap.py` | First-install seeding, idempotence, custom role name, drift check on `clickhouse_admin`. |

DELETED files:

| Path | Why |
|---|---|
| `src/iris/auth/authz/loader.py` | `RoleMappingLoader` replaced by `RoleMappingStore`. |
| `src/iris/auth/authz/config.py` | `AuthzSettings` (AUTHZ_CONFIG_PATH) gone. |
| `tests/auth/authz/test_loader.py` | Loader is gone. |
| `tests/auth/authz/test_authz_config.py` | AuthzSettings is gone. |
| `tests/auth/authz/test_mapping.py` | YAML parser tests; closure/cycle cases lift into `test_role_mapping_store.py`. |
| `tests/auth/authz/test_install_wiring.py` | Tests AUTHZ_CONFIG_PATH failure modes; the env var is gone. New install-wiring tests live in `test_authz_bootstrap.py`. |

MODIFIED files:

| Path | Change |
|---|---|
| `src/iris/auth/authz/mapping.py` | Strip YAML code (`parse`, `_NoDuplicatesSafeLoader`, `_construct_mapping_no_dupes`, `_coerce_string_list`). Drop `import yaml`. Keep `RoleMapping`, `RoleDef`, `_compute_closure`, `_ROLE_NAME_RE`, `RoleMappingError`. Drop the file-level pyright suppression. |
| `src/iris/auth/authz/core.py` | `current_mapping` body changes from `request.app.state.authz_loader.get()` to `await request.app.state.authz_store.get_mapping()`. |
| `src/iris/auth/config.py` | Rename `session_db_path` → `auth_db_path`; read `AUTH_DB_PATH` instead of `SESSION_DB_PATH`. Add `bootstrap_role`, `bootstrap_user`. |
| `src/iris/auth/routes.py` | `install()` constructs `RoleMappingStore`, calls `install_authz_schema`, registers `app.state.authz_store` + close hook; remove `RoleMappingLoader` + `AuthzSettings` references. |
| `src/iris/app.py` | `_lifespan` adds `auth_close_authz_store` to the existing closer chain. |
| `tests/conftest.py` | Drop the YAML fixture write; rename `SESSION_DB_PATH` env default to `AUTH_DB_PATH`; add `AUTHZ_BOOTSTRAP_USER=alice`. |
| `tests/auth/test_session_dep.py` | `_build_app` builds a `RoleMappingStore` and seeds the test fixture roles via mutators (no more YAML temp-file write). |
| `tests/auth/authz/test_authz_deps.py` | Same retargeting. |
| `pyproject.toml` | Drop `pyyaml` from `dependencies`. |
| `CLAUDE.md` | Replace YAML schema docs with new env vars + schema overview + mutator API. Remove "robustness against bad edits" / mtime-cache paragraphs. |

---

## Task 1: Rename SESSION_DB_PATH → AUTH_DB_PATH

**Files:**
- Modify: `src/iris/auth/config.py`
- Modify: `src/iris/auth/routes.py`
- Modify: `tests/conftest.py`

Mechanical rename. The DB file holds both `sessions` (existing) and `authz_*` (added in later tasks); the new name reflects the broader scope.

- [ ] **Step 1.1: Rename in `AuthSettings`**

In `src/iris/auth/config.py`:

```python
# Rename the field
@dataclass(frozen=True)
class AuthSettings:
    method: Literal["oauth", "ldap", "mock"]
    cookie_name: str
    ttl_seconds: int
    absolute_ttl_seconds: int
    max_per_user: int
    cookie_secure: bool
    auth_db_path: str          # WAS: session_db_path
    oidc: OIDCSettings | None
    ldap: LDAPSettings | None
    mock: MockSettings | None

    @classmethod
    def from_env(cls) -> AuthSettings:
        # ... existing ...
        cookie_secure = _get_bool("COOKIE_SECURE", True)
        auth_db_path = (                                      # WAS: session_db_path
            os.environ.get("AUTH_DB_PATH", "").strip()        # WAS: SESSION_DB_PATH
            or "./iris-auth.db"                               # WAS: ./iris-sessions.db
        )
        # ... existing ...
        return cls(
            method=method,
            cookie_name=cookie_name,
            ttl_seconds=ttl_seconds,
            absolute_ttl_seconds=absolute_ttl_seconds,
            max_per_user=max_per_user,
            cookie_secure=cookie_secure,
            auth_db_path=auth_db_path,                         # WAS: session_db_path=session_db_path
            oidc=oidc,
            ldap=ldap,
            mock=mock,
        )
```

- [ ] **Step 1.2: Update `routes.install` to use the new field name**

In `src/iris/auth/routes.py:install`, change:

```python
store = SessionStore(
    path=settings.session_db_path,
```

to:

```python
store = SessionStore(
    path=settings.auth_db_path,
```

- [ ] **Step 1.3: Update `tests/conftest.py`**

Change:

```python
os.environ.setdefault("SESSION_DB_PATH", ":memory:")
```

to:

```python
os.environ.setdefault("AUTH_DB_PATH", ":memory:")
```

- [ ] **Step 1.4: Run the suite**

```
uv run pytest --ignore=tests/auth/integration
```
Expected: 266 passed (same as before).

- [ ] **Step 1.5: Type-check**

```
uv run basedpyright --level error
```
Expected: 0 errors.

- [ ] **Step 1.6: Commit**

```
git add src/iris/auth/config.py src/iris/auth/routes.py tests/conftest.py
git commit -m "refactor(auth): rename SESSION_DB_PATH -> AUTH_DB_PATH

The DB file holds both sessions and (soon) authz_* tables. The new
name reflects the broader scope. Default path becomes
./iris-auth.db. Tests' :memory: default carries over."
```

---

## Task 2: RoleMappingStore — read API (`get_mapping`)

**Files:**
- Create: `src/iris/auth/authz/store.py`
- Create: `tests/auth/authz/test_role_mapping_store.py`

Build the store class with schema initialization, the read path, and `close`. Mutators come in Tasks 3 and 4.

- [ ] **Step 2.1: Write the failing tests**

Create `tests/auth/authz/test_role_mapping_store.py`:

```python
"""Unit tests for RoleMappingStore.

These tests use a tempfile DB. The store opens its own connection on
each test; the file persists for the duration of the test and is
cleaned up by tmp_path teardown.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from iris.auth.authz.mapping import RoleMapping, RoleMappingError
from iris.auth.authz.store import RoleMappingStore


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "auth.db"


@pytest.fixture
def store(store_path):
    s = RoleMappingStore(path=str(store_path))
    yield s
    asyncio.run(s.close())


def test_get_mapping_on_empty_db_returns_empty_mapping(store):
    mapping = asyncio.run(store.get_mapping())
    assert isinstance(mapping, RoleMapping)
    assert mapping.roles == {}
    assert mapping.closure == {}


def test_get_mapping_returns_seeded_role(store):
    # Seed via direct SQL (mutators are added in later tasks).
    store._conn.execute("INSERT INTO authz_roles(name) VALUES ('reader')")
    mapping = asyncio.run(store.get_mapping())
    assert "reader" in mapping.roles
    assert mapping.roles["reader"].groups == frozenset()
    assert mapping.roles["reader"].users_lower == frozenset()
    assert mapping.roles["reader"].includes == ()
    assert mapping.closure["reader"] == frozenset({"reader"})


def test_get_mapping_returns_groups_users_includes(store):
    c = store._conn
    c.execute("INSERT INTO authz_roles(name) VALUES ('reader')")
    c.execute("INSERT INTO authz_roles(name) VALUES ('writer')")
    c.execute("INSERT INTO authz_role_groups(role_name, group_name) VALUES ('writer', 'editors')")
    c.execute("INSERT INTO authz_role_users(role_name, username_lower) VALUES ('writer', 'bob')")
    c.execute("INSERT INTO authz_role_includes(role_name, included_role) VALUES ('writer', 'reader')")

    mapping = asyncio.run(store.get_mapping())

    assert mapping.roles["writer"].groups == frozenset({"editors"})
    assert mapping.roles["writer"].users_lower == frozenset({"bob"})
    assert mapping.roles["writer"].includes == ("reader",)
    assert mapping.closure["writer"] == frozenset({"reader", "writer"})
    assert mapping.closure["reader"] == frozenset({"reader"})


def test_get_mapping_users_lookup_is_case_insensitive_via_lowered_storage(store):
    """Users are stored lowercased; the existing resolve_roles lowercases the
    incoming username for comparison. So storage must already be lowercased."""
    c = store._conn
    c.execute("INSERT INTO authz_roles(name) VALUES ('admin')")
    c.execute(
        "INSERT INTO authz_role_users(role_name, username_lower) VALUES ('admin', 'alice')"
    )
    mapping = asyncio.run(store.get_mapping())
    assert "alice" in mapping.roles["admin"].users_lower


def test_close_is_idempotent(store_path):
    s = RoleMappingStore(path=str(store_path))
    asyncio.run(s.close())
    asyncio.run(s.close())  # must not raise


def test_schema_creates_indexes(store):
    """Sanity check the indexes the spec calls out."""
    rows = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_authz_%'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_authz_role_groups_group" in names
    assert "idx_authz_role_users_user" in names
    assert "idx_authz_role_includes_inc" in names


def test_schema_enforces_fk_on_includes(store):
    """included_role FK -- can't include a role that doesn't exist."""
    c = store._conn
    c.execute("INSERT INTO authz_roles(name) VALUES ('a')")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO authz_role_includes(role_name, included_role) VALUES ('a', 'nope')"
        )
```

- [ ] **Step 2.2: Run the tests to verify they fail**

```
uv run pytest tests/auth/authz/test_role_mapping_store.py -v
```
Expected: ImportError / ModuleNotFoundError on `iris.auth.authz.store`.

- [ ] **Step 2.3: Create `src/iris/auth/authz/store.py`**

```python
"""SQLite-backed role mapping store.

Opens its own sqlite3.Connection against the auth DB file (same file as
SessionStore; WAL mode handles coexistence). All sync sqlite3 calls are
wrapped in asyncio.to_thread.

This file ships the read path + schema bootstrap. Mutators land in
later commits.
"""
from __future__ import annotations

import asyncio
import sqlite3

from iris.auth.authz.mapping import (
    RoleDef,
    RoleMapping,
    _compute_closure,
)

_AUTHZ_SCHEMA = """
CREATE TABLE IF NOT EXISTS authz_roles (
    name TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS authz_role_groups (
    role_name  TEXT NOT NULL,
    group_name TEXT NOT NULL,
    PRIMARY KEY (role_name, group_name),
    FOREIGN KEY (role_name) REFERENCES authz_roles(name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS authz_role_users (
    role_name      TEXT NOT NULL,
    username_lower TEXT NOT NULL,
    PRIMARY KEY (role_name, username_lower),
    FOREIGN KEY (role_name) REFERENCES authz_roles(name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS authz_role_includes (
    role_name     TEXT NOT NULL,
    included_role TEXT NOT NULL,
    PRIMARY KEY (role_name, included_role),
    FOREIGN KEY (role_name)     REFERENCES authz_roles(name) ON DELETE CASCADE,
    FOREIGN KEY (included_role) REFERENCES authz_roles(name) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_authz_role_groups_group ON authz_role_groups(group_name);
CREATE INDEX IF NOT EXISTS idx_authz_role_users_user   ON authz_role_users(username_lower);
CREATE INDEX IF NOT EXISTS idx_authz_role_includes_inc ON authz_role_includes(included_role);
"""


class RoleMappingStore:
    def __init__(self, *, path: str) -> None:
        self._lock = asyncio.Lock()
        self._closed = False
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        # Same PRAGMAs as SessionStore. journal_mode=WAL is file-level and
        # idempotent. synchronous + busy_timeout are connection-level and
        # need to be set on this connection too.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_AUTHZ_SCHEMA)

    async def get_mapping(self) -> RoleMapping:
        async with self._lock:
            return await asyncio.to_thread(self._get_mapping_sync)

    def _get_mapping_sync(self) -> RoleMapping:
        # Four queries — assemble RoleDef per role, then compute closure.
        role_rows = self._conn.execute(
            "SELECT name FROM authz_roles"
        ).fetchall()
        group_rows = self._conn.execute(
            "SELECT role_name, group_name FROM authz_role_groups"
        ).fetchall()
        user_rows = self._conn.execute(
            "SELECT role_name, username_lower FROM authz_role_users"
        ).fetchall()
        include_rows = self._conn.execute(
            "SELECT role_name, included_role FROM authz_role_includes "
            "ORDER BY role_name, included_role"
        ).fetchall()

        groups_by_role: dict[str, set[str]] = {}
        users_by_role: dict[str, set[str]] = {}
        includes_by_role: dict[str, list[str]] = {}
        for r in group_rows:
            groups_by_role.setdefault(r["role_name"], set()).add(r["group_name"])
        for r in user_rows:
            users_by_role.setdefault(r["role_name"], set()).add(r["username_lower"])
        for r in include_rows:
            includes_by_role.setdefault(r["role_name"], []).append(r["included_role"])

        roles: dict[str, RoleDef] = {}
        for r in role_rows:
            name = r["name"]
            roles[name] = RoleDef(
                name=name,
                groups=frozenset(groups_by_role.get(name, set())),
                users_lower=frozenset(users_by_role.get(name, set())),
                includes=tuple(includes_by_role.get(name, [])),
            )

        closure = _compute_closure(roles)
        return RoleMapping(roles=roles, closure=closure)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._conn.close)
```

- [ ] **Step 2.4: Run the tests to verify they pass**

```
uv run pytest tests/auth/authz/test_role_mapping_store.py -v
```
Expected: All 7 tests pass.

- [ ] **Step 2.5: Type-check**

```
uv run basedpyright --level error src/iris/auth/authz/store.py tests/auth/authz/test_role_mapping_store.py
```
Expected: 0 errors.

- [ ] **Step 2.6: Commit**

```
git add src/iris/auth/authz/store.py tests/auth/authz/test_role_mapping_store.py
git commit -m "feat(authz): RoleMappingStore — schema + get_mapping read path

Four-table schema (authz_roles, authz_role_groups, authz_role_users,
authz_role_includes) with FK CASCADE/RESTRICT. WAL + synchronous=NORMAL
PRAGMAs match SessionStore. get_mapping fetches all four tables, builds
RoleDef per role, computes closure via the existing helper.

Mutators land in subsequent commits."
```

---

## Task 3: RoleMappingStore mutators — roles, groups, users

**Files:**
- Modify: `src/iris/auth/authz/store.py`
- Modify: `tests/auth/authz/test_role_mapping_store.py`

Six mutators that all run a single SQL statement plus app-side validation. `add_*` use `INSERT OR IGNORE` for idempotence.

- [ ] **Step 3.1: Append failing tests**

Add to `tests/auth/authz/test_role_mapping_store.py`:

```python
def test_add_role_creates_row(store):
    asyncio.run(store.add_role("reader"))
    mapping = asyncio.run(store.get_mapping())
    assert "reader" in mapping.roles


def test_add_role_is_idempotent(store):
    asyncio.run(store.add_role("reader"))
    asyncio.run(store.add_role("reader"))  # must not raise
    mapping = asyncio.run(store.get_mapping())
    assert list(mapping.roles) == ["reader"]


def test_add_role_rejects_invalid_name(store):
    with pytest.raises(RoleMappingError):
        asyncio.run(store.add_role("bad name with spaces"))
    with pytest.raises(RoleMappingError):
        asyncio.run(store.add_role(""))
    with pytest.raises(RoleMappingError):
        asyncio.run(store.add_role("role!"))


def test_remove_role_deletes_row_and_cascades(store):
    asyncio.run(store.add_role("admin"))
    asyncio.run(store.add_group_to_role("admin", "platform"))
    asyncio.run(store.add_user_to_role("admin", "alice"))
    asyncio.run(store.remove_role("admin"))
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles == {}
    # Verify the child rows were cascade-deleted by checking no orphans remain.
    assert store._conn.execute(
        "SELECT COUNT(*) FROM authz_role_groups"
    ).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM authz_role_users"
    ).fetchone()[0] == 0


def test_remove_role_unknown_id_is_noop(store):
    asyncio.run(store.remove_role("not-a-role"))  # must not raise


def test_add_group_to_role_round_trips(store):
    asyncio.run(store.add_role("writer"))
    asyncio.run(store.add_group_to_role("writer", "editors"))
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles["writer"].groups == frozenset({"editors"})


def test_add_group_to_role_is_idempotent(store):
    asyncio.run(store.add_role("writer"))
    asyncio.run(store.add_group_to_role("writer", "editors"))
    asyncio.run(store.add_group_to_role("writer", "editors"))  # no-op
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles["writer"].groups == frozenset({"editors"})


def test_add_group_to_role_fails_if_role_missing(store):
    with pytest.raises(RoleMappingError):
        asyncio.run(store.add_group_to_role("nope", "editors"))


def test_remove_group_from_role(store):
    asyncio.run(store.add_role("writer"))
    asyncio.run(store.add_group_to_role("writer", "editors"))
    asyncio.run(store.remove_group_from_role("writer", "editors"))
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles["writer"].groups == frozenset()


def test_add_user_to_role_lowercases(store):
    asyncio.run(store.add_role("admin"))
    asyncio.run(store.add_user_to_role("admin", "Alice"))
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles["admin"].users_lower == frozenset({"alice"})


def test_add_user_to_role_idempotent_across_case(store):
    """Same user added twice with different cases — only one row."""
    asyncio.run(store.add_role("admin"))
    asyncio.run(store.add_user_to_role("admin", "Alice"))
    asyncio.run(store.add_user_to_role("admin", "ALICE"))
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles["admin"].users_lower == frozenset({"alice"})


def test_remove_user_from_role_lowercases(store):
    asyncio.run(store.add_role("admin"))
    asyncio.run(store.add_user_to_role("admin", "alice"))
    asyncio.run(store.remove_user_from_role("admin", "ALICE"))  # case-insensitive
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles["admin"].users_lower == frozenset()
```

- [ ] **Step 3.2: Run the tests to verify they fail**

```
uv run pytest tests/auth/authz/test_role_mapping_store.py -v
```
Expected: 12 new tests fail with `AttributeError: 'RoleMappingStore' has no attribute 'add_role'` etc.

- [ ] **Step 3.3: Add the mutators to `RoleMappingStore`**

Append to `src/iris/auth/authz/store.py` (inside the class), and add the import at the top:

```python
# Top of file — add to existing imports
from iris.auth.authz.mapping import (
    _ROLE_NAME_RE,
    RoleDef,
    RoleMapping,
    RoleMappingError,
    _compute_closure,
)


# Methods on RoleMappingStore — append below close():

    def _validate_role_name(self, name: str) -> None:
        if not _ROLE_NAME_RE.fullmatch(name):
            raise RoleMappingError(f"invalid role name {name!r}")

    async def add_role(self, name: str) -> None:
        self._validate_role_name(name)
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "INSERT OR IGNORE INTO authz_roles(name) VALUES (?)",
                (name,),
            )

    async def remove_role(self, name: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "DELETE FROM authz_roles WHERE name = ?",
                (name,),
            )

    async def add_group_to_role(self, role: str, group: str) -> None:
        async with self._lock:
            try:
                await asyncio.to_thread(
                    self._conn.execute,
                    "INSERT OR IGNORE INTO authz_role_groups(role_name, group_name) VALUES (?, ?)",
                    (role, group),
                )
            except sqlite3.IntegrityError as exc:
                raise RoleMappingError(
                    f"role {role!r} not defined; create it before assigning groups"
                ) from exc

    async def remove_group_from_role(self, role: str, group: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "DELETE FROM authz_role_groups WHERE role_name = ? AND group_name = ?",
                (role, group),
            )

    async def add_user_to_role(self, role: str, username: str) -> None:
        username_lower = username.lower()
        async with self._lock:
            try:
                await asyncio.to_thread(
                    self._conn.execute,
                    "INSERT OR IGNORE INTO authz_role_users(role_name, username_lower) VALUES (?, ?)",
                    (role, username_lower),
                )
            except sqlite3.IntegrityError as exc:
                raise RoleMappingError(
                    f"role {role!r} not defined; create it before assigning users"
                ) from exc

    async def remove_user_from_role(self, role: str, username: str) -> None:
        username_lower = username.lower()
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "DELETE FROM authz_role_users WHERE role_name = ? AND username_lower = ?",
                (role, username_lower),
            )
```

The `_ROLE_NAME_RE` regex already exists in `mapping.py` — re-export it via the existing import. If it's currently module-private (`_` prefix), that's fine; we're inside the same package.

- [ ] **Step 3.4: Run the tests to verify they pass**

```
uv run pytest tests/auth/authz/test_role_mapping_store.py -v
```
Expected: all tests pass (the original 7 + 12 new = 19).

- [ ] **Step 3.5: Type-check**

```
uv run basedpyright --level error src/iris/auth/authz/store.py
```
Expected: 0 errors.

- [ ] **Step 3.6: Commit**

```
git add src/iris/auth/authz/store.py tests/auth/authz/test_role_mapping_store.py
git commit -m "feat(authz): RoleMappingStore mutators — roles, groups, users

Six mutators with single-SQL-statement implementations and app-side
validation. INSERT OR IGNORE keeps add_* idempotent. FK violations
on add_group_to_role / add_user_to_role (role doesn't exist) are
re-raised as RoleMappingError. Usernames are lowercased on storage
and on lookup, matching the existing resolve_roles convention."
```

---

## Task 4: RoleMappingStore mutators — includes (with cycle check)

**Files:**
- Modify: `src/iris/auth/authz/store.py`
- Modify: `tests/auth/authz/test_role_mapping_store.py`

`add_include` is the most interesting mutator — it has to detect cycles before persisting. The check fetches the current includes table, virtually adds the new edge, and runs DFS from the source role to see whether it can reach itself.

- [ ] **Step 4.1: Append failing tests**

Add to `tests/auth/authz/test_role_mapping_store.py`:

```python
def test_add_include_creates_edge(store):
    asyncio.run(store.add_role("reader"))
    asyncio.run(store.add_role("writer"))
    asyncio.run(store.add_include("writer", "reader"))
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles["writer"].includes == ("reader",)
    assert mapping.closure["writer"] == frozenset({"reader", "writer"})


def test_add_include_is_idempotent(store):
    asyncio.run(store.add_role("reader"))
    asyncio.run(store.add_role("writer"))
    asyncio.run(store.add_include("writer", "reader"))
    asyncio.run(store.add_include("writer", "reader"))  # no-op
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles["writer"].includes == ("reader",)


def test_add_include_rejects_self_cycle(store):
    asyncio.run(store.add_role("admin"))
    with pytest.raises(RoleMappingError, match="cycle"):
        asyncio.run(store.add_include("admin", "admin"))


def test_add_include_rejects_two_role_cycle(store):
    asyncio.run(store.add_role("a"))
    asyncio.run(store.add_role("b"))
    asyncio.run(store.add_include("a", "b"))
    with pytest.raises(RoleMappingError, match="cycle"):
        asyncio.run(store.add_include("b", "a"))


def test_add_include_rejects_transitive_cycle(store):
    """a -> b -> c, then c -> a would create a cycle through three nodes."""
    for r in ("a", "b", "c"):
        asyncio.run(store.add_role(r))
    asyncio.run(store.add_include("a", "b"))
    asyncio.run(store.add_include("b", "c"))
    with pytest.raises(RoleMappingError, match="cycle"):
        asyncio.run(store.add_include("c", "a"))


def test_add_include_rejects_missing_included_role(store):
    asyncio.run(store.add_role("a"))
    with pytest.raises(RoleMappingError):
        asyncio.run(store.add_include("a", "nonexistent"))


def test_add_include_rejects_missing_role(store):
    asyncio.run(store.add_role("a"))
    with pytest.raises(RoleMappingError):
        asyncio.run(store.add_include("nonexistent", "a"))


def test_remove_include_deletes_edge(store):
    asyncio.run(store.add_role("a"))
    asyncio.run(store.add_role("b"))
    asyncio.run(store.add_include("a", "b"))
    asyncio.run(store.remove_include("a", "b"))
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles["a"].includes == ()


def test_remove_role_blocked_when_included_by_another(store):
    asyncio.run(store.add_role("base"))
    asyncio.run(store.add_role("admin"))
    asyncio.run(store.add_include("admin", "base"))
    with pytest.raises(RoleMappingError, match="included"):
        asyncio.run(store.remove_role("base"))
```

- [ ] **Step 4.2: Run the tests to verify they fail**

```
uv run pytest tests/auth/authz/test_role_mapping_store.py -v
```
Expected: 9 new failing tests.

- [ ] **Step 4.3: Implement `add_include` / `remove_include` and update `remove_role` to translate FK RESTRICT**

Append to `src/iris/auth/authz/store.py`:

```python
    async def add_include(self, role: str, included_role: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._add_include_sync, role, included_role)

    def _add_include_sync(self, role: str, included_role: str) -> None:
        # Both roles must exist (enforced by FKs, but we want a clean error message
        # rather than the IntegrityError from sqlite).
        rows = self._conn.execute(
            "SELECT name FROM authz_roles WHERE name IN (?, ?)",
            (role, included_role),
        ).fetchall()
        existing = {r["name"] for r in rows}
        if role not in existing:
            raise RoleMappingError(f"role {role!r} not defined")
        if included_role not in existing:
            raise RoleMappingError(f"included role {included_role!r} not defined")

        # Cycle detection: walk the existing includes graph + the prospective
        # new edge from `included_role` and see if we hit `role`.
        edges_rows = self._conn.execute(
            "SELECT role_name, included_role FROM authz_role_includes"
        ).fetchall()
        adj: dict[str, list[str]] = {}
        for r in edges_rows:
            adj.setdefault(r["role_name"], []).append(r["included_role"])
        # Add the prospective edge.
        adj.setdefault(role, []).append(included_role)

        # DFS from role; if we reach role again, there's a cycle.
        visiting: set[str] = set()

        def reaches_self(start: str, current: str) -> bool:
            if current in visiting:
                return False  # already explored
            visiting.add(current)
            for nxt in adj.get(current, []):
                if nxt == start:
                    return True
                if reaches_self(start, nxt):
                    return True
            return False

        if reaches_self(role, included_role):
            raise RoleMappingError(
                f"cycle detected: {role!r} -> ... -> {role!r}"
            )

        # Safe to insert. INSERT OR IGNORE keeps the call idempotent.
        self._conn.execute(
            "INSERT OR IGNORE INTO authz_role_includes(role_name, included_role) VALUES (?, ?)",
            (role, included_role),
        )

    async def remove_include(self, role: str, included_role: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "DELETE FROM authz_role_includes WHERE role_name = ? AND included_role = ?",
                (role, included_role),
            )
```

Replace the existing `remove_role` with one that catches the FK RESTRICT IntegrityError:

```python
    async def remove_role(self, name: str) -> None:
        async with self._lock:
            try:
                await asyncio.to_thread(
                    self._conn.execute,
                    "DELETE FROM authz_roles WHERE name = ?",
                    (name,),
                )
            except sqlite3.IntegrityError as exc:
                raise RoleMappingError(
                    f"role {name!r} is included by other roles; remove the includes first"
                ) from exc
```

- [ ] **Step 4.4: Run the tests to verify they pass**

```
uv run pytest tests/auth/authz/test_role_mapping_store.py -v
```
Expected: all 28 tests pass.

- [ ] **Step 4.5: Type-check**

```
uv run basedpyright --level error src/iris/auth/authz/store.py
```
Expected: 0 errors.

- [ ] **Step 4.6: Commit**

```
git add src/iris/auth/authz/store.py tests/auth/authz/test_role_mapping_store.py
git commit -m "feat(authz): RoleMappingStore include mutators with cycle check

add_include detects cycles app-side via DFS over the existing graph
plus the prospective edge — SQLite can't enforce graph acyclicity,
the YAML loader did this same check in Python. remove_role translates
the FK RESTRICT IntegrityError (when another role still includes it)
into a clean RoleMappingError."
```

---

## Task 5: install_authz_schema bootstrap

**Files:**
- Create: `src/iris/auth/authz/bootstrap.py`
- Create: `tests/auth/authz/test_authz_bootstrap.py`

The bootstrap detects first install (table absent), creates the schema, and seeds the bootstrap role + `clickhouse_admin` + the include edge + the bootstrap user.

- [ ] **Step 5.1: Write the failing tests**

Create `tests/auth/authz/test_authz_bootstrap.py`:

```python
"""install_authz_schema seeds the admin role + clickhouse_admin + bootstrap user
on first install only. Subsequent installs (tables exist) leave content alone.
"""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from iris.auth.authz.bootstrap import install_authz_schema
from iris.auth.authz.store import RoleMappingStore


@dataclass(frozen=True)
class _StubSettings:
    bootstrap_role: str = "admin"
    bootstrap_user: str | None = "alice"


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_first_install_seeds_admin_with_clickhouse_admin_include(tmp_path: Path):
    conn = _open(tmp_path / "auth.db")
    try:
        install_authz_schema(conn, _StubSettings())

        roles = {
            r["name"] for r in conn.execute("SELECT name FROM authz_roles").fetchall()
        }
        assert roles == {"admin", "clickhouse_admin"}

        includes = conn.execute(
            "SELECT role_name, included_role FROM authz_role_includes"
        ).fetchall()
        assert [(r["role_name"], r["included_role"]) for r in includes] == [
            ("admin", "clickhouse_admin")
        ]

        users = conn.execute(
            "SELECT role_name, username_lower FROM authz_role_users"
        ).fetchall()
        assert [(r["role_name"], r["username_lower"]) for r in users] == [
            ("admin", "alice")
        ]
    finally:
        conn.close()


def test_second_install_is_noop_even_with_changed_settings(tmp_path: Path):
    """Once tables exist, the bootstrap function leaves content alone."""
    conn = _open(tmp_path / "auth.db")
    try:
        install_authz_schema(conn, _StubSettings(bootstrap_user="alice"))
        # Operator removes alice via mutator API.
        conn.execute(
            "DELETE FROM authz_role_users WHERE username_lower = 'alice'"
        )
        # Restart with a different bootstrap user — should NOT re-seed.
        install_authz_schema(
            conn, _StubSettings(bootstrap_user="bob")
        )

        users = conn.execute(
            "SELECT username_lower FROM authz_role_users"
        ).fetchall()
        assert users == []  # alice gone; bob NOT added
    finally:
        conn.close()


def test_bootstrap_user_unset_skips_seeding(tmp_path: Path):
    """Fresh DB but operator chose not to seed."""
    conn = _open(tmp_path / "auth.db")
    try:
        install_authz_schema(conn, _StubSettings(bootstrap_user=None))

        roles = conn.execute("SELECT name FROM authz_roles").fetchall()
        assert roles == []
        users = conn.execute("SELECT * FROM authz_role_users").fetchall()
        assert users == []
    finally:
        conn.close()


def test_custom_bootstrap_role_name(tmp_path: Path):
    conn = _open(tmp_path / "auth.db")
    try:
        install_authz_schema(
            conn, _StubSettings(bootstrap_role="superuser", bootstrap_user="alice")
        )
        roles = {
            r["name"] for r in conn.execute("SELECT name FROM authz_roles").fetchall()
        }
        assert roles == {"superuser", "clickhouse_admin"}
        includes = conn.execute(
            "SELECT role_name, included_role FROM authz_role_includes"
        ).fetchall()
        assert [(r["role_name"], r["included_role"]) for r in includes] == [
            ("superuser", "clickhouse_admin")
        ]
    finally:
        conn.close()


def test_username_lowercased(tmp_path: Path):
    conn = _open(tmp_path / "auth.db")
    try:
        install_authz_schema(conn, _StubSettings(bootstrap_user="Alice"))
        users = conn.execute(
            "SELECT username_lower FROM authz_role_users"
        ).fetchall()
        assert [r["username_lower"] for r in users] == ["alice"]
    finally:
        conn.close()


def test_clickhouse_admin_string_matches_clickhouse_module():
    """Drift check: the hardcoded string in bootstrap.py must match the
    constant in iris.clickhouse.deps. If clickhouse renames the constant,
    this test fails and the bootstrap must be updated."""
    from iris.auth.authz import bootstrap
    from iris.clickhouse.deps import CLICKHOUSE_ADMIN_ROLE

    assert bootstrap._CLICKHOUSE_ADMIN_ROLE == CLICKHOUSE_ADMIN_ROLE


def test_works_with_role_mapping_store_after_bootstrap(tmp_path: Path):
    """End-to-end: install_authz_schema then use a RoleMappingStore against
    the same DB; the seeded data is visible via get_mapping."""
    db_path = tmp_path / "auth.db"
    conn = _open(db_path)
    try:
        install_authz_schema(conn, _StubSettings())
    finally:
        conn.close()

    store = RoleMappingStore(path=str(db_path))
    try:
        mapping = asyncio.run(store.get_mapping())
        assert "admin" in mapping.roles
        assert "clickhouse_admin" in mapping.roles
        assert mapping.roles["admin"].includes == ("clickhouse_admin",)
        assert mapping.roles["admin"].users_lower == frozenset({"alice"})
        # Closure: admin transitively includes clickhouse_admin.
        assert mapping.closure["admin"] == frozenset({"admin", "clickhouse_admin"})
    finally:
        asyncio.run(store.close())
```

- [ ] **Step 5.2: Run the tests to verify they fail**

```
uv run pytest tests/auth/authz/test_authz_bootstrap.py -v
```
Expected: ImportError on `iris.auth.authz.bootstrap`.

- [ ] **Step 5.3: Create `src/iris/auth/authz/bootstrap.py`**

```python
"""First-install bootstrap for the authz tables.

install_authz_schema(conn, settings) detects whether authz_roles already
exists. If it doesn't, the function creates the schema AND seeds the
configured admin role with `clickhouse_admin` as an include and the
configured user as a member. If the table already exists, only the
schema is (idempotently) ensured — no content is touched.

The string "clickhouse_admin" is hardcoded here. It MUST match the
constant `CLICKHOUSE_ADMIN_ROLE` in `iris.clickhouse.deps`. The drift
check in tests/auth/authz/test_authz_bootstrap.py asserts equality.
"""
from __future__ import annotations

import sqlite3
from typing import Protocol

from iris.auth.authz.store import _AUTHZ_SCHEMA

# Must match iris.clickhouse.deps.CLICKHOUSE_ADMIN_ROLE.
_CLICKHOUSE_ADMIN_ROLE = "clickhouse_admin"


class _BootstrapSettings(Protocol):
    bootstrap_role: str
    bootstrap_user: str | None


def install_authz_schema(
    conn: sqlite3.Connection, settings: _BootstrapSettings
) -> None:
    """Create the authz schema and (on first install) seed the bootstrap user.

    First install is detected by `authz_roles` not existing yet. After the
    schema runs, the table exists, so subsequent calls leave content alone.
    """
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='authz_roles'"
    ).fetchone() is not None

    conn.executescript(_AUTHZ_SCHEMA)

    if table_exists:
        return

    if not settings.bootstrap_user:
        return

    role = settings.bootstrap_role
    user_lower = settings.bootstrap_user.lower()

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

- [ ] **Step 5.4: Run the tests to verify they pass**

```
uv run pytest tests/auth/authz/test_authz_bootstrap.py -v
```
Expected: all 7 tests pass.

- [ ] **Step 5.5: Type-check**

```
uv run basedpyright --level error src/iris/auth/authz/bootstrap.py tests/auth/authz/test_authz_bootstrap.py
```
Expected: 0 errors.

- [ ] **Step 5.6: Commit**

```
git add src/iris/auth/authz/bootstrap.py tests/auth/authz/test_authz_bootstrap.py
git commit -m "feat(authz): install_authz_schema bootstrap helper

Detects first install via sqlite_master lookup. On first install, seeds
the configured admin role (default 'admin') with 'clickhouse_admin' as
an include edge and the configured user as a member. Subsequent calls
only ensure the schema exists; content is left alone. The user can clear
the bootstrap env vars after first install — they're inert from then on.

The 'clickhouse_admin' string is hardcoded; a test asserts it matches
iris.clickhouse.deps.CLICKHOUSE_ADMIN_ROLE so any drift is caught."
```

---

## Task 6: Cutover — wire RoleMappingStore into install + retarget tests + delete dead code

**Files:**
- Modify: `src/iris/auth/config.py`
- Modify: `src/iris/auth/routes.py`
- Modify: `src/iris/auth/authz/core.py`
- Modify: `src/iris/auth/authz/mapping.py`
- Modify: `src/iris/app.py`
- Modify: `tests/conftest.py`
- Modify: `tests/auth/test_session_dep.py`
- Modify: `tests/auth/authz/test_authz_deps.py`
- Delete: `src/iris/auth/authz/loader.py`
- Delete: `src/iris/auth/authz/config.py`
- Delete: `tests/auth/authz/test_loader.py`
- Delete: `tests/auth/authz/test_authz_config.py`
- Delete: `tests/auth/authz/test_install_wiring.py`
- Delete: `tests/auth/authz/test_mapping.py`

This is the big one. The cutover has to be atomic — partially-wired state means the test suite is broken across multiple commits. Steps below in execution order.

- [ ] **Step 6.1: Add `bootstrap_role` and `bootstrap_user` to `AuthSettings`**

In `src/iris/auth/config.py`:

```python
@dataclass(frozen=True)
class AuthSettings:
    method: Literal["oauth", "ldap", "mock"]
    cookie_name: str
    ttl_seconds: int
    absolute_ttl_seconds: int
    max_per_user: int
    cookie_secure: bool
    auth_db_path: str
    bootstrap_role: str           # NEW
    bootstrap_user: str | None    # NEW
    oidc: OIDCSettings | None
    ldap: LDAPSettings | None
    mock: MockSettings | None

    @classmethod
    def from_env(cls) -> AuthSettings:
        # ... existing parsing ...
        auth_db_path = (
            os.environ.get("AUTH_DB_PATH", "").strip() or "./iris-auth.db"
        )
        bootstrap_role = (                                          # NEW
            os.environ.get("AUTHZ_BOOTSTRAP_ROLE", "").strip() or "admin"
        )
        bootstrap_user = os.environ.get("AUTHZ_BOOTSTRAP_USER", "").strip() or None  # NEW

        # ... existing oidc/ldap/mock branches ...

        return cls(
            method=method,
            cookie_name=cookie_name,
            ttl_seconds=ttl_seconds,
            absolute_ttl_seconds=absolute_ttl_seconds,
            max_per_user=max_per_user,
            cookie_secure=cookie_secure,
            auth_db_path=auth_db_path,
            bootstrap_role=bootstrap_role,        # NEW
            bootstrap_user=bootstrap_user,        # NEW
            oidc=oidc,
            ldap=ldap,
            mock=mock,
        )
```

- [ ] **Step 6.2: Update `core.current_mapping` to read from the store**

In `src/iris/auth/authz/core.py`:

```python
async def current_mapping(request: Request) -> RoleMapping:
    return await request.app.state.authz_store.get_mapping()
```

(The function is already `async`. Body change only.)

- [ ] **Step 6.3: Update `routes.install`**

In `src/iris/auth/routes.py`, replace the YAML loader + `AuthzSettings` block with the store + bootstrap.

Old (around line 173):

```python
def install(app: FastAPI) -> None:
    """Wire the auth package into a FastAPI app: settings, store, exception handlers, router."""
    from iris.auth.config import AuthSettings
    from iris.auth.authz.config import AuthzSettings
    from iris.auth.authz.loader import RoleMappingLoader
    from iris.auth.deps import set_session_store, set_settings
    from iris.auth.exceptions import install_exception_handlers
    from iris.auth.providers import build_provider

    settings = AuthSettings.from_env()
    authz_settings = AuthzSettings.from_env()
    loader = RoleMappingLoader(authz_settings.config_path)
    loader.get()  # eager initial load; bad YAML stops the app from booting
    app.state.authz_loader = loader
```

New:

```python
def install(app: FastAPI) -> None:
    """Wire the auth package into a FastAPI app: settings, store, exception handlers, router."""
    import sqlite3
    from iris.auth.config import AuthSettings
    from iris.auth.authz.bootstrap import install_authz_schema
    from iris.auth.authz.store import RoleMappingStore
    from iris.auth.deps import set_session_store, set_settings
    from iris.auth.exceptions import install_exception_handlers
    from iris.auth.providers import build_provider

    settings = AuthSettings.from_env()

    # Bootstrap the authz schema on a temporary connection. install_authz_schema
    # is idempotent; on first install it also seeds the bootstrap admin user.
    bootstrap_conn = sqlite3.connect(
        settings.auth_db_path,
        check_same_thread=False,
        isolation_level=None,
    )
    try:
        bootstrap_conn.execute("PRAGMA foreign_keys=ON")
        install_authz_schema(bootstrap_conn, settings)
    finally:
        bootstrap_conn.close()

    # The long-lived RoleMappingStore opens its own connection, runs the
    # schema again (no-op on the second pass), and serves get_mapping calls.
    authz_store = RoleMappingStore(path=settings.auth_db_path)
    app.state.authz_store = authz_store
    app.state.auth_close_authz_store = authz_store.close
```

(The remainder of `install` — SessionStore setup, `provider`, templates, etc. — is unchanged.)

- [ ] **Step 6.4: Update `_lifespan` in `src/iris/app.py`**

```python
@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    yield
    closer = getattr(app.state, "auth_close_provider", None)
    if closer is not None:
        await closer()
    ch_closer = getattr(app.state, "clickhouse_close_http", None)
    if ch_closer is not None:
        await ch_closer()
    sess_closer = getattr(app.state, "auth_close_session_store", None)
    if sess_closer is not None:
        await sess_closer()
    authz_closer = getattr(app.state, "auth_close_authz_store", None)   # NEW
    if authz_closer is not None:
        await authz_closer()
```

- [ ] **Step 6.5: Strip `mapping.py` of YAML code**

Replace `src/iris/auth/authz/mapping.py` entirely:

```python
"""Value types and graph helpers for the authz role mapping.

The YAML parser used to live here. After the SQLite cutover, the store
in iris.auth.authz.store builds RoleDef / RoleMapping by querying the DB,
then computes the closure via _compute_closure below. The regex and
RoleMappingError are reused by store.py and bootstrap.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_ROLE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class RoleMappingError(ValueError):
    """Raised when the role mapping fails to load or validate."""


@dataclass(frozen=True, slots=True)
class RoleDef:
    name: str
    groups: frozenset[str]
    users_lower: frozenset[str]
    includes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoleMapping:
    roles: dict[str, RoleDef]
    closure: dict[str, frozenset[str]]


def _compute_closure(roles: dict[str, RoleDef]) -> dict[str, frozenset[str]]:
    closure: dict[str, frozenset[str]] = {}
    visiting: set[str] = set()

    def visit(name: str) -> frozenset[str]:
        if name in closure:
            return closure[name]
        if name in visiting:
            raise RoleMappingError(f"cycle detected involving role {name!r}")
        visiting.add(name)
        try:
            result = {name}
            for inc in roles[name].includes:
                result |= visit(inc)
        finally:
            visiting.discard(name)
        frozen = frozenset(result)
        closure[name] = frozen
        return frozen

    for name in roles:
        visit(name)
    return closure
```

- [ ] **Step 6.6: Update `tests/conftest.py`**

Replace the `_AUTHZ_FIXTURE` YAML write block with bootstrap env vars:

Find this section:

```python
# Write a fixture role mapping that maps the mock user's groups into roles
# so authed_client can hit role-gated routes. Lives in a tempfile that's
# not cleaned up — leaks one file per test session, acceptable for v1.
_AUTHZ_FIXTURE = """\
roles:
  reader:
    groups: []
    users: []
  writer:
    groups: []
    users: []
    includes: ["reader"]
  admin:
    groups: ["admins"]
    users: []
    includes: ["writer"]
  clickhouse_admin:
    groups: ["admins"]
    users: []
"""

_authz_path = os.path.join(tempfile.gettempdir(), "iris-test-authz.yaml")
with open(_authz_path, "w") as f:
    f.write(_AUTHZ_FIXTURE)
os.environ.setdefault("AUTHZ_CONFIG_PATH", _authz_path)
```

Replace with:

```python
# The conftest seeds a single bootstrap admin user that matches the
# MOCK_USERNAME above. Any test that wants additional roles or richer
# fixtures builds them via RoleMappingStore mutators in its own fixture.
os.environ.setdefault("AUTHZ_BOOTSTRAP_ROLE", "admin")
os.environ.setdefault("AUTHZ_BOOTSTRAP_USER", "alice")
```

Also remove the `import tempfile` at the top of conftest.py if it's no longer used.

- [ ] **Step 6.7: Retarget `tests/auth/test_session_dep.py`**

Replace the YAML fixture and `_build_app` with a store-based setup:

```python
import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from iris.auth import Session, OptionalSession
from iris.auth.authz.store import RoleMappingStore
from iris.auth.deps import set_session_store, set_settings
from iris.auth.exceptions import install_exception_handlers
from iris.auth.identity import User
from iris.auth.sessions import SessionStore


def _seed_authz_fixture(store: RoleMappingStore) -> None:
    """reader -> writer -> admin closure; admin gated on the 'admins' group."""
    asyncio.run(store.add_role("reader"))
    asyncio.run(store.add_role("writer"))
    asyncio.run(store.add_role("admin"))
    asyncio.run(store.add_include("writer", "reader"))
    asyncio.run(store.add_include("admin", "writer"))
    asyncio.run(store.add_group_to_role("admin", "admins"))


def _build_app(tmp_path: Path) -> tuple[FastAPI, SessionStore, RoleMappingStore]:
    app = FastAPI()
    db_path = tmp_path / "sessions.db"
    sess_store = SessionStore(
        path=str(db_path), ttl_seconds=60, absolute_ttl_seconds=3600
    )
    set_session_store(app, sess_store)
    set_settings(app, cookie_name="iris_session")
    install_exception_handlers(app, cookie_name="iris_session")

    authz_store = RoleMappingStore(path=str(db_path))
    _seed_authz_fixture(authz_store)
    app.state.authz_store = authz_store

    @app.get("/me")
    async def me(session: Session):
        return {"subject": session.user.subject}

    @app.get("/optional")
    async def optional(session: OptionalSession):
        return {"present": session is not None}

    @app.get("/whoami-full")
    async def whoami_full(session: Session):
        return {
            "id": session.id,
            "subject": session.user.subject,
            "data_keys": sorted(session.data.keys()),
            "roles": sorted(session.roles),
        }

    @app.get("/data")
    async def read_data(session: Session):
        return {"counter": session.data.get("counter", 0)}

    @app.post("/data")
    async def bump_data(request: Request, session: Session):
        session.data["counter"] = session.data.get("counter", 0) + 1
        await request.app.state.auth_session_store.update_data(
            session.id, session.data
        )
        return {"counter": session.data["counter"]}

    return app, sess_store, authz_store


def _seed(store: SessionStore, **overrides) -> str:
    user = User(
        subject=overrides.get("subject", "alice"),
        username=overrides.get("username", overrides.get("subject", "alice")),
        display_name=overrides.get("display_name", "Alice"),
        groups=overrides.get("groups", ("admins",)),
    )
    session = asyncio.run(store.create(user))
    return session.id


def _close(sess_store: SessionStore, authz_store: RoleMappingStore) -> None:
    asyncio.run(sess_store.close())
    asyncio.run(authz_store.close())


def test_no_credentials_returns_401(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        r = TestClient(app).get("/me", headers={"accept": "application/json"})
        assert r.status_code == 401
    finally:
        _close(sess_store, authz_store)


def test_cookie_credential_resolves_session(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/me", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"subject": "alice"}
    finally:
        _close(sess_store, authz_store)


def test_bearer_credential_resolves_session(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)
        r = TestClient(app).get(
            "/me",
            headers={"accept": "application/json", "authorization": f"Bearer {sid}"},
        )
        assert r.status_code == 200
        assert r.json() == {"subject": "alice"}
    finally:
        _close(sess_store, authz_store)


def test_optional_session_returns_none_when_unauthenticated(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        r = TestClient(app).get("/optional", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"present": False}
    finally:
        _close(sess_store, authz_store)


def test_optional_session_returns_session_when_authenticated(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/optional", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"present": True}
    finally:
        _close(sess_store, authz_store)


def test_session_data_round_trip(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        assert c.get("/data").json() == {"counter": 0}
        assert c.post("/data").json() == {"counter": 1}
        assert c.post("/data").json() == {"counter": 2}
        assert c.get("/data").json() == {"counter": 2}
    finally:
        _close(sess_store, authz_store)


def test_session_data_isolated_between_sessions(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid_a = _seed(sess_store, subject="alice")
        sid_b = _seed(sess_store, subject="bob")
        ca = TestClient(app)
        ca.cookies.set("iris_session", sid_a)
        cb = TestClient(app)
        cb.cookies.set("iris_session", sid_b)
        ca.post("/data")
        ca.post("/data")
        cb.post("/data")
        assert ca.get("/data").json() == {"counter": 2}
        assert cb.get("/data").json() == {"counter": 1}
    finally:
        _close(sess_store, authz_store)


def test_session_data_requires_auth(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        r = TestClient(app).get("/data", headers={"accept": "application/json"})
        assert r.status_code == 401
    finally:
        _close(sess_store, authz_store)


def test_session_exposes_id_user_and_data(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        c.post("/data")
        r = c.get("/whoami-full", headers={"accept": "application/json"})
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == sid
        assert body["subject"] == "alice"
        assert body["data_keys"] == ["counter"]
    finally:
        _close(sess_store, authz_store)


def test_session_roles_includes_closure(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, subject="charlie", groups=("admins",))
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/whoami-full", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json()["roles"] == ["admin", "reader", "writer"]
    finally:
        _close(sess_store, authz_store)


def test_session_roles_empty_for_user_without_match(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, subject="dave", groups=("strangers",))
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/whoami-full", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json()["roles"] == []
    finally:
        _close(sess_store, authz_store)
```

- [ ] **Step 6.8: Retarget `tests/auth/authz/test_authz_deps.py`**

Same pattern. Replace the file:

```python
import asyncio
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from iris.auth import Session as RequireSession
from iris.auth.authz.deps import require_role
from iris.auth.authz.store import RoleMappingStore
from iris.auth.deps import set_session_store, set_settings
from iris.auth.exceptions import install_exception_handlers
from iris.auth.identity import User
from iris.auth.session import Session
from iris.auth.sessions import SessionStore


def _seed_authz_fixture(store: RoleMappingStore) -> None:
    """Mirror the previous YAML fixture:
       reader: groups=[], users=[]
       writer: groups=["editors"], users=["bob"], includes=["reader"]
       admin:  groups=["admins"], users=["Alice"], includes=["writer"]
    """
    asyncio.run(store.add_role("reader"))
    asyncio.run(store.add_role("writer"))
    asyncio.run(store.add_role("admin"))
    asyncio.run(store.add_include("writer", "reader"))
    asyncio.run(store.add_include("admin", "writer"))
    asyncio.run(store.add_group_to_role("writer", "editors"))
    asyncio.run(store.add_user_to_role("writer", "bob"))
    asyncio.run(store.add_group_to_role("admin", "admins"))
    asyncio.run(store.add_user_to_role("admin", "Alice"))


def _build_app(tmp_path: Path) -> tuple[FastAPI, SessionStore, RoleMappingStore]:
    app = FastAPI()
    db_path = tmp_path / "auth.db"
    sess_store = SessionStore(
        path=str(db_path), ttl_seconds=60, absolute_ttl_seconds=3600
    )
    set_session_store(app, sess_store)
    set_settings(app, cookie_name="iris_session")
    install_exception_handlers(app, cookie_name="iris_session")

    authz_store = RoleMappingStore(path=str(db_path))
    _seed_authz_fixture(authz_store)
    app.state.authz_store = authz_store

    @app.get("/reader-only")
    async def reader_only(session: Session = Depends(require_role("reader"))):
        return {"subject": session.user.subject}

    @app.get("/admin-only")
    async def admin_only(session: Session = Depends(require_role("admin"))):
        return {"subject": session.user.subject}

    @app.get("/needs-undefined-role")
    async def needs_undefined(session: Session = Depends(require_role("super_admin"))):
        return {"subject": session.user.subject}

    @app.get("/my-roles")
    async def my_roles(session: RequireSession):
        return {"roles": sorted(session.roles)}

    return app, sess_store, authz_store


def _seed(store: SessionStore, *, username: str, groups: tuple[str, ...]) -> str:
    user = User(
        subject=f"mock:{username}",
        username=username,
        display_name=username.title(),
        groups=groups,
    )
    session = asyncio.run(store.create(user))
    return session.id


def _close(sess_store: SessionStore, authz_store: RoleMappingStore) -> None:
    asyncio.run(sess_store.close())
    asyncio.run(authz_store.close())


def test_admin_via_group_reaches_reader_only_route(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, username="charlie", groups=("admins",))
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/reader-only", headers={"accept": "application/json"})
        assert r.status_code == 200
    finally:
        _close(sess_store, authz_store)


def test_writer_via_username_reaches_reader_only_route(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, username="bob", groups=())
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/reader-only", headers={"accept": "application/json"})
        assert r.status_code == 200
    finally:
        _close(sess_store, authz_store)


def test_username_match_is_case_insensitive(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, username="alice", groups=())
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/admin-only", headers={"accept": "application/json"})
        assert r.status_code == 200
    finally:
        _close(sess_store, authz_store)


def test_user_with_no_matching_role_is_forbidden(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, username="dave", groups=("strangers",))
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/reader-only", headers={"accept": "application/json"})
        assert r.status_code == 403
    finally:
        _close(sess_store, authz_store)


def test_unauthenticated_user_gets_401(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        r = TestClient(app).get(
            "/reader-only", headers={"accept": "application/json"}
        )
        assert r.status_code == 401
    finally:
        _close(sess_store, authz_store)


def test_route_requiring_undefined_role_returns_500(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, username="alice", groups=("admins",))
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/needs-undefined-role", headers={"accept": "application/json"})
        assert r.status_code == 500
        assert "super_admin" not in r.text
    finally:
        _close(sess_store, authz_store)


def test_session_roles_returns_full_effective_set_for_admin(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, username="charlie", groups=("admins",))
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/my-roles", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"roles": ["admin", "reader", "writer"]}
    finally:
        _close(sess_store, authz_store)


def test_session_roles_returns_empty_set_for_user_with_no_match(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, username="nobody", groups=())
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/my-roles", headers={"accept": "application/json"})
        assert r.json() == {"roles": []}
    finally:
        _close(sess_store, authz_store)
```

- [ ] **Step 6.9: Delete dead files**

```
rm src/iris/auth/authz/loader.py
rm src/iris/auth/authz/config.py
rm tests/auth/authz/test_loader.py
rm tests/auth/authz/test_authz_config.py
rm tests/auth/authz/test_install_wiring.py
rm tests/auth/authz/test_mapping.py
```

- [ ] **Step 6.10: Run the full suite**

```
uv run pytest --ignore=tests/auth/integration
```
Expected: all tests pass. Test count drops by however many were in the deleted YAML-loader files; `test_role_mapping_store.py` and `test_authz_bootstrap.py` provide more than enough coverage to compensate.

- [ ] **Step 6.11: Type-check at error and warning levels**

```
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: 0 errors, 0 warnings.

- [ ] **Step 6.12: Commit**

```
git add -A
git commit -m "feat(authz): cut over from authz.yaml to RoleMappingStore

routes.install constructs RoleMappingStore against AUTH_DB_PATH, runs
install_authz_schema (which seeds the bootstrap admin on first install
only), and registers app.state.authz_store + the close hook on lifespan.

current_mapping reads from app.state.authz_store.get_mapping().

mapping.py keeps the value types + closure helper; drops the YAML
parser, _NoDuplicatesSafeLoader, and the pyright suppression header.

tests/conftest.py drops the YAML temp-file write, sets
AUTHZ_BOOTSTRAP_USER=alice. test_session_dep.py and
authz/test_authz_deps.py retarget _build_app to seed roles via
RoleMappingStore mutators.

Deletes loader.py, authz/config.py, and the YAML-era test files."
```

---

## Task 7: Drop pyyaml dep + update CLAUDE.md

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `CLAUDE.md`

- [ ] **Step 7.1: Drop the pyyaml dep**

```
uv remove pyyaml
```

This rewrites `pyproject.toml` (removes the `pyyaml` line under `dependencies`) and updates `uv.lock`.

- [ ] **Step 7.2: Confirm no remaining `import yaml` references**

```
grep -rn "import yaml\|from yaml" src/ tests/
```
Expected: no output.

- [ ] **Step 7.3: Update CLAUDE.md — env vars**

Find the auth section's "Configuration" subsection. The current env-var block has these lines (among others):

```
SESSION_DB_PATH=./iris-auth.db   # SQLite file backing the session store; :memory: for tests
COOKIE_SECURE=true
AUTHZ_CONFIG_PATH=./authz.yaml   # role mapping; required, fail-loud if unset
```

Replace with:

```
AUTH_DB_PATH=./iris-auth.db                # SQLite file backing both sessions and authz tables; :memory: for tests
COOKIE_SECURE=true
AUTHZ_BOOTSTRAP_ROLE=admin                 # default: "admin"; the role created on first install of an empty DB
AUTHZ_BOOTSTRAP_USER=                      # if set, the named user is added to the bootstrap role on first install
```

(Note `SESSION_DB_PATH` was already renamed in Task 1 — the env-var documentation block had the old name; replace it with `AUTH_DB_PATH`.)

- [ ] **Step 7.4: Update CLAUDE.md — replace YAML schema docs with the new authz section**

Find the `### Authorization (roles)` heading and replace its body (through the end of that section, just before `### Configuration` or whatever follows) with:

```markdown
### Authorization (roles)

Application code references **internal role names only** (`admin`, `writer`, `reader`, etc.). The mapping from role → external IdP groups/usernames lives in SQLite, in the same `AUTH_DB_PATH` file as the session store. Routes never reference IdP group names directly; they use `Depends(require_role("admin"))`. Operators edit the mapping via the `RoleMappingStore` API (CLI / future admin routes); no file edits, no app restart.

**Schema** (four tables, `authz_*` prefix):

```sql
CREATE TABLE authz_roles (
    name TEXT PRIMARY KEY                     -- regex: [a-zA-Z0-9_-]+
);

CREATE TABLE authz_role_groups (
    role_name  TEXT NOT NULL,
    group_name TEXT NOT NULL,
    PRIMARY KEY (role_name, group_name),
    FOREIGN KEY (role_name) REFERENCES authz_roles(name) ON DELETE CASCADE
);

CREATE TABLE authz_role_users (
    role_name      TEXT NOT NULL,
    username_lower TEXT NOT NULL,             -- case-insensitive: stored lowercased
    PRIMARY KEY (role_name, username_lower),
    FOREIGN KEY (role_name) REFERENCES authz_roles(name) ON DELETE CASCADE
);

CREATE TABLE authz_role_includes (
    role_name     TEXT NOT NULL,
    included_role TEXT NOT NULL,
    PRIMARY KEY (role_name, included_role),
    FOREIGN KEY (role_name)     REFERENCES authz_roles(name) ON DELETE CASCADE,
    FOREIGN KEY (included_role) REFERENCES authz_roles(name) ON DELETE RESTRICT
);
```

`ON DELETE CASCADE` on the child tables means dropping a role removes its assignments automatically. `ON DELETE RESTRICT` on `included_role` prevents deleting a role that another role still includes (matches the previous YAML loader's "includes must reference defined roles" rule). Cycles are rejected app-side on `add_include` — SQLite can't enforce graph acyclicity.

**Mutator API** (in `iris.auth.authz.store.RoleMappingStore`, exposed on `app.state.authz_store`):

```python
await store.add_role(name)
await store.remove_role(name)             # raises if another role includes it
await store.add_group_to_role(role, group)
await store.remove_group_from_role(role, group)
await store.add_user_to_role(role, username)         # username lowercased on storage
await store.remove_user_from_role(role, username)
await store.add_include(role, included_role)        # cycle-checked app-side
await store.remove_include(role, included_role)
```

Each mutator validates inputs (role names against `[a-zA-Z0-9_-]+`) and translates SQLite FK violations into `RoleMappingError` with a clean message. `add_*` are idempotent (`INSERT OR IGNORE`).

**Use in routes:**

```python
@app.get("/docs")
async def list_docs(session: Session = Depends(require_role("reader"))):
    ...
```

For routes that want bare auth and need to read roles:

```python
@app.get("/me/roles")
async def my_roles(session: Session):
    return {"roles": sorted(session.roles)}
```

`require_role("reader")` admits any user whose effective role set contains `reader`, directly or via `includes` (so admins and writers get in too). `session.roles` returns the user's full effective role set as a `frozenset[str]`.

If a route names a role that isn't defined in the DB, the request returns **500** (not 403) with a generic body — same `AuthorizationMisconfigured` flow as before. Operator typos like `require_role("reder")` fail loud.

**Bootstrap (first install only):**

Two env vars seed the initial admin user:

```
AUTHZ_BOOTSTRAP_ROLE=admin       # default: "admin"
AUTHZ_BOOTSTRAP_USER=alice       # if unset, no bootstrap
```

`install_authz_schema` runs at app boot. If `authz_roles` doesn't yet exist, it creates the schema AND seeds:
- a row in `authz_roles` for the bootstrap role (default `admin`)
- a row in `authz_roles` for `clickhouse_admin` (so the include FK has somewhere to point)
- an include edge `(admin → clickhouse_admin)` so the seeded user immediately gets ClickHouse admin powers
- a row in `authz_role_users` adding the bootstrap user to the admin role

Once tables exist, the function only ensures the schema (idempotent) and leaves content alone. Operators can rename/delete the bootstrap role, change includes, remove the bootstrap user — restart won't fight them. Wiping the DB file re-triggers bootstrap.

If `AUTHZ_BOOTSTRAP_USER` is unset on a fresh DB, the tables are empty. Role-gated routes 500 until the operator populates the mapping via `app.state.authz_store` calls.

The hardcoded string `"clickhouse_admin"` in `iris.auth.authz.bootstrap` must match `iris.clickhouse.deps.CLICKHOUSE_ADMIN_ROLE`. A drift test in `tests/auth/authz/test_authz_bootstrap.py` asserts equality.
```

Also: scrub the obsolete sentences elsewhere in CLAUDE.md that mentioned the YAML loader's mtime cache or "robustness against bad edits." Search for `mtime` and `last-known-good` references and remove them.

- [ ] **Step 7.5: Run the full suite + type-check + ruff**

```
uv run pytest --ignore=tests/auth/integration
uv run basedpyright --level error
uv run basedpyright --level warning
uv run ruff check
```
Expected: all clean.

- [ ] **Step 7.6: Commit**

```
git add pyproject.toml uv.lock CLAUDE.md
git commit -m "docs+chore: drop pyyaml; document SQLite-backed authz mapping

- pyyaml is no longer used; uv remove drops it from dependencies and
  uv.lock. The grep confirms no remaining 'import yaml' references.
- CLAUDE.md: replace AUTHZ_CONFIG_PATH and YAML schema docs with the
  AUTH_DB_PATH + AUTHZ_BOOTSTRAP_* env vars, the four-table schema
  overview, the RoleMappingStore mutator API, and the bootstrap
  semantics. Scrubs the obsolete mtime-cache / last-known-good
  paragraphs."
```

---

## Self-review

**Spec coverage:**

- [x] DB rename `SESSION_DB_PATH` → `AUTH_DB_PATH` — Task 1.
- [x] Four-table schema with FK CASCADE/RESTRICT and indexes — Task 2 (`_AUTHZ_SCHEMA`).
- [x] `RoleMappingStore.get_mapping` with closure computation — Task 2.
- [x] PRAGMAs match SessionStore (WAL, NORMAL, FK, busy_timeout) — Task 2 (`_init_schema`).
- [x] All 8 mutators — Task 3 (6) + Task 4 (2 plus updated remove_role).
- [x] App-side cycle check on `add_include` — Task 4.
- [x] FK RESTRICT on `remove_role` translated to `RoleMappingError` — Task 4.
- [x] Username lowercased on storage and lookup — Task 3.
- [x] Bootstrap detects first install via `sqlite_master` — Task 5.
- [x] Bootstrap seeds admin role + clickhouse_admin + include + user — Task 5.
- [x] Bootstrap is idempotent (no-op on second install) — Task 5.
- [x] Drift test for `clickhouse_admin` constant — Task 5.
- [x] `AUTHZ_BOOTSTRAP_ROLE` / `AUTHZ_BOOTSTRAP_USER` env vars in `AuthSettings` — Task 6.
- [x] `routes.install` constructs store, runs bootstrap, registers close hook — Task 6.
- [x] `current_mapping` becomes async `await store.get_mapping()` — Task 6.
- [x] `_lifespan` closes the store — Task 6.
- [x] Big-bang deletion of loader.py, config.py, parse(), YAML tests — Task 6.
- [x] `tests/conftest.py` and the two retargeted test files — Task 6.
- [x] Drop `pyyaml` dep — Task 7.
- [x] CLAUDE.md updates — Task 7.

**Placeholder scan:** No "TBD"/"add validation"/"similar to Task N"/"implement later" patterns. Every step has complete code.

**Type consistency:**
- `RoleMappingStore.__init__(*, path: str)` — used identically in tests, `routes.install`, retargeted test fixtures.
- Mutator signatures match across Task 3, Task 4, the test fixtures in Task 6, and CLAUDE.md docs in Task 7.
- `app.state.authz_store` is the registration name — set by `routes.install` (Task 6), read by `core.current_mapping` (Task 6) and the retargeted tests (Task 6).
- `app.state.auth_close_authz_store` is the close-hook name — registered in Task 6, invoked in `_lifespan` (Task 6).
- The hardcoded `"clickhouse_admin"` string in `bootstrap.py` (Task 5) is asserted to equal `iris.clickhouse.deps.CLICKHOUSE_ADMIN_ROLE` (Task 5 test).
- `AuthSettings.auth_db_path` (Task 1) is read by `routes.install` (Task 6) for both `SessionStore(path=...)` and `RoleMappingStore(path=...)`.
- `AuthSettings.bootstrap_role` and `.bootstrap_user` (Task 6) are passed to `install_authz_schema(conn, settings)` (Task 5 protocol matches).

