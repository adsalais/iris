# ClickHouse-only Authorization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace iris's two-layer authorization (SQLite role mapping + per-DB admin tables) with a single source of truth in ClickHouse. The session caches a derived `Rights` view used by tier-based dependency aliases.

**Architecture:** Per-database tier roles in CH (`X_DBADMIN`, `X_DBWRITER`, `X_DBREADER`) hold the privileges. Per-user roles (`<username>_USER`) and per-group roles (`<group>_GRP`) get the tier roles granted to them. At login, iris walks `system.role_grants` and `system.grants` for the user's effective role set and stuffs the result into a frozen `Rights` value persisted on the session row. Dependency aliases (`Session`, `SessionRead`, `SessionDatabaseAdmin`, …) gate routes by inspecting `session.rights`. The whole `iris.auth.authz` subpackage and the `clickhouse_database_admins_*` SQLite tables disappear.

**Tech Stack:** Python 3.13, FastAPI, SQLite (sessions only), ClickHouse 26.3 RBAC, clickhouse-connect, httpx, pytest with testcontainers-python for CH.

**Spec:** `docs/superpowers/specs/2026-05-08-clickhouse-only-authz-design.md`

---

## File Structure

### New files

- `src/iris/clickhouse/rights.py` — `derive_rights(client, username, groups) -> Rights`. Walks `system.role_grants` transitively, parses tier-role suffixes, queries `system.grants` for global flags.
- `src/iris/auth/bootstrap.py` — `bootstrap_admin(client, username)`. Idempotent: creates `<username>_USER` and grants `ALL ON *.* WITH GRANT OPTION` if no admin grant exists in CH.

### Modified files

- `src/iris/auth/session.py` — rename `Session` → `AuthSession`. Drop `roles` field. Add `rights: Rights` field. Add `Rights` dataclass with `has_read/has_write/has_admin` methods + serialization helpers.
- `src/iris/auth/sessions.py` — `sessions` table gains a `rights_json` column. New `set_rights(session_id, rights)` method.
- `src/iris/auth/identity.py` — `UserSession` gains `rights: Rights | None`.
- `src/iris/auth/deps.py` — replace `optional_session` / `require_session` with full alias suite (`Session`, `SessionOptional`, `SessionAdmin`, `SessionDatabaseCreator`, `SessionDatabaseAdmin`, `SessionWrite`, `SessionRead`).
- `src/iris/auth/routes.py` — login flows derive rights via `derive_rights` after `init_user_rights` and persist via `store.set_rights`. `/api/whoami` returns rights instead of roles.
- `src/iris/auth/exceptions.py` — drop `AuthorizationMisconfigured` and its handler.
- `src/iris/auth/config.py` — replace `AUTHZ_BOOTSTRAP_USER`/`AUTHZ_BOOTSTRAP_ROLE` with `IRIS_BOOTSTRAP_USER`. Drop `bootstrap_role`.
- `src/iris/auth/__init__.py` — export new public surface.
- `src/iris/clickhouse/grants.py` — add tier-role lifecycle helpers (`create_tier_roles`, `drop_tier_roles`, `tier_role_name`) and tier-grant helpers (`grant_tier_to_user`, `grant_tier_to_group`, `revoke_tier_from_user`, `revoke_tier_from_group`).
- `src/iris/clickhouse/handle.py` — `ClickHouseDatabaseCreatorHandle.create_database` creates tier roles + grants DBADMIN to creator. Drop `db_admin_store`. `ClickHouseDatabaseAdminHandle` methods become tier-role grants (`grant_reader`, `grant_writer`, `add_admin_user`); add `delete_database`. Drop `db_admin_store`/`authz_store` parameters.
- `src/iris/clickhouse/deps.py` — `require_clickhouse_admin` checks `session.rights.is_admin`; `require_clickhouse_database_creator` checks `is_admin or can_create_database`; `require_clickhouse_database_admin` checks `rights.has_admin(database)`. Drop `CLICKHOUSE_ADMIN_ROLE` and `CLICKHOUSE_DATABASE_CREATOR_ROLE` constants.
- `src/iris/clickhouse/install.py` — drop `DatabaseAdminStore` instantiation. Call `bootstrap_admin` after `ensure_service_admin` if `IRIS_BOOTSTRAP_USER` is set. Add post-login hook step that calls `derive_rights` and `store.set_rights`.
- `src/iris/clickhouse/__init__.py` — drop `DatabaseAdminStore`, `CLICKHOUSE_ADMIN_ROLE`, `CLICKHOUSE_DATABASE_CREATOR_ROLE` exports.
- `CLAUDE.md` — replace `Authentication > Authorization (roles)`, `ClickHouse > Per-database admin tier`, and the env-var sections with the new model.

### Deleted files

- `src/iris/auth/authz/` (entire subpackage: `__init__.py`, `mapping.py`, `store.py`, `bootstrap.py`, `core.py`, `deps.py`)
- `src/iris/clickhouse/database_admins.py`
- `tests/auth/authz/` (entire directory)
- `tests/auth/integration/test_authz_store.py` (if present — confirm during Task 13)
- `tests/clickhouse/test_database_admin*.py` (replaced by tier-role tests)

---

## Order of Operations

The plan adds new infrastructure first (Tasks 1-5), then migrates the session model and deps atomically (Tasks 6-8), refactors the CH handles and bridge deps (Tasks 9-11), wires bootstrap (Task 12), deletes the old code (Tasks 13-14), updates documentation and tests (Tasks 15-21).

There is a deliberate breakage window between Task 6 (rename + drop roles) and Task 11 (CH deps stop reading authz_store): the codebase will not compile during this stretch. **Do not commit between Task 6 and Task 11 unless the suite is green at that point.** If executing this plan with subagents, run Tasks 6-11 in a single agent pass.

---

## Task 1: Add `Rights` dataclass + serialization

**Files:**
- Modify: `src/iris/auth/session.py`
- Test: `tests/auth/test_rights.py`

- [ ] **Step 1: Write the failing test**

Create `tests/auth/test_rights.py`:

```python
from iris.auth.session import Rights, rights_from_dict, rights_to_dict


def test_empty_rights_admits_nothing():
    r = Rights(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    assert not r.has_read("finance")
    assert not r.has_write("finance")
    assert not r.has_admin("finance")


def test_is_admin_implies_all():
    r = Rights(
        is_admin=True,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    assert r.has_read("anything")
    assert r.has_write("anything")
    assert r.has_admin("anything")


def test_db_admin_implies_writer_and_reader():
    r = Rights(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset({"finance"}),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    assert r.has_read("finance")
    assert r.has_write("finance")
    assert r.has_admin("finance")
    assert not r.has_read("hr")


def test_db_writer_implies_reader_not_admin():
    r = Rights(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset({"finance"}),
        db_reader=frozenset(),
    )
    assert r.has_read("finance")
    assert r.has_write("finance")
    assert not r.has_admin("finance")


def test_db_reader_only_reads():
    r = Rights(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset({"finance"}),
    )
    assert r.has_read("finance")
    assert not r.has_write("finance")
    assert not r.has_admin("finance")


def test_serialization_roundtrip():
    r = Rights(
        is_admin=True,
        can_create_database=True,
        db_admin=frozenset({"finance"}),
        db_writer=frozenset({"hr", "logs"}),
        db_reader=frozenset({"clickstream"}),
    )
    d = rights_to_dict(r)
    assert d == {
        "is_admin": True,
        "can_create_database": True,
        "db_admin": ["finance"],
        "db_writer": ["hr", "logs"],
        "db_reader": ["clickstream"],
    }
    assert rights_from_dict(d) == r


def test_deserialize_missing_field_defaults_false_or_empty():
    # forward-compat: an older session row written before a field existed must round-trip safely
    r = rights_from_dict({"is_admin": False, "can_create_database": False})
    assert r.db_admin == frozenset()
    assert r.db_writer == frozenset()
    assert r.db_reader == frozenset()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/auth/test_rights.py -v`
Expected: FAIL — `Rights` cannot be imported.

- [ ] **Step 3: Implement `Rights` and serialization in `src/iris/auth/session.py`**

Rewrite the file:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from iris.auth.identity import User


@dataclass(frozen=True, slots=True)
class Rights:
    """Frozen view of a session's effective ClickHouse-derived authorization.

    Computed once at login by ``iris.clickhouse.rights.derive_rights`` and persisted
    on the session row. Routes never re-derive mid-session; operator changes take
    effect on the user's next login.
    """
    is_admin: bool
    can_create_database: bool
    db_admin: frozenset[str]
    db_writer: frozenset[str]
    db_reader: frozenset[str]

    def has_read(self, database: str) -> bool:
        return self.is_admin or database in (
            self.db_admin | self.db_writer | self.db_reader
        )

    def has_write(self, database: str) -> bool:
        return self.is_admin or database in (self.db_admin | self.db_writer)

    def has_admin(self, database: str) -> bool:
        return self.is_admin or database in self.db_admin


@dataclass(frozen=True, slots=True)
class AuthSession:
    """Request-scoped view of a logged-in session.

    Built once per request by the auth dep. Routes receive an ``AuthSession`` via
    one of the ``Annotated`` alias deps in ``iris.auth.deps``: ``Session`` (require
    auth), ``SessionOptional`` (admit None), ``SessionRead``/``SessionWrite``/
    ``SessionDatabaseAdmin`` (database-scoped tier checks via ``rights``),
    ``SessionDatabaseCreator``/``SessionAdmin`` (global flags).

    Frozen except for ``data``: the dict is a per-request snapshot deserialized
    from the SQLite session store. Mutations to the dict do NOT auto-persist —
    call ``await request.app.state.auth_session_store.update_data(session.id,
    session.data)`` to write changes back.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    data: dict[str, Any]
    rights: Rights


def rights_to_dict(r: Rights) -> dict[str, Any]:
    return {
        "is_admin": r.is_admin,
        "can_create_database": r.can_create_database,
        "db_admin": sorted(r.db_admin),
        "db_writer": sorted(r.db_writer),
        "db_reader": sorted(r.db_reader),
    }


def rights_from_dict(d: dict[str, Any]) -> Rights:
    return Rights(
        is_admin=bool(d.get("is_admin", False)),
        can_create_database=bool(d.get("can_create_database", False)),
        db_admin=frozenset(d.get("db_admin", [])),
        db_writer=frozenset(d.get("db_writer", [])),
        db_reader=frozenset(d.get("db_reader", [])),
    )


EMPTY_RIGHTS = Rights(
    is_admin=False,
    can_create_database=False,
    db_admin=frozenset(),
    db_writer=frozenset(),
    db_reader=frozenset(),
)
```

This file will not yet have any callers because `Session` (the old dataclass name) is gone. The build will be broken until Task 6 finishes. **Do not run the full test suite at this point — only the targeted file test.**

- [ ] **Step 4: Run targeted test to verify it passes**

Run: `uv run pytest tests/auth/test_rights.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/auth/test_rights.py src/iris/auth/session.py
git commit -m "feat(auth): add Rights dataclass + AuthSession (rename pending)"
```

NB: the build is broken from this commit until Task 6. That is expected.

---

## Task 2: Tier-role lifecycle helpers

**Files:**
- Modify: `src/iris/clickhouse/grants.py`
- Test: `tests/clickhouse/test_tier_roles.py`

- [ ] **Step 1: Write the failing test**

Create `tests/clickhouse/test_tier_roles.py` (uses the existing CH testcontainer fixture from `tests/clickhouse/conftest.py`):

```python
from iris.clickhouse.grants import (
    TIER_DBADMIN,
    TIER_DBWRITER,
    TIER_DBREADER,
    create_tier_roles,
    drop_tier_roles,
    tier_role_name,
)


def test_tier_role_name_format():
    assert tier_role_name("finance", TIER_DBADMIN) == "finance_DBADMIN"
    assert tier_role_name("finance", TIER_DBWRITER) == "finance_DBWRITER"
    assert tier_role_name("finance", TIER_DBREADER) == "finance_DBREADER"


def test_create_tier_roles_creates_three_roles_and_grants(ch_client, prefix):
    db = f"{prefix}_finance"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    rows = ch_client.query(
        "SELECT name FROM system.roles WHERE name IN "
        "({a:String}, {w:String}, {r:String})",
        parameters={
            "a": f"{db}_DBADMIN",
            "w": f"{db}_DBWRITER",
            "r": f"{db}_DBREADER",
        },
    ).result_rows
    assert {r[0] for r in rows} == {f"{db}_DBADMIN", f"{db}_DBWRITER", f"{db}_DBREADER"}

    # admin role has GRANT ALL ON db.* WITH GRANT OPTION
    admin_grants = ch_client.query(
        "SELECT access_type, grant_option, database FROM system.grants "
        "WHERE role_name = {r:String}",
        parameters={"r": f"{db}_DBADMIN"},
    ).result_rows
    assert any(g[1] == 1 and g[2] == db for g in admin_grants)

    # writer has SELECT, INSERT, ALTER UPDATE; reader has SELECT only
    w_types = {
        row[0]
        for row in ch_client.query(
            "SELECT access_type FROM system.grants WHERE role_name = {r:String}",
            parameters={"r": f"{db}_DBWRITER"},
        ).result_rows
    }
    assert w_types >= {"SELECT", "INSERT", "ALTER UPDATE"}
    r_types = {
        row[0]
        for row in ch_client.query(
            "SELECT access_type FROM system.grants WHERE role_name = {r:String}",
            parameters={"r": f"{db}_DBREADER"},
        ).result_rows
    }
    assert r_types == {"SELECT"}


def test_create_tier_roles_idempotent(ch_client, prefix):
    db = f"{prefix}_idemp"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    create_tier_roles(ch_client, database=db)  # second run must not error


def test_drop_tier_roles_removes_them(ch_client, prefix):
    db = f"{prefix}_drop"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    drop_tier_roles(ch_client, database=db)
    rows = ch_client.query(
        "SELECT count() FROM system.roles WHERE name IN "
        "({a:String}, {w:String}, {r:String})",
        parameters={
            "a": f"{db}_DBADMIN",
            "w": f"{db}_DBWRITER",
            "r": f"{db}_DBREADER",
        },
    ).result_rows
    assert rows[0][0] == 0


def test_drop_tier_roles_idempotent(ch_client, prefix):
    db = f"{prefix}_drop_idemp"
    drop_tier_roles(ch_client, database=db)  # never created — must not error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/clickhouse/test_tier_roles.py -v`
Expected: FAIL — `tier_role_name`, `create_tier_roles`, `drop_tier_roles`, `TIER_*` cannot be imported.

- [ ] **Step 3: Implement helpers in `src/iris/clickhouse/grants.py`**

Append to the existing file:

```python
from typing import Final

TIER_DBADMIN: Final = "DBADMIN"
TIER_DBWRITER: Final = "DBWRITER"
TIER_DBREADER: Final = "DBREADER"

_TIERS: Final = (TIER_DBADMIN, TIER_DBWRITER, TIER_DBREADER)


def tier_role_name(database: str, tier: str) -> str:
    """Return the tier role name for ``database`` and tier (one of ``TIER_DBADMIN``,
    ``TIER_DBWRITER``, ``TIER_DBREADER``)."""
    if tier not in _TIERS:
        raise ValueError(f"unknown tier: {tier!r}")
    return f"{database}_{tier}"


def create_tier_roles(client: Client, *, database: str) -> None:
    """Create the three tier roles for ``database`` and grant their privileges.
    Idempotent. Caller is responsible for ``CREATE DATABASE``."""
    db_q = quote_identifier(database, kind="database")
    admin_role = tier_role_name(database, TIER_DBADMIN)
    writer_role = tier_role_name(database, TIER_DBWRITER)
    reader_role = tier_role_name(database, TIER_DBREADER)
    admin_q = quote_identifier(admin_role, kind="role")
    writer_q = quote_identifier(writer_role, kind="role")
    reader_q = quote_identifier(reader_role, kind="role")
    client.command(f"CREATE ROLE IF NOT EXISTS {admin_q}")
    client.command(f"CREATE ROLE IF NOT EXISTS {writer_q}")
    client.command(f"CREATE ROLE IF NOT EXISTS {reader_q}")
    client.command(f"GRANT ALL ON {db_q}.* TO {admin_q} WITH GRANT OPTION")
    client.command(f"GRANT SELECT, INSERT, ALTER UPDATE ON {db_q}.* TO {writer_q}")
    client.command(f"GRANT SELECT ON {db_q}.* TO {reader_q}")


def drop_tier_roles(client: Client, *, database: str) -> None:
    """Drop the three tier roles for ``database``. Idempotent."""
    admin_q = quote_identifier(tier_role_name(database, TIER_DBADMIN), kind="role")
    writer_q = quote_identifier(tier_role_name(database, TIER_DBWRITER), kind="role")
    reader_q = quote_identifier(tier_role_name(database, TIER_DBREADER), kind="role")
    client.command(f"DROP ROLE IF EXISTS {admin_q}")
    client.command(f"DROP ROLE IF EXISTS {writer_q}")
    client.command(f"DROP ROLE IF EXISTS {reader_q}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/clickhouse/test_tier_roles.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/clickhouse/test_tier_roles.py src/iris/clickhouse/grants.py
git commit -m "feat(clickhouse): tier-role lifecycle helpers"
```

---

## Task 3: Tier-grant helpers (user/group ↔ tier)

**Files:**
- Modify: `src/iris/clickhouse/grants.py`
- Test: `tests/clickhouse/test_tier_grants.py`

- [ ] **Step 1: Write the failing test**

Create `tests/clickhouse/test_tier_grants.py`:

```python
from iris.clickhouse.grants import (
    TIER_DBREADER,
    TIER_DBWRITER,
    create_tier_roles,
    grant_tier_to_group,
    grant_tier_to_user,
    revoke_tier_from_group,
    revoke_tier_from_user,
)
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX


def _granted_role_names(ch_client, *, role_name):
    rows = ch_client.query(
        "SELECT granted_role_name FROM system.role_grants WHERE role_name = {r:String}",
        parameters={"r": role_name},
    ).result_rows
    return {r[0] for r in rows}


def test_grant_tier_to_user_pre_creates_user_role(ch_client, prefix):
    db = f"{prefix}_finance"
    user = f"{prefix}_alice"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    # alice has never logged in — her _USER role does not exist yet
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBREADER, username=user)
    assert f"{db}_DBREADER" in _granted_role_names(
        ch_client, role_name=f"{user}{USER_ROLE_SUFFIX}"
    )


def test_grant_tier_to_group_pre_creates_group_role(ch_client, prefix):
    db = f"{prefix}_finance"
    group = f"{prefix}_engineering"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    grant_tier_to_group(ch_client, database=db, tier=TIER_DBWRITER, group=group)
    assert f"{db}_DBWRITER" in _granted_role_names(
        ch_client, role_name=f"{group}{GROUP_ROLE_SUFFIX}"
    )


def test_grant_tier_idempotent(ch_client, prefix):
    db = f"{prefix}_idemp"
    user = f"{prefix}_bob"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBREADER, username=user)
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBREADER, username=user)


def test_revoke_tier_removes_grant(ch_client, prefix):
    db = f"{prefix}_revoke"
    user = f"{prefix}_carol"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBREADER, username=user)
    revoke_tier_from_user(ch_client, database=db, tier=TIER_DBREADER, username=user)
    assert f"{db}_DBREADER" not in _granted_role_names(
        ch_client, role_name=f"{user}{USER_ROLE_SUFFIX}"
    )


def test_revoke_tier_idempotent_when_not_granted(ch_client, prefix):
    db = f"{prefix}_revoke_idemp"
    user = f"{prefix}_dave"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    # never granted; revoke must not raise
    revoke_tier_from_user(ch_client, database=db, tier=TIER_DBREADER, username=user)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/clickhouse/test_tier_grants.py -v`
Expected: FAIL — `grant_tier_to_user` etc. cannot be imported.

- [ ] **Step 3: Implement helpers in `src/iris/clickhouse/grants.py`**

Append to the file:

```python
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX


def _ensure_role(client: Client, role: str) -> None:
    """``CREATE ROLE IF NOT EXISTS`` — pre-creates the role so grants succeed
    even if the user/group has never authenticated. Closes username enumeration
    via differential CH errors."""
    role_q = quote_identifier(role, kind="role")
    client.command(f"CREATE ROLE IF NOT EXISTS {role_q}")


def grant_tier_to_user(
    client: Client, *, database: str, tier: str, username: str
) -> None:
    """``GRANT <database>_<tier> TO <username>_USER``. Pre-creates the user role
    if it does not yet exist. Idempotent."""
    user_role = f"{username}{USER_ROLE_SUFFIX}"
    _ensure_role(client, user_role)
    user_role_q = quote_identifier(user_role, kind="role")
    tier_q = quote_identifier(tier_role_name(database, tier), kind="role")
    client.command(f"GRANT {tier_q} TO {user_role_q}")


def grant_tier_to_group(
    client: Client, *, database: str, tier: str, group: str
) -> None:
    """``GRANT <database>_<tier> TO <group>_GRP``. Pre-creates the group role
    if it does not yet exist. Idempotent."""
    group_role = f"{group}{GROUP_ROLE_SUFFIX}"
    _ensure_role(client, group_role)
    group_role_q = quote_identifier(group_role, kind="role")
    tier_q = quote_identifier(tier_role_name(database, tier), kind="role")
    client.command(f"GRANT {tier_q} TO {group_role_q}")


def revoke_tier_from_user(
    client: Client, *, database: str, tier: str, username: str
) -> None:
    """``REVOKE <database>_<tier> FROM <username>_USER``. Idempotent — CH no-ops
    when the grant does not exist."""
    user_role = f"{username}{USER_ROLE_SUFFIX}"
    _ensure_role(client, user_role)
    user_role_q = quote_identifier(user_role, kind="role")
    tier_q = quote_identifier(tier_role_name(database, tier), kind="role")
    client.command(f"REVOKE {tier_q} FROM {user_role_q}")


def revoke_tier_from_group(
    client: Client, *, database: str, tier: str, group: str
) -> None:
    """``REVOKE <database>_<tier> FROM <group>_GRP``. Idempotent."""
    group_role = f"{group}{GROUP_ROLE_SUFFIX}"
    _ensure_role(client, group_role)
    group_role_q = quote_identifier(group_role, kind="role")
    tier_q = quote_identifier(tier_role_name(database, tier), kind="role")
    client.command(f"REVOKE {tier_q} FROM {group_role_q}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/clickhouse/test_tier_grants.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/clickhouse/test_tier_grants.py src/iris/clickhouse/grants.py
git commit -m "feat(clickhouse): grant_tier_to_user/group helpers + pre-create roles"
```

---

## Task 4: `derive_rights` function

**Files:**
- Create: `src/iris/clickhouse/rights.py`
- Test: `tests/clickhouse/test_rights_derivation.py`

- [ ] **Step 1: Write the failing test**

Create `tests/clickhouse/test_rights_derivation.py`:

```python
from iris.auth.session import EMPTY_RIGHTS
from iris.clickhouse.grants import (
    TIER_DBADMIN,
    TIER_DBREADER,
    TIER_DBWRITER,
    create_tier_roles,
    grant_tier_to_group,
    grant_tier_to_user,
)
from iris.clickhouse.rights import derive_rights
from iris.clickhouse.users import init_user_rights


def test_user_with_no_grants_has_empty_rights(ch_client, ch_settings, prefix):
    user = f"{prefix}_no_grants"
    init_user_rights(ch_client, username=user, groups=[], settings=ch_settings)
    r = derive_rights(ch_client, username=user, groups=[])
    assert r == EMPTY_RIGHTS


def test_direct_user_grant_produces_reader_label(ch_client, ch_settings, prefix):
    user = f"{prefix}_reader"
    db = f"{prefix}_finance"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    init_user_rights(ch_client, username=user, groups=[], settings=ch_settings)
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBREADER, username=user)
    r = derive_rights(ch_client, username=user, groups=[])
    assert r.db_reader == frozenset({db})
    assert r.db_writer == frozenset()
    assert r.db_admin == frozenset()


def test_group_grant_propagates_to_user(ch_client, ch_settings, prefix):
    user = f"{prefix}_via_group"
    group = f"{prefix}_engineering"
    db = f"{prefix}_logs"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    init_user_rights(ch_client, username=user, groups=[group], settings=ch_settings)
    grant_tier_to_group(ch_client, database=db, tier=TIER_DBWRITER, group=group)
    r = derive_rights(ch_client, username=user, groups=[group])
    assert r.db_writer == frozenset({db})


def test_admin_grant_yields_is_admin(ch_client, ch_settings, prefix):
    user = f"{prefix}_admin"
    init_user_rights(ch_client, username=user, groups=[], settings=ch_settings)
    user_role = f"{user}_USER"
    ch_client.command(f"GRANT ALL ON *.* TO `{user_role}` WITH GRANT OPTION")
    r = derive_rights(ch_client, username=user, groups=[])
    assert r.is_admin is True


def test_create_database_grant_yields_can_create(ch_client, ch_settings, prefix):
    user = f"{prefix}_creator"
    init_user_rights(ch_client, username=user, groups=[], settings=ch_settings)
    user_role = f"{user}_USER"
    ch_client.command(f"GRANT CREATE DATABASE ON *.* TO `{user_role}`")
    r = derive_rights(ch_client, username=user, groups=[])
    assert r.can_create_database is True
    assert r.is_admin is False  # GRANT CREATE DATABASE alone is not admin


def test_db_admin_label_set_when_grant_option_present(ch_client, ch_settings, prefix):
    user = f"{prefix}_dbadmin"
    db = f"{prefix}_owned"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    init_user_rights(ch_client, username=user, groups=[], settings=ch_settings)
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBADMIN, username=user)
    r = derive_rights(ch_client, username=user, groups=[])
    assert r.db_admin == frozenset({db})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/clickhouse/test_rights_derivation.py -v`
Expected: FAIL — `derive_rights` cannot be imported.

- [ ] **Step 3: Implement `src/iris/clickhouse/rights.py`**

```python
"""Derive a session's effective Rights from ClickHouse RBAC at login.

Walks ``system.role_grants`` transitively for the user's effective role set
(``<username>_USER`` plus ``<group>_GRP`` for each group), then queries
``system.grants`` for the global flags. Returns a frozen ``Rights`` value.

Called by the post-login hook in ``iris.clickhouse.install`` exactly once per
real login. Operator changes to grants take effect on the user's next login.
"""
from __future__ import annotations

from typing import cast

from clickhouse_connect.driver.client import Client

from iris.auth.session import Rights
from iris.clickhouse.grants import TIER_DBADMIN, TIER_DBREADER, TIER_DBWRITER
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX

_TIER_SUFFIXES = (
    ("_" + TIER_DBADMIN, TIER_DBADMIN),
    ("_" + TIER_DBWRITER, TIER_DBWRITER),
    ("_" + TIER_DBREADER, TIER_DBREADER),
)


def _effective_role_set(
    client: Client, *, username: str, groups: list[str]
) -> set[str]:
    """Compute the transitive closure of role grants reachable from the user's
    seed roles (``<username>_USER`` plus each ``<group>_GRP``)."""
    seed = {f"{username}{USER_ROLE_SUFFIX}"} | {
        f"{g}{GROUP_ROLE_SUFFIX}" for g in groups
    }
    closed: set[str] = set()
    frontier = set(seed)
    while frontier:
        closed |= frontier
        # role_grants.role_name is the parent; granted_role_name is the child.
        # We want everything reachable downward from frontier roles.
        rows = client.query(
            "SELECT granted_role_name FROM system.role_grants "
            "WHERE role_name IN ({names:Array(String)})",
            parameters={"names": list(frontier)},
        ).result_rows
        next_frontier = {cast(str, r[0]) for r in rows} - closed
        frontier = next_frontier
    return closed


def derive_rights(
    client: Client, *, username: str, groups: list[str]
) -> Rights:
    """Compute the user's ``Rights`` view from CH state.

    Pre-conditions: the user's per-user role and per-group roles must already
    exist in CH. Call after ``init_user_rights``.
    """
    effective = _effective_role_set(client, username=username, groups=groups)

    db_admin: set[str] = set()
    db_writer: set[str] = set()
    db_reader: set[str] = set()
    for role in effective:
        for suffix, tier in _TIER_SUFFIXES:
            if role.endswith(suffix):
                database = role[: -len(suffix)]
                if not database:
                    continue
                if tier == TIER_DBADMIN:
                    db_admin.add(database)
                elif tier == TIER_DBWRITER:
                    db_writer.add(database)
                else:
                    db_reader.add(database)
                break

    is_admin = False
    can_create_database = False
    if effective:
        rows = client.query(
            "SELECT access_type, grant_option, database, table, column "
            "FROM system.grants WHERE role_name IN ({names:Array(String)})",
            parameters={"names": list(effective)},
        ).result_rows
        for access_type, grant_option, database, table, column in rows:
            access_type = cast(str, access_type)
            grant_option = cast(int, grant_option)
            database = cast(str, database) or ""
            table = cast(str, table) or ""
            column = cast(str, column) or ""
            if (
                access_type == "ALL"
                and grant_option == 1
                and database == ""
                and table == ""
                and column == ""
            ):
                is_admin = True
            if access_type == "CREATE DATABASE" and database == "":
                can_create_database = True

    return Rights(
        is_admin=is_admin,
        can_create_database=can_create_database,
        db_admin=frozenset(db_admin),
        db_writer=frozenset(db_writer),
        db_reader=frozenset(db_reader),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/clickhouse/test_rights_derivation.py -v`
Expected: PASS (6 tests).

If `system.grants` does not represent `GRANT ALL` as a single row with `access_type='ALL'` on the testcontainer's CH version, the `is_admin` test will fail. Diagnose by inspecting `SELECT access_type, grant_option FROM system.grants WHERE role_name = ?` for the admin user role and adjust the predicate (CH may expand `ALL` into individual privileges; if so, treat "user has SELECT on *.* with grant_option=1" as the proxy and document in `derive_rights`'s docstring).

- [ ] **Step 5: Commit**

```bash
git add tests/clickhouse/test_rights_derivation.py src/iris/clickhouse/rights.py
git commit -m "feat(clickhouse): derive_rights walks role grants + system.grants"
```

---

## Task 5: `bootstrap_admin` function

**Files:**
- Create: `src/iris/auth/bootstrap.py`
- Test: `tests/auth/test_bootstrap_admin.py`

- [ ] **Step 1: Write the failing test**

Create `tests/auth/test_bootstrap_admin.py`. Reuse the CH testcontainer fixture (the existing `tests/clickhouse/conftest.py` exposes `ch_client` and `prefix`; the file lives under `tests/auth/` so import paths align).

```python
from iris.auth.bootstrap import bootstrap_admin
from iris.clickhouse.rights import derive_rights


def test_bootstrap_creates_admin_when_absent(ch_client, prefix):
    user = f"{prefix}_first_admin"
    bootstrap_admin(ch_client, username=user)
    r = derive_rights(ch_client, username=user, groups=[])
    assert r.is_admin is True


def test_bootstrap_skips_when_admin_exists(ch_client, prefix):
    a = f"{prefix}_existing"
    b = f"{prefix}_second"
    bootstrap_admin(ch_client, username=a)
    bootstrap_admin(ch_client, username=b)
    # Second call must not seed b — it sees the existing admin and skips.
    r = derive_rights(ch_client, username=b, groups=[])
    assert r.is_admin is False


def test_bootstrap_idempotent_for_same_user(ch_client, prefix):
    user = f"{prefix}_repeat"
    bootstrap_admin(ch_client, username=user)
    bootstrap_admin(ch_client, username=user)  # must not error
```

If the existing `tests/clickhouse/conftest.py` fixtures are not visible from `tests/auth/`, copy the fixture wiring into `tests/auth/conftest.py` so the same `ch_client`/`prefix` symbols resolve. Verify by reading `tests/clickhouse/conftest.py` first.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/auth/test_bootstrap_admin.py -v`
Expected: FAIL — `bootstrap_admin` cannot be imported.

- [ ] **Step 3: Implement `src/iris/auth/bootstrap.py`**

```python
"""Bootstrap option β: seed the first ClickHouse admin user at app boot.

Runs at app boot after ``ensure_service_admin``. Idempotent: if any role already
holds the admin marker, the function is a no-op. Wiping the CH server and
restarting iris re-triggers the seed.

The bootstrap user need not exist in the IdP yet. iris creates the corresponding
``<username>_USER`` role in CH and grants it ``ALL ON *.* WITH GRANT OPTION``;
when the operator logs in for the first time, ``init_user_rights`` reuses the
existing role and ``derive_rights`` returns ``is_admin=True``.
"""
from __future__ import annotations

import logging
from typing import cast

from clickhouse_connect.driver.client import Client

from iris.clickhouse.identifiers import quote_identifier
from iris.clickhouse.users import USER_ROLE_SUFFIX

logger = logging.getLogger("iris.auth.bootstrap")


def _admin_exists(client: Client) -> bool:
    rows = client.query(
        "SELECT count() FROM system.grants "
        "WHERE access_type = 'ALL' AND grant_option = 1 "
        "AND database = '' AND table = '' AND column = ''"
    ).result_rows
    return cast(int, rows[0][0]) > 0


def bootstrap_admin(client: Client, *, username: str) -> None:
    """Seed ``<username>_USER`` with admin grants when no admin exists in CH.

    No-op when an admin grant is already present. Idempotent across restarts.
    """
    if _admin_exists(client):
        logger.info("bootstrap: admin already present in CH; skipping seed")
        return
    role = f"{username}{USER_ROLE_SUFFIX}"
    role_q = quote_identifier(role, kind="role")
    client.command(f"CREATE ROLE IF NOT EXISTS {role_q}")
    client.command(f"GRANT ALL ON *.* TO {role_q} WITH GRANT OPTION")
    logger.info("bootstrap: seeded admin role for username=%s", username)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/auth/test_bootstrap_admin.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/auth/test_bootstrap_admin.py src/iris/auth/bootstrap.py
git commit -m "feat(auth): bootstrap_admin seeds first CH admin idempotently"
```

---

## Task 6: Rename `Session` → `AuthSession`, drop `roles`, persist `rights`

**Important:** This task starts the breakage window. The codebase will not compile until Task 11 finishes. Run the suite at the *end* of Task 11, not within Task 6.

**Files:**
- Modify: `src/iris/auth/sessions.py` (schema gains `rights_json` column, `set_rights` method, `_row_to_session` reads rights)
- Modify: `src/iris/auth/identity.py` (`UserSession` gains `rights: Rights`)
- Modify: every site that imports `Session` from `iris.auth.session` (use grep — listed below)
- Test: `tests/auth/test_session_store.py` (or update existing test of the same name; create if absent)

- [ ] **Step 1: Find every reference to the old `Session` name**

Run: `git grep -n 'iris\.auth\.session\|iris\.auth import Session\b\|from iris\.auth import.*\bSession\b' src/ tests/`

Record the file list — every match needs `Session` → `AuthSession` substitution in this task or one of its callers.

- [ ] **Step 2: Update `src/iris/auth/identity.py` — add `rights`**

```python
from __future__ import annotations

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
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    absolute_expires_at: datetime
    data: dict[str, Any] = field(default_factory=dict)
    rights: Rights = EMPTY_RIGHTS
```

Note the import from `iris.auth.session`. There is currently no cycle (session.py imports from identity.py, not the other way). After this change, `session.py` must not import from `identity.py` at module level — `User` is referenced as a forward type. Verify Step 3 below.

- [ ] **Step 3: Verify `src/iris/auth/session.py` has no top-level import from `identity`**

Open `src/iris/auth/session.py`. The current code has:

```python
from iris.auth.identity import User
```

at module top level. Move it inside `TYPE_CHECKING`:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from iris.auth.identity import User
```

The `User` type appears only in the `AuthSession.user` annotation, which is a string under `from __future__ import annotations` (already present), so this is safe.

- [ ] **Step 4: Update `src/iris/auth/sessions.py`**

Replace the whole file:

```python
"""SQLite-backed session store.

One sqlite3.Connection per process. WAL mode + synchronous=NORMAL handle
cross-process locking so multiple uvicorn workers can share a single DB
file. All sync sqlite3 calls are wrapped in asyncio.to_thread to keep the
FastAPI event loop unblocked.

Schema:

    CREATE TABLE sessions (
        id                       TEXT PRIMARY KEY,
        subject                  TEXT NOT NULL,
        username                 TEXT NOT NULL,
        display_name             TEXT NOT NULL,
        groups_json              TEXT NOT NULL,
        created_at_ts            INTEGER NOT NULL,
        expires_at_ts            INTEGER NOT NULL,
        absolute_expires_at_ts   INTEGER NOT NULL,
        data_json                TEXT NOT NULL DEFAULT '{}',
        rights_json              TEXT NOT NULL DEFAULT '{}'
    );

Timestamps are Unix epoch INTEGER. Groups, data, and rights are JSON text.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, UTC
from typing import Any

from iris.auth.identity import User, UserSession
from iris.auth.session import EMPTY_RIGHTS, Rights, rights_from_dict, rights_to_dict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                       TEXT PRIMARY KEY,
    subject                  TEXT NOT NULL,
    username                 TEXT NOT NULL,
    display_name             TEXT NOT NULL,
    groups_json              TEXT NOT NULL,
    created_at_ts            INTEGER NOT NULL,
    expires_at_ts            INTEGER NOT NULL,
    absolute_expires_at_ts   INTEGER NOT NULL,
    data_json                TEXT NOT NULL DEFAULT '{}',
    rights_json              TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sessions_subject ON sessions(subject);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at_ts);
"""


def _to_ts(dt: datetime) -> int:
    return int(dt.timestamp())


def _from_ts(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


def _row_to_session(row: sqlite3.Row) -> UserSession:
    user = User(
        subject=row["subject"],
        username=row["username"],
        display_name=row["display_name"],
        groups=tuple(json.loads(row["groups_json"])),
    )
    rights_raw = json.loads(row["rights_json"]) if row["rights_json"] else {}
    rights = rights_from_dict(rights_raw) if rights_raw else EMPTY_RIGHTS
    return UserSession(
        id=row["id"],
        user=user,
        created_at=_from_ts(row["created_at_ts"]),
        expires_at=_from_ts(row["expires_at_ts"]),
        absolute_expires_at=_from_ts(row["absolute_expires_at_ts"]),
        data=json.loads(row["data_json"]),
        rights=rights,
    )


class SessionStore:
    def __init__(
        self,
        *,
        path: str,
        ttl_seconds: int,
        absolute_ttl_seconds: int,
        max_per_user: int = 10,
    ) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._absolute_ttl = timedelta(seconds=absolute_ttl_seconds)
        self._max_per_user = max_per_user
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
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    async def create(self, user: User) -> UserSession:
        async with self._lock:
            return await asyncio.to_thread(self._create_sync, user)

    def _create_sync(self, user: User) -> UserSession:
        now = datetime.now(UTC)
        session = UserSession(
            id=secrets.token_urlsafe(32),
            user=user,
            created_at=now,
            expires_at=now + self._ttl,
            absolute_expires_at=now + self._absolute_ttl,
        )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                """
                INSERT INTO sessions (
                    id, subject, username, display_name, groups_json,
                    created_at_ts, expires_at_ts, absolute_expires_at_ts,
                    data_json, rights_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.user.subject,
                    session.user.username,
                    session.user.display_name,
                    json.dumps(list(session.user.groups)),
                    _to_ts(session.created_at),
                    _to_ts(session.expires_at),
                    _to_ts(session.absolute_expires_at),
                    "{}",
                    "{}",
                ),
            )
            rows = self._conn.execute(
                "SELECT id FROM sessions WHERE subject = ? ORDER BY created_at_ts ASC",
                (session.user.subject,),
            ).fetchall()
            excess = len(rows) - self._max_per_user
            if excess > 0:
                ids_to_delete = [r["id"] for r in rows[:excess]]
                self._conn.executemany(
                    "DELETE FROM sessions WHERE id = ?",
                    [(sid,) for sid in ids_to_delete],
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return session

    async def get_and_refresh(self, session_id: str) -> UserSession | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_and_refresh_sync, session_id)

    def _get_and_refresh_sync(self, session_id: str) -> UserSession | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        now = datetime.now(UTC)
        expires_at = _from_ts(row["expires_at_ts"])
        absolute_expires_at = _from_ts(row["absolute_expires_at_ts"])
        if expires_at <= now or absolute_expires_at <= now:
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return None
        new_expires = now + self._ttl
        self._conn.execute(
            "UPDATE sessions SET expires_at_ts = ? WHERE id = ?",
            (_to_ts(new_expires), session_id),
        )
        session = _row_to_session(row)
        return UserSession(
            id=session.id,
            user=session.user,
            created_at=session.created_at,
            expires_at=new_expires,
            absolute_expires_at=session.absolute_expires_at,
            data=session.data,
            rights=session.rights,
        )

    async def update_data(self, session_id: str, data: dict[str, Any]) -> None:
        data_json = json.dumps(data)
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "UPDATE sessions SET data_json = ? WHERE id = ?",
                (data_json, session_id),
            )

    async def set_rights(self, session_id: str, rights: Rights) -> None:
        """Persist the derived ``Rights`` view onto a session row.

        Called once per real login by the post-login hook chain after
        ``init_user_rights`` and ``derive_rights`` succeed. Stored as JSON
        alongside ``data_json`` on the same row.
        """
        rights_json = json.dumps(rights_to_dict(rights))
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "UPDATE sessions SET rights_json = ? WHERE id = ?",
                (rights_json, session_id),
            )

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "DELETE FROM sessions WHERE id = ?",
                (session_id,),
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._conn.close)
```

- [ ] **Step 5: Add SessionStore round-trip test**

Create or extend `tests/auth/test_session_store.py`:

```python
import asyncio

from iris.auth.identity import User
from iris.auth.session import Rights
from iris.auth.sessions import SessionStore


def test_set_rights_round_trips_via_get_and_refresh():
    async def go():
        store = SessionStore(
            path=":memory:",
            ttl_seconds=3600,
            absolute_ttl_seconds=86400,
        )
        try:
            user = User(
                subject="s1",
                username="alice",
                display_name="Alice",
                groups=("admins",),
            )
            session = await store.create(user)
            rights = Rights(
                is_admin=True,
                can_create_database=False,
                db_admin=frozenset({"finance"}),
                db_writer=frozenset(),
                db_reader=frozenset({"hr"}),
            )
            await store.set_rights(session.id, rights)
            refreshed = await store.get_and_refresh(session.id)
            assert refreshed is not None
            assert refreshed.rights == rights
        finally:
            await store.close()

    asyncio.run(go())
```

- [ ] **Step 6: Substitute `Session` → `AuthSession` in every source caller**

For each file from Step 1's grep list, change:

```python
from iris.auth.session import Session
```

to:

```python
from iris.auth.session import AuthSession
```

and rename every type usage `Session` → `AuthSession`. This includes `src/iris/auth/deps.py`, `src/iris/auth/routes.py`, `src/iris/clickhouse/deps.py`, `src/iris/clickhouse/handle.py` (if it references `Session`), and any tests. **Also drop every reference to `session.roles`** — those reads need to be replaced; for now leave them as `# TODO: rights` comments if you cannot determine the replacement immediately, but the build won't pass until Task 7-8 wire `rights` correctly.

Note: `tests/auth/test_rights.py`, `tests/auth/test_session_store.py`, and the new tests added in Tasks 1-5 already use the correct `AuthSession`/`Rights` names — no change needed there.

- [ ] **Step 7: Defer commit**

Do not commit yet. The substitutions in Step 6 leave dangling `roles` references that fail typing and unit tests. The next task wires the new dep aliases that consume `rights`. Continue to Task 7.

---

## Task 7: Replace `iris/auth/deps.py` with the alias suite

**Files:**
- Modify: `src/iris/auth/deps.py`
- Test: `tests/auth/test_deps.py`

- [ ] **Step 1: Write the failing test**

Create `tests/auth/test_deps.py`:

```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iris.auth.deps import (
    Session,
    SessionAdmin,
    SessionDatabaseAdmin,
    SessionDatabaseCreator,
    SessionOptional,
    SessionRead,
    SessionWrite,
    set_session_store,
    set_settings,
)
from iris.auth.identity import User
from iris.auth.session import Rights
from iris.auth.sessions import SessionStore


def _build_app(rights: Rights):
    """Return a FastAPI app with one canned session preinstalled and routes
    that exercise each alias dep. Returns (app, sid)."""
    app = FastAPI()
    store = SessionStore(
        path=":memory:", ttl_seconds=3600, absolute_ttl_seconds=86400
    )
    set_session_store(app, store)
    set_settings(app, cookie_name="iris_session", cookie_secure=False)

    import asyncio

    async def setup():
        user = User(
            subject="s1", username="alice", display_name="A", groups=("admins",)
        )
        s = await store.create(user)
        await store.set_rights(s.id, rights)
        return s.id

    sid = asyncio.run(setup())

    @app.get("/me")
    def me(session: Session) -> dict:
        return {"username": session.user.username}

    @app.get("/maybe")
    def maybe(session: SessionOptional) -> dict:
        return {"present": session is not None}

    @app.get("/admin")
    def admin(session: SessionAdmin) -> dict:
        return {"ok": True}

    @app.get("/creator")
    def creator(session: SessionDatabaseCreator) -> dict:
        return {"ok": True}

    @app.get("/db/{database}/admin")
    def db_admin(database: str, session: SessionDatabaseAdmin) -> dict:
        return {"db": database}

    @app.get("/db/{database}/write")
    def db_write(database: str, session: SessionWrite) -> dict:
        return {"db": database}

    @app.get("/db/{database}/read")
    def db_read(database: str, session: SessionRead) -> dict:
        return {"db": database}

    return app, sid


def _client(app, sid):
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    return c


def test_session_alias_admits_logged_in_user():
    app, sid = _build_app(Rights(False, False, frozenset(), frozenset(), frozenset()))
    r = _client(app, sid).get("/me")
    assert r.status_code == 200


def test_session_alias_rejects_anonymous():
    app, _ = _build_app(Rights(False, False, frozenset(), frozenset(), frozenset()))
    c = TestClient(app)
    r = c.get("/me", headers={"accept": "application/json"})
    assert r.status_code == 401


def test_session_optional_returns_none_anonymous():
    app, _ = _build_app(Rights(False, False, frozenset(), frozenset(), frozenset()))
    c = TestClient(app)
    r = c.get("/maybe")
    assert r.status_code == 200
    assert r.json() == {"present": False}


def test_session_admin_admits_when_is_admin():
    app, sid = _build_app(Rights(True, False, frozenset(), frozenset(), frozenset()))
    assert _client(app, sid).get("/admin").status_code == 200


def test_session_admin_rejects_when_not_admin():
    app, sid = _build_app(Rights(False, False, frozenset(), frozenset(), frozenset()))
    assert _client(app, sid).get("/admin").status_code == 403


def test_session_database_creator_admits_creator_or_admin():
    app1, sid1 = _build_app(
        Rights(False, True, frozenset(), frozenset(), frozenset())
    )
    assert _client(app1, sid1).get("/creator").status_code == 200
    app2, sid2 = _build_app(
        Rights(True, False, frozenset(), frozenset(), frozenset())
    )
    assert _client(app2, sid2).get("/creator").status_code == 200


def test_session_read_implied_by_higher_tiers():
    rights_admin = Rights(False, False, frozenset({"finance"}), frozenset(), frozenset())
    app, sid = _build_app(rights_admin)
    assert _client(app, sid).get("/db/finance/read").status_code == 200
    assert _client(app, sid).get("/db/finance/write").status_code == 200
    assert _client(app, sid).get("/db/finance/admin").status_code == 200
    # but a different db is rejected
    assert _client(app, sid).get("/db/other/read").status_code == 403


def test_session_write_does_not_imply_admin():
    rights_writer = Rights(False, False, frozenset(), frozenset({"finance"}), frozenset())
    app, sid = _build_app(rights_writer)
    assert _client(app, sid).get("/db/finance/read").status_code == 200
    assert _client(app, sid).get("/db/finance/write").status_code == 200
    assert _client(app, sid).get("/db/finance/admin").status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/auth/test_deps.py -v`
Expected: FAIL — alias deps cannot be imported.

- [ ] **Step 3: Replace `src/iris/auth/deps.py`**

```python
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Request

from iris.auth.exceptions import AuthForbidden, AuthRequired
from iris.auth.identity import UserSession
from iris.auth.session import AuthSession
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


async def _resolve_stored(request: Request) -> UserSession | None:
    cookie_name = _get_cookie_name(request)
    sid = request.cookies.get(cookie_name)
    if not sid:
        return None
    store = _get_store(request)
    return await store.get_and_refresh(sid)


_StoredSession = Annotated[UserSession | None, Depends(_resolve_stored)]


def _to_view(stored: UserSession) -> AuthSession:
    return AuthSession(
        id=stored.id,
        user=stored.user,
        created_at=stored.created_at,
        expires_at=stored.expires_at,
        data=stored.data,
        rights=stored.rights,
    )


async def _optional_session(stored: _StoredSession) -> AuthSession | None:
    if stored is None:
        return None
    return _to_view(stored)


async def _require_session(stored: _StoredSession) -> AuthSession:
    if stored is None:
        raise AuthRequired()
    return _to_view(stored)


_RequiredAuth = Annotated[AuthSession, Depends(_require_session)]


async def _require_admin(session: _RequiredAuth) -> AuthSession:
    if not session.rights.is_admin:
        raise AuthForbidden(needed=("admin",), have=())
    return session


async def _require_database_creator(session: _RequiredAuth) -> AuthSession:
    r = session.rights
    if not (r.is_admin or r.can_create_database):
        raise AuthForbidden(needed=("admin", "database_creator"), have=())
    return session


async def _require_database_admin(
    database: str, session: _RequiredAuth
) -> AuthSession:
    if not session.rights.has_admin(database):
        raise AuthForbidden(
            needed=(f"database_admin[{database}]",), have=()
        )
    return session


async def _require_write(database: str, session: _RequiredAuth) -> AuthSession:
    if not session.rights.has_write(database):
        raise AuthForbidden(
            needed=(f"database_writer[{database}]",), have=()
        )
    return session


async def _require_read(database: str, session: _RequiredAuth) -> AuthSession:
    if not session.rights.has_read(database):
        raise AuthForbidden(
            needed=(f"database_reader[{database}]",), have=()
        )
    return session


# Public Annotated aliases — what routes consume.
Session = Annotated[AuthSession, Depends(_require_session)]
SessionOptional = Annotated["AuthSession | None", Depends(_optional_session)]
SessionAdmin = Annotated[AuthSession, Depends(_require_admin)]
SessionDatabaseCreator = Annotated[AuthSession, Depends(_require_database_creator)]
SessionDatabaseAdmin = Annotated[AuthSession, Depends(_require_database_admin)]
SessionWrite = Annotated[AuthSession, Depends(_require_write)]
SessionRead = Annotated[AuthSession, Depends(_require_read)]
```

- [ ] **Step 4: Run targeted test**

Run: `uv run pytest tests/auth/test_deps.py -v`
Expected: PASS (8 tests).

If `SessionOptional` errors with a `ForwardRef` issue, replace `"AuthSession | None"` with the explicit `AuthSession | None` (no quotes); the `from __future__ import annotations` already stringifies type expressions.

- [ ] **Step 5: Defer commit**

Continue to Task 8 — routes.py still needs to populate `rights` and `/api/whoami` still references `session.roles`.

---

## Task 8: Wire rights into login and whoami

**Files:**
- Modify: `src/iris/auth/routes.py`
- Modify: `src/iris/clickhouse/install.py` (extend post-login hook to derive rights)

- [ ] **Step 1: Update `src/iris/clickhouse/install.py`'s post-login hook**

Replace `_provision_on_login` to derive rights and persist them. Look up the session store from `app.state.auth_session_store`. The hook now needs the session ID, but the existing signature is `async def _provision_on_login(user: User)` — extend the hook contract to `(user, session_id)`.

In `iris/clickhouse/install.py`:

```python
from iris.auth.session import Rights
from iris.clickhouse.rights import derive_rights

# inside install():

async def _provision_on_login(user: User, session_id: str) -> None:
    await asyncio.to_thread(
        init_user_rights,
        client,
        username=user.username,
        groups=list(user.groups),
        settings=settings,
    )
    rights = await asyncio.to_thread(
        derive_rights,
        client,
        username=user.username,
        groups=list(user.groups),
    )
    store: SessionStore = app.state.auth_session_store
    await store.set_rights(session_id, rights)
    logger.info(
        "clickhouse: provisioned user=%s groups=%s rights=admin:%s creator:%s",
        user.username, list(user.groups), rights.is_admin, rights.can_create_database,
    )
```

Add `from iris.auth.sessions import SessionStore` to the imports. Drop the `DatabaseAdminStore` block (deletion happens in Task 14, so for now leave it — the bridge handles will switch off `db_admin_store` in Task 9-10).

- [ ] **Step 2: Update `src/iris/auth/routes.py`**

Update `_finalize_login_redirect` to pass the session id to hooks:

```python
async def _finalize_login_redirect(
    *, user: User, target: str, method: str
) -> RedirectResponse:
    session = await store.create(user)
    for hook in app.state.post_login_hooks:
        await hook(user, session.id)
    ...
```

Update `/api/whoami` to expose rights:

```python
@router.get("/api/whoami")
async def whoami(session: Session) -> dict[str, Any]:
    r = session.rights
    return {
        "subject": session.user.subject,
        "display_name": session.user.display_name,
        "groups": list(session.user.groups),
        "rights": {
            "is_admin": r.is_admin,
            "can_create_database": r.can_create_database,
            "db_admin": sorted(r.db_admin),
            "db_writer": sorted(r.db_writer),
            "db_reader": sorted(r.db_reader),
        },
    }
```

Replace the `Session = Depends(require_session)` parameter shape with the alias `Session` everywhere in `routes.py` (also the `/logout` endpoint). Drop the `from iris.auth.deps import require_session` import; add `from iris.auth.deps import Session`. Also remove the `from iris.auth.authz.store import RoleMappingStore` import and the `authz_store` block in `install()` — those die in Task 13.

- [ ] **Step 3: Defer commit**

Continue to Task 9 — `iris/clickhouse/deps.py` still references `mapping`, `session.roles`, and `db_admin_store`.

---

## Task 9: Refactor `ClickHouseDatabaseCreatorHandle.create_database`

**Files:**
- Modify: `src/iris/clickhouse/handle.py`
- Test: `tests/clickhouse/test_creator_handle.py` (replaces tests against the old DatabaseAdminStore-based behavior)

- [ ] **Step 1: Write the failing test**

Create `tests/clickhouse/test_creator_handle.py`:

```python
import pytest
from iris.clickhouse.handle import ClickHouseDatabaseCreatorHandle
from iris.clickhouse.rights import derive_rights


@pytest.mark.asyncio
async def test_create_database_creates_db_and_tier_roles(
    ch_client, ch_settings, prefix
):
    user = f"{prefix}_creator"
    db = f"{prefix}_owned"
    handle = ClickHouseDatabaseCreatorHandle(
        client=ch_client, settings=ch_settings, username=user
    )
    await handle.create_database(db)

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
        "SELECT granted_role_name FROM system.role_grants WHERE role_name = {r:String}",
        parameters={"r": user_role},
    ).result_rows
    assert any(g[0] == f"{db}_DBADMIN" for g in granted)


@pytest.mark.asyncio
async def test_create_database_idempotent(ch_client, ch_settings, prefix):
    user = f"{prefix}_creator2"
    db = f"{prefix}_idemp"
    handle = ClickHouseDatabaseCreatorHandle(
        client=ch_client, settings=ch_settings, username=user
    )
    await handle.create_database(db)
    await handle.create_database(db)  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/clickhouse/test_creator_handle.py -v`
Expected: FAIL — current `ClickHouseDatabaseCreatorHandle` constructor still requires `db_admin_store=`.

- [ ] **Step 3: Update `src/iris/clickhouse/handle.py`'s creator class**

Replace the `ClickHouseDatabaseCreatorHandle` class:

```python
from iris.clickhouse.grants import (
    TIER_DBADMIN,
    create_tier_roles,
    grant_tier_to_user,
)


class ClickHouseDatabaseCreatorHandle:
    """Handle for users with ``can_create_database`` (or ``is_admin``).

    ``create_database`` creates the database, the three tier roles, the
    privilege grants, and grants ``DBADMIN`` to the creator's per-user role.
    All steps are ``IF NOT EXISTS`` and idempotent.
    """

    def __init__(
        self,
        *,
        client: Client,
        settings: ClickHouseSettings,
        username: str,
    ) -> None:
        self._client = client
        self._settings = settings
        self._username = username

    async def create_database(self, name: str) -> None:
        validate_identifier(name, kind="database")
        quoted = quote_identifier(name, kind="database")
        await asyncio.to_thread(
            self._client.command, f"CREATE DATABASE IF NOT EXISTS {quoted}"
        )
        await asyncio.to_thread(create_tier_roles, self._client, database=name)
        await asyncio.to_thread(
            grant_tier_to_user,
            self._client,
            database=name,
            tier=TIER_DBADMIN,
            username=self._username,
        )
```

Remove the `from iris.clickhouse.database_admins import DatabaseAdminStore` import and the `db_admin_store` parameter.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/clickhouse/test_creator_handle.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Defer commit**

Continue to Task 10 — admin handle still references `db_admin_store` and `authz_store`.

---

## Task 10: Refactor `ClickHouseDatabaseAdminHandle` to tier-role grants + add `delete_database`

**Files:**
- Modify: `src/iris/clickhouse/handle.py`
- Test: `tests/clickhouse/test_admin_handle.py`

- [ ] **Step 1: Write the failing test**

Create `tests/clickhouse/test_admin_handle.py`:

```python
import pytest
from iris.clickhouse.handle import (
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
)
from iris.clickhouse.rights import derive_rights


@pytest.mark.asyncio
async def test_grant_reader_writer_admin_propagate_to_rights(
    ch_client, ch_settings, ch_http_client, prefix
):
    creator = f"{prefix}_creator"
    target = f"{prefix}_target"
    db = f"{prefix}_admin_grants"
    creator_h = ClickHouseDatabaseCreatorHandle(
        client=ch_client, settings=ch_settings, username=creator
    )
    await creator_h.create_database(db)
    admin_h = ClickHouseDatabaseAdminHandle(
        client=ch_client,
        http_client=ch_http_client,
        settings=ch_settings,
        database=db,
        username=creator,
    )

    await admin_h.grant_reader(target)
    r = derive_rights(ch_client, username=target, groups=[])
    assert db in r.db_reader

    await admin_h.grant_writer(target)
    r = derive_rights(ch_client, username=target, groups=[])
    assert db in r.db_writer

    await admin_h.add_admin_user(target)
    r = derive_rights(ch_client, username=target, groups=[])
    assert db in r.db_admin


@pytest.mark.asyncio
async def test_revoke_clears_label(ch_client, ch_settings, ch_http_client, prefix):
    creator = f"{prefix}_c"
    target = f"{prefix}_t"
    db = f"{prefix}_revoke_admin"
    creator_h = ClickHouseDatabaseCreatorHandle(
        client=ch_client, settings=ch_settings, username=creator
    )
    await creator_h.create_database(db)
    admin_h = ClickHouseDatabaseAdminHandle(
        client=ch_client,
        http_client=ch_http_client,
        settings=ch_settings,
        database=db,
        username=creator,
    )
    await admin_h.grant_reader(target)
    await admin_h.revoke_reader(target)
    r = derive_rights(ch_client, username=target, groups=[])
    assert db not in r.db_reader


@pytest.mark.asyncio
async def test_delete_database_drops_tier_roles_and_db(
    ch_client, ch_settings, ch_http_client, prefix
):
    creator = f"{prefix}_c"
    db = f"{prefix}_to_drop"
    creator_h = ClickHouseDatabaseCreatorHandle(
        client=ch_client, settings=ch_settings, username=creator
    )
    await creator_h.create_database(db)
    admin_h = ClickHouseDatabaseAdminHandle(
        client=ch_client,
        http_client=ch_http_client,
        settings=ch_settings,
        database=db,
        username=creator,
    )
    await admin_h.delete_database()

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
```

The test expects an `ch_http_client` fixture for the admin handle's `http_client` arg. If absent, mark each test with `pytest.skip` and revisit, or add a synthesized minimal `httpx.AsyncClient` fixture in `tests/clickhouse/conftest.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/clickhouse/test_admin_handle.py -v`
Expected: FAIL — `grant_reader`, `delete_database`, etc. do not exist on `ClickHouseDatabaseAdminHandle`; constructor still wants `db_admin_store`/`authz_store`.

- [ ] **Step 3: Replace `ClickHouseDatabaseAdminHandle`**

Replace the entire class in `src/iris/clickhouse/handle.py`:

```python
from iris.clickhouse.grants import (
    TIER_DBADMIN,
    TIER_DBREADER,
    TIER_DBWRITER,
    drop_tier_roles,
    grant_tier_to_group,
    grant_tier_to_user,
    revoke_tier_from_group,
    revoke_tier_from_user,
    tier_role_name,
)


class ClickHouseDatabaseAdminHandle:
    """Per-database admin handle.

    All grant/revoke operations are tier-role grants on per-user/per-group
    roles in CH. Reading "who is admin of database X" is querying CH for
    members of ``X_DBADMIN``. Adding an admin is granting ``X_DBADMIN`` to
    the target's ``<username>_USER`` role (pre-creating it if absent).
    """

    def __init__(
        self,
        *,
        client: Client,
        http_client: httpx.AsyncClient,
        settings: ClickHouseSettings,
        database: str,
        username: str,
    ) -> None:
        self._client = client
        self._http_client = http_client
        self._settings = settings
        self._database = database
        self._username = username

    # ---- tier grants ----

    async def grant_reader(self, username: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_user,
            self._client,
            database=self._database,
            tier=TIER_DBREADER,
            username=username,
        )

    async def grant_writer(self, username: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_user,
            self._client,
            database=self._database,
            tier=TIER_DBWRITER,
            username=username,
        )

    async def add_admin_user(self, username: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_user,
            self._client,
            database=self._database,
            tier=TIER_DBADMIN,
            username=username,
        )

    async def revoke_reader(self, username: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_user,
            self._client,
            database=self._database,
            tier=TIER_DBREADER,
            username=username,
        )

    async def revoke_writer(self, username: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_user,
            self._client,
            database=self._database,
            tier=TIER_DBWRITER,
            username=username,
        )

    async def remove_admin_user(self, username: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_user,
            self._client,
            database=self._database,
            tier=TIER_DBADMIN,
            username=username,
        )

    # ---- group equivalents ----

    async def grant_reader_to_group(self, group: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_group,
            self._client,
            database=self._database,
            tier=TIER_DBREADER,
            group=group,
        )

    async def grant_writer_to_group(self, group: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_group,
            self._client,
            database=self._database,
            tier=TIER_DBWRITER,
            group=group,
        )

    async def add_admin_group(self, group: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_group,
            self._client,
            database=self._database,
            tier=TIER_DBADMIN,
            group=group,
        )

    async def revoke_reader_from_group(self, group: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_group,
            self._client,
            database=self._database,
            tier=TIER_DBREADER,
            group=group,
        )

    async def revoke_writer_from_group(self, group: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_group,
            self._client,
            database=self._database,
            tier=TIER_DBWRITER,
            group=group,
        )

    async def remove_admin_group(self, group: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_group,
            self._client,
            database=self._database,
            tier=TIER_DBADMIN,
            group=group,
        )

    # ---- database lifecycle ----

    async def delete_database(self) -> None:
        """``DROP DATABASE IF EXISTS`` then drop the three tier roles. Idempotent.

        Order matters: drop the database before the roles so a partial failure
        leaves the data dropped (the goal) rather than orphan grants.
        """
        db_q = quote_identifier(self._database, kind="database")
        await asyncio.to_thread(
            self._client.command, f"DROP DATABASE IF EXISTS {db_q}"
        )
        await asyncio.to_thread(
            drop_tier_roles, self._client, database=self._database
        )

    # ---- listing ----

    async def list_admin_members(self) -> list[str]:
        """Members of ``<database>_DBADMIN`` — both user and group roles."""
        admin_role = tier_role_name(self._database, TIER_DBADMIN)
        rows = await asyncio.to_thread(
            self._client.query,
            "SELECT role_name FROM system.role_grants WHERE granted_role_name = {r:String}",
            {"r": admin_role},
        )
        return [cast_str(row["role_name"]) for row in rows.named_results()]

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

Add `from typing import cast as cast_str` if needed (or inline `cast(str, row["role_name"])`).

Drop the row-policy methods on the admin handle entirely — operators attach row policies via the admin handle on `ClickHouseAdminHandle.add_row_policy` against tier-role names (no per-DB-admin convenience method). If the existing tests reference `add_row_policy_for_user` on this handle, they need updating in Task 21.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/clickhouse/test_admin_handle.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Defer commit**

Continue to Task 11 — `iris/clickhouse/deps.py` still constructs handles with the old signatures.

---

## Task 11: Replace `iris/clickhouse/deps.py` with rights-based gates

**Files:**
- Modify: `src/iris/clickhouse/deps.py`

- [ ] **Step 1: Replace the file**

```python
"""FastAPI dependencies that bridge iris.auth into iris.clickhouse.

Each dep checks the session's ``rights`` (already derived at login by the
post-login hook in ``iris.clickhouse.install``). No SQLite mapping or per-DB
admin store is consulted; ClickHouse is the source of truth.
"""
from __future__ import annotations

from fastapi import Depends, Request

from iris.auth.deps import Session, SessionDatabaseAdmin, SessionDatabaseCreator
from iris.auth.exceptions import AuthForbidden
from iris.auth.session import AuthSession
from iris.clickhouse.handle import (
    ClickHouseAdminHandle,
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
    ClickHouseHandle,
)
from iris.clickhouse.identifiers import validate_identifier


async def get_clickhouse_handle(
    request: Request, session: Session
) -> ClickHouseHandle:
    return ClickHouseHandle(
        client=request.app.state.clickhouse_client,
        http_client=request.app.state.clickhouse_http_client,
        username=session.user.username,
    )


async def require_clickhouse_admin(
    request: Request, session: Session
) -> ClickHouseAdminHandle:
    if not session.rights.is_admin:
        raise AuthForbidden(needed=("admin",), have=())
    return ClickHouseAdminHandle(
        client=request.app.state.clickhouse_client,
        http_client=request.app.state.clickhouse_http_client,
        username=session.user.username,
        settings=request.app.state.clickhouse_settings,
    )


async def require_clickhouse_database_creator(
    request: Request, session: SessionDatabaseCreator
) -> ClickHouseDatabaseCreatorHandle:
    return ClickHouseDatabaseCreatorHandle(
        client=request.app.state.clickhouse_client,
        settings=request.app.state.clickhouse_settings,
        username=session.user.username,
    )


async def require_clickhouse_database_admin(
    request: Request, database: str, session: SessionDatabaseAdmin
) -> ClickHouseDatabaseAdminHandle:
    validate_identifier(database, kind="database")
    return ClickHouseDatabaseAdminHandle(
        client=request.app.state.clickhouse_client,
        http_client=request.app.state.clickhouse_http_client,
        settings=request.app.state.clickhouse_settings,
        database=database,
        username=session.user.username,
    )
```

The handle providers reuse the alias deps for tier checks; FastAPI threads the `database` path parameter through the alias dep automatically because both the alias dep and the handle provider declare `database: str` as a parameter. The handle provider does not need to repeat the rights check.

- [ ] **Step 2: Run the full test suite**

Run: `uv run pytest -x -vv`
Expected: many failures still — `tests/auth/authz/`, `tests/clickhouse/test_database_admin*`, `iris.auth.authz.*` imports in the codebase. Continue.

- [ ] **Step 3: Type-check**

Run: `uv run basedpyright --level error`
Expected: failures in `iris/auth/authz/*`, `iris/clickhouse/database_admins.py`, `iris/clickhouse/install.py` (still references `DatabaseAdminStore`), and `iris/clickhouse/handle.py` (if `iris.auth.authz` import lingers). Address by continuing to Task 12.

- [ ] **Step 4: Defer commit**

Hold the commit until Task 13 finishes the deletion. The breakage window closes at end of Task 13.

---

## Task 12: Wire `bootstrap_admin` + `IRIS_BOOTSTRAP_USER`

**Files:**
- Modify: `src/iris/auth/config.py`
- Modify: `src/iris/clickhouse/install.py`

- [ ] **Step 1: Update `AuthSettings.from_env`**

In `src/iris/auth/config.py`:

- Drop the `bootstrap_role` field and its `os.environ.get("AUTHZ_BOOTSTRAP_ROLE", ...)` line.
- Replace `bootstrap_user` env-var read with `IRIS_BOOTSTRAP_USER`:

```python
bootstrap_user = (
    os.environ.get("IRIS_BOOTSTRAP_USER", "").strip() or None
)
```

- Drop `bootstrap_role` from the dataclass and from `cls(...)`.

- [ ] **Step 2: Update `iris/clickhouse/install.py`**

After `ensure_service_admin(client, settings)`, add:

```python
from iris.auth.bootstrap import bootstrap_admin
from iris.auth.config import AuthSettings

auth_settings = AuthSettings.from_env()
if auth_settings.bootstrap_user:
    await asyncio.to_thread(
        bootstrap_admin, client, username=auth_settings.bootstrap_user
    )
```

Wrap the call to be sync-compatible with the install pathway (which is currently sync). `install` is called during app construction; if there's no event loop, switch to a sync call: `bootstrap_admin(client, username=auth_settings.bootstrap_user)`.

Verify by running: `git grep 'def install(' src/iris/clickhouse/install.py` — confirm it's sync. If sync, drop the `asyncio.to_thread` wrapper.

- [ ] **Step 3: Run the full test suite**

Still expected to fail (Tasks 13-14 still pending). Continue.

- [ ] **Step 4: Defer commit**

---

## Task 13: Delete `iris/auth/authz/` and its tests

**Files:**
- Delete: `src/iris/auth/authz/` (entire directory)
- Delete: `tests/auth/authz/` (entire directory)
- Modify: every file that imports `iris.auth.authz` (final cleanup)

- [ ] **Step 1: Delete the source subpackage**

```bash
git rm -r src/iris/auth/authz/
```

- [ ] **Step 2: Delete the test subpackage**

```bash
git rm -r tests/auth/authz/
```

- [ ] **Step 3: Remove dangling imports**

```bash
git grep -n 'iris\.auth\.authz' src/ tests/
```

For each match, delete the import. Common culprits (verify against the grep output): `src/iris/auth/__init__.py`, `src/iris/auth/routes.py`, `src/iris/clickhouse/handle.py`, `src/iris/clickhouse/install.py`, `src/iris/clickhouse/deps.py`, `tests/conftest.py`. After all imports are removed, `git grep iris\.auth\.authz` should be empty.

- [ ] **Step 4: Update `tests/conftest.py`**

Drop any `bootstrap_role`/`bootstrap_user` defaults that referenced the old `AUTHZ_*` env vars. Replace with `os.environ.setdefault("IRIS_BOOTSTRAP_USER", "")` (empty default — bootstrap is opt-in). Drop any pytest fixtures that constructed a `RoleMappingStore` or seeded the YAML mapping.

- [ ] **Step 5: Update `src/iris/auth/__init__.py`**

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
from iris.auth.identity import User
from iris.auth.routes import install
from iris.auth.session import AuthSession, EMPTY_RIGHTS, Rights

__all__ = [
    "AuthSession",
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

- [ ] **Step 6: Update `src/iris/auth/exceptions.py`**

Drop the `AuthorizationMisconfigured` class and its handler in `install_exception_handlers`. Diff:

```python
# DELETE:
class AuthorizationMisconfigured(RuntimeError):
    ...

# DELETE inside install_exception_handlers:
@app.exception_handler(AuthorizationMisconfigured)
async def _on_authorization_misconfigured(...):
    ...
```

- [ ] **Step 7: Run typecheck**

Run: `uv run basedpyright --level error`
Expected: clean unless `iris/clickhouse/database_admins.py` still exists. That file dies in Task 14.

If errors mention unresolved `iris.auth.authz`, find the importer and delete the import.

---

## Task 14: Delete `iris/clickhouse/database_admins.py` + its tests

**Files:**
- Delete: `src/iris/clickhouse/database_admins.py`
- Delete: any `tests/clickhouse/test_database_admin*.py` (replaced by tier-role tests already added)
- Modify: `src/iris/clickhouse/install.py` (drop `DatabaseAdminStore`)
- Modify: `src/iris/clickhouse/__init__.py` (drop the export)

- [ ] **Step 1: Delete the file**

```bash
git rm src/iris/clickhouse/database_admins.py
```

- [ ] **Step 2: Delete the obsolete tests**

Identify with `git ls-files tests/clickhouse | grep -i database_admin`. Delete every file that tested the SQLite admin store. The new `test_creator_handle.py`, `test_admin_handle.py`, `test_tier_roles.py`, and `test_tier_grants.py` cover the replacement behavior.

- [ ] **Step 3: Update `src/iris/clickhouse/install.py`**

Remove all `DatabaseAdminStore`-related lines:

```python
# DELETE:
from iris.clickhouse.database_admins import DatabaseAdminStore
# ...
auth_db_path = app.state.auth_db_path
db_admin_store = DatabaseAdminStore(path=auth_db_path)
db_admin_store.bootstrap()
app.state.clickhouse_database_admins = db_admin_store
app.state.clickhouse_close_database_admins = db_admin_store.close
```

- [ ] **Step 4: Update `src/iris/clickhouse/__init__.py`**

Drop these from imports and `__all__`:

- `DatabaseAdminStore`
- `CLICKHOUSE_ADMIN_ROLE`
- `CLICKHOUSE_DATABASE_CREATOR_ROLE`

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest -x -vv`
Expected: most tests pass. Failures will likely concentrate in `tests/auth/integration/` (asserts on `session.roles`) and `tests/conftest.py` fixtures. Address in subsequent tasks.

- [ ] **Step 6: Run typecheck**

Run: `uv run basedpyright --level error`
Expected: clean.

- [ ] **Step 7: Commit the breakage-window changes as one large commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(authz): collapse to ClickHouse-only authorization

- AuthSession (renamed from Session) carries derived Rights
- Per-database tier roles in CH (X_DBADMIN/_DBWRITER/_DBREADER)
- Annotated dep aliases: Session, SessionOptional, SessionRead,
  SessionWrite, SessionDatabaseAdmin, SessionDatabaseCreator, SessionAdmin
- bootstrap_admin seeds first admin via IRIS_BOOTSTRAP_USER
- Drop iris.auth.authz subpackage and clickhouse.database_admins module
EOF
)"
```

---

## Task 15: Update `tests/auth/integration/` for the new session shape

**Files:**
- Modify: every test under `tests/auth/integration/` that asserts `session.roles`, references `RoleMappingStore`, or constructs sessions directly.

- [ ] **Step 1: Find call sites**

Run: `git grep -n 'session\.roles\|RoleMappingStore\|require_role\|AuthorizationMisconfigured' tests/`

- [ ] **Step 2: Replace `session.roles` reads**

Templates and assertions that previously read `session.roles` should now read `session.rights`. For tests verifying "alice is admin", change:

```python
assert "admin" in session.roles
```

to:

```python
assert session.rights.is_admin
```

For "alice has access to finance", change:

```python
assert "finance_reader" in session.roles
```

to:

```python
assert session.rights.has_read("finance")
```

- [ ] **Step 3: Replace `require_role` route gates with the new aliases**

Routes (in tests, fixtures, or the demo app) that used `Depends(require_role("admin"))` now use `Annotated[AuthSession, Depends(_require_admin)]` via the alias `SessionAdmin`. Search for `require_role(` and replace each call site.

- [ ] **Step 4: Run the integration suite**

Run: `uv run pytest tests/auth/integration -v`
Expected: PASS. If a test tried to verify the old "missing role → 500" pathway, delete it (the failure mode no longer exists).

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test(auth/integration): assert via session.rights instead of roles"
```

---

## Task 16: End-to-end tier-promotion test

**Files:**
- Create: `tests/clickhouse/test_tier_promotion.py`

- [ ] **Step 1: Write the test**

```python
"""End-to-end: creator creates DB → grants writer to bob → bob's session
shows db_writer → bob's writes succeed via query_as_user → bob's attempt
to delegate is rejected because his rights don't include is_admin."""
import pytest

from iris.clickhouse.handle import (
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
    ClickHouseHandle,
)
from iris.clickhouse.rights import derive_rights
from iris.clickhouse.users import init_user_rights


@pytest.mark.asyncio
async def test_creator_grants_writer_promotes_target(
    ch_client, ch_settings, ch_http_client, prefix
):
    creator = f"{prefix}_creator"
    bob = f"{prefix}_bob"
    db = f"{prefix}_promo"

    creator_h = ClickHouseDatabaseCreatorHandle(
        client=ch_client, settings=ch_settings, username=creator
    )
    await creator_h.create_database(db)

    init_user_rights(ch_client, username=bob, groups=[], settings=ch_settings)
    bob_rights_before = derive_rights(ch_client, username=bob, groups=[])
    assert db not in bob_rights_before.db_writer

    admin_h = ClickHouseDatabaseAdminHandle(
        client=ch_client,
        http_client=ch_http_client,
        settings=ch_settings,
        database=db,
        username=creator,
    )
    await admin_h.grant_writer(bob)

    bob_rights_after = derive_rights(ch_client, username=bob, groups=[])
    assert db in bob_rights_after.db_writer
    assert db not in bob_rights_after.db_admin

    # bob can run a SELECT impersonated; iris doesn't gate query_as_user beyond auth
    bob_handle = ClickHouseHandle(
        client=ch_client, http_client=ch_http_client, username=bob
    )
    rows = await bob_handle.query_as_user(f"SELECT 1 AS x FROM `{db}`.`numbers(1)`")
    # CH has system.numbers but a DB-scoped read on numbers(1) verifies impersonation
    # works; if your CH version errors, adjust to a table you create in this test.
    assert rows  # at least one row returned
```

If `numbers(1)` does not work scoped to a user database in your CH build, replace with a test table created via `creator_h` and a `query_as_service` insert.

- [ ] **Step 2: Run**

Run: `uv run pytest tests/clickhouse/test_tier_promotion.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/clickhouse/test_tier_promotion.py
git commit -m "test(clickhouse): end-to-end tier promotion"
```

---

## Task 17: Update `tests/conftest.py` for new fixture shape

**Files:**
- Modify: `tests/conftest.py` (and `tests/auth/integration/conftest.py` if it references `RoleMappingStore`)

- [ ] **Step 1: Audit existing fixtures**

Run: `git grep -n 'role_mapping\|authz_store\|database_admin_store\|RoleMappingStore\|AUTHZ_BOOTSTRAP' tests/`

- [ ] **Step 2: Drop authz fixtures**

Delete fixtures that constructed `RoleMappingStore` or seeded the YAML mapping. The `authed_client` fixture should now construct an `AuthSession` with empty `Rights` (or a configurable `Rights`) by:

1. Creating a session via the `SessionStore` (existing behavior).
2. Calling `await store.set_rights(session.id, rights)` with the desired `Rights`.
3. Setting the cookie on the `TestClient`.

Add a parametrized variant if any test depends on a specific `Rights` shape:

```python
import pytest
from iris.auth.session import EMPTY_RIGHTS, Rights


@pytest.fixture
def make_authed_client(client, session_store):
    """Factory: returns a TestClient with a session pre-installed for the given user/rights."""
    import asyncio

    def _make(user, rights: Rights = EMPTY_RIGHTS):
        async def setup():
            s = await session_store.create(user)
            await session_store.set_rights(s.id, rights)
            return s.id

        sid = asyncio.run(setup())
        client.cookies.set("iris_session", sid)
        return client

    return _make
```

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -x -vv`
Expected: PASS. If anything in `tests/auth/` fails on a missing `RoleMappingStore` fixture, delete that test or migrate it.

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test(conftest): drop authz fixtures, factory for rights-aware authed_client"
```

---

## Task 18: Update `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Replace the `Authentication > Authorization (roles)` section**

Delete the entire "Authorization (roles)" subsection (the block describing `authz_*` tables, `RoleMappingStore`, `for_session(...)`, `add_role`/`remove_role`, the bootstrap env vars, etc.). Replace with a new "Authorization (CH-derived rights)" subsection that points readers to the spec at `docs/superpowers/specs/2026-05-08-clickhouse-only-authz-design.md` for the full model and gives a 5-bullet summary:

- Source of truth: ClickHouse RBAC.
- Derived once at login by `iris.clickhouse.rights.derive_rights`; cached on the session row.
- Per-database tier roles `<X>_DBADMIN/_DBWRITER/_DBREADER` are created at DB creation and dropped at deletion.
- Routes gate via the `Annotated` aliases in `iris.auth.deps`: `Session`, `SessionOptional`, `SessionRead`, `SessionWrite`, `SessionDatabaseAdmin`, `SessionDatabaseCreator`, `SessionAdmin`.
- Bootstrap: `IRIS_BOOTSTRAP_USER` env var seeds the first admin in CH on a fresh deploy.

- [ ] **Step 2: Replace the `ClickHouse > Per-database admin tier` section**

Replace with text matching the new model: "Per-database admin is a CH role membership (`<X>_DBADMIN`), not a SQLite table. Use `ClickHouseDatabaseAdminHandle.add_admin_user(username)` to delegate, `delete_database()` to drop a DB and its tier roles." Drop all references to `DatabaseAdminStore` and `clickhouse_database_admins_*`.

- [ ] **Step 3: Update env-var section**

Replace `AUTHZ_BOOTSTRAP_ROLE` and `AUTHZ_BOOTSTRAP_USER` with `IRIS_BOOTSTRAP_USER` only. Update the example `.env` block accordingly.

- [ ] **Step 4: Update module map sections**

In the auth and ClickHouse module-map blocks, drop the `authz/` subpackage and the `database_admins.py` line. Add `bootstrap.py` to the auth module list and `rights.py` to the clickhouse module list. Mark `deps.py` (auth) as exporting all the alias deps.

- [ ] **Step 5: Read the file, sanity-check sections still cross-reference**

Verify there are no leftover mentions of `require_role`, `RoleMappingStore`, `authz_store`, `clickhouse_database_admins`, `AUTHZ_BOOTSTRAP_*`, or `clickhouse_admin` (the role name).

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE): replace dual-layer authz with CH-only rights model"
```

---

## Task 19: Final verification — full suite + lint + typecheck

**Files:** none.

- [ ] **Step 1: Full test suite**

Run: `uv run pytest -vv`
Expected: PASS, including the integration tier (`tests/auth/integration/`).

- [ ] **Step 2: Type check (errors)**

Run: `uv run basedpyright --level error`
Expected: zero errors.

- [ ] **Step 3: Type check (warnings)**

Run: `uv run basedpyright --level warning`
Expected: zero warnings (project gate).

- [ ] **Step 4: Lint**

Run: `uv run ruff check`
Expected: only the documented `E402` in `src/iris/__init__.py`. Anything else is a regression — fix.

- [ ] **Step 5: Smoke-test the dev server**

Run in one terminal: `uv run iris`
In another: `curl -i http://127.0.0.1:8000/`. Expected: 302 to `/login` (no session) or a 200 from the home template (depending on `optional_session` rendering).

If the demo app under `iris/app.py` had any routes that referenced `session.roles` or `require_role`, they break here — fix in this task.

- [ ] **Step 6: Stop the dev server.**

- [ ] **Step 7: Final commit (only if any fixes were made)**

```bash
git add -A
git commit -m "chore: final cleanup after CH-only authz migration"
```

---

## Self-Review (do not delete this section after running)

- **Spec coverage check** — every spec section maps to at least one task:
  - "Why" (motivation) — covered by Task 18 (CLAUDE.md).
  - "Granularity: database-level only" — implicit; no table-level deps exist.
  - "Right semantics" table — Tasks 1, 4 (Rights + derive_rights cover all five rows).
  - "CH-side state: per-database tier roles" — Tasks 2, 3, 9, 10.
  - "Rights derivation" — Task 4.
  - "Database name parsing" — Task 4 (suffix-anchored split inside derive_rights).
  - "Session shape" (`AuthSession`, drop `roles`) — Task 6.
  - "Dep aliases" — Task 7.
  - "Route examples" — Tasks 7, 11 (deps are the gates, handles are the providers).
  - "Lifecycle: tier-role create/drop" — Tasks 9, 10.
  - "Username enumeration: pre-create on grant" — Task 3.
  - "Row policies" — surface unchanged (existing `add_row_policy` callable from `ClickHouseAdminHandle`); confirm during Task 17 that no test asserts policies attached to per-user roles, only to tier roles.
  - "Bootstrap (option β)" — Tasks 5, 12.
  - "Configuration" — Task 12 (env-var rename).
  - "Module map" — Tasks 13, 14, 18 (deletes + CLAUDE.md).
  - "Tests" — Tasks 15, 16, 17.
  - "Open risks" / "Migration / rollout" — operator-facing, captured in CLAUDE.md (Task 18).

- **Type/method consistency check** — the `ClickHouseDatabaseAdminHandle` exposes `grant_reader` / `grant_writer` / `add_admin_user` and `revoke_reader` / `revoke_writer` / `remove_admin_user`. The end-to-end test (Task 16) and admin-handle test (Task 10) both use these names. The creator handle exposes `create_database`. The single source of truth for tier role names is `tier_role_name(database, tier)` in `grants.py`, used by `derive_rights` (Task 4), `create_tier_roles` (Task 2), `grant_tier_to_user/group` (Task 3), and the admin handle's `list_admin_members` (Task 10). The `Rights` field names (`is_admin`, `can_create_database`, `db_admin`, `db_writer`, `db_reader`) are referenced by the dep resolvers (Task 7), the whoami route (Task 8), the test fixture (Task 17), and the admin-handle test (Task 10) — all consistent.

- **Placeholder scan** — no "TBD"/"TODO"/"implement later" left in code. Two prose-level deferrals exist:
  1. Task 4 Step 4 — diagnostic note for CH version compatibility on `system.grants` representation of `GRANT ALL`. Includes the diagnostic SQL to run; not a placeholder.
  2. Task 16 Step 1 — fallback if `numbers(1)` doesn't work on the CH version. Includes the fallback ("create a test table via `creator_h` and `query_as_service` insert"); not a placeholder.

- **Order check** — the breakage-window task ordering (6 → 11) means the codebase doesn't compile between Task 5's commit and Task 14's commit. The plan calls this out in the "Order of Operations" preamble and at Task 6 Step 7, Task 11 Step 4, and Task 14 Step 7. Engineers running this plan must finish 6-14 in one session before treating the work as resumable.
