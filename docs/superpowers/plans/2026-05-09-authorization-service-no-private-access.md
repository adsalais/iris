# Authorization service — remove private-session access — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move 5 SQL functions out of `iris.features.authorization.service` (which currently calls `session._ch()`) into `iris.clickhouse.{audit, grants}`; expose them as typed-session methods; rewrite `service.py` and the admin-console routes to consume the typed methods. Restore the invariant: zero `# pyright: ignore[reportPrivateUsage]` and zero `# noqa: SLF001` in `src/`.

**Architecture:** Three commits. Task 1 adds the 5 CH SQL helpers (sync, take `client` as first arg) with testcontainer-backed unit tests. Task 2 is the migration: 5 new session methods on `AdminSession`/`DatabaseAdminSession` + service.py rewrite + routes.py update + test monkeypatch fixes + deletion of the now-unused `list_admin_members`. Task 3 adds the CLAUDE.md convention and runs the final audit (greps + gates).

**Tech Stack:** Python 3.13, ClickHouse 26.3 testcontainer, FastAPI, pytest, basedpyright, ruff. No new runtime deps.

---

## File map

### New files

| Path | Responsibility |
|---|---|
| `tests/auth/test_admin_inventory_methods.py` | Mock-based tests for the 4 new `AdminSession.list_*` methods (verify they call the right `iris.clickhouse.audit` helper with the client from `_ch()`) |
| `tests/auth/test_database_admin_list_members.py` | Mock-based test for `DatabaseAdminSession.list_members` |

### Modified files

| Path | Change |
|---|---|
| `src/iris/clickhouse/grants.py` | Add `list_tier_members(client, *, database) -> dict[str, list[dict[str, str]]]` |
| `src/iris/clickhouse/audit.py` | Add `list_all_users`, `list_all_databases`, `list_all_row_policies`, `list_all_grants` — 4 sync helpers |
| `src/iris/auth/views.py` | DatabaseAdminSession: replace `list_admin_members()` with `list_members()`. AdminSession: add `list_users()`, `list_databases()`, `list_all_row_policies()`, `list_all_grants()` |
| `src/iris/features/authorization/service.py` | Delete `list_members`, `list_all_users`, `list_all_databases`, `list_all_row_policies`, `list_all_grants`. `manage_view` becomes a thin aggregator over typed session methods. `my_access_view` unchanged |
| `src/iris/features/authorization/routes.py` | 5 admin-console routes: `await list_all_users(admin)` → `await admin.list_users()` (and analogous edits). Drop the corresponding `from iris.features.authorization.service import list_all_*` lines |
| `tests/clickhouse/test_clickhouse_grants.py` | Add `test_list_tier_members_returns_three_tier_dict` (testcontainer-backed) |
| `tests/clickhouse/test_clickhouse_audit.py` | Add 4 testcontainer-backed tests, one per new helper |
| `tests/features/test_authorization_admin_console.py` | Update 4 monkeypatches: `iris.features.authorization.service.list_all_*` → `iris.auth.views.AdminSession.list_*` |
| `tests/features/test_authorization_audit.py` | Update monkeypatch for `service.list_members` → `iris.auth.views.DatabaseAdminSession.list_members` |
| `tests/features/test_authorization_manage.py` | No change expected (it monkeypatches `service.manage_view`, which survives) |
| `CLAUDE.md` | Add the "no private access across modules" convention bullet |

### Deleted (no file deletions; method/function-level deletions only)

| Symbol | Why |
|---|---|
| `DatabaseAdminSession.list_admin_members()` (in `views.py`) | Replaced by `list_members()` returning all three tiers |
| `service.list_members`, `service.list_all_users`, `service.list_all_databases`, `service.list_all_row_policies`, `service.list_all_grants` | All migrate to typed session methods |

---

## Task 1 — CH helpers + testcontainer tests

**Files:**
- Modify: `src/iris/clickhouse/grants.py`
- Modify: `src/iris/clickhouse/audit.py`
- Modify: `tests/clickhouse/test_clickhouse_grants.py`
- Modify: `tests/clickhouse/test_clickhouse_audit.py`

- [ ] **Step 1: Snapshot baseline + verify the 5 violations exist (pre-condition)**

```bash
grep -n "reportPrivateUsage" src/iris/features/authorization/service.py
```
Expected: 5 matches (the violations the refactor exists to remove).

```bash
uv run pytest tests/clickhouse/test_clickhouse_grants.py tests/clickhouse/test_clickhouse_audit.py -q
```
Expected: existing tests pass.

- [ ] **Step 2: Write the failing tests for the new CH helpers**

Append to `tests/clickhouse/test_clickhouse_grants.py`:

```python
from iris.clickhouse.grants import list_tier_members


def test_list_tier_members_returns_three_tier_dict(ch_client, ch_settings, prefix):
    """Tier-role membership grouped by tier, returning {admin, reader, writer}."""
    from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
    db = f"{prefix}_listmem"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    # Bootstrap the three tier roles for this database.
    from iris.clickhouse.grants import (
        TIER_DBADMIN, TIER_DBREADER, TIER_DBWRITER,
        create_tier_roles, grant_tier_to_user, grant_tier_to_group,
        tier_role_name,
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{GLOBAL_ADMIN_ROLE}`")
    create_tier_roles(ch_client, database=db)
    # Seed: alice=admin, bob=writer, carol=reader, group_x=admin.
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBADMIN, username=f"{prefix}_alice")
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBWRITER, username=f"{prefix}_bob")
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBREADER, username=f"{prefix}_carol")
    grant_tier_to_group(ch_client, database=db, tier=TIER_DBADMIN, group=f"{prefix}_group_x")

    result = list_tier_members(ch_client, database=db)

    assert set(result.keys()) == {"admin", "reader", "writer"}
    admin_names = {(m["kind"], m["name"]) for m in result["admin"]}
    assert ("user", f"{prefix}_alice_USER") in admin_names
    assert ("role", f"{prefix}_group_x_GRP") in admin_names
    writer_names = {(m["kind"], m["name"]) for m in result["writer"]}
    assert ("user", f"{prefix}_bob_USER") in writer_names
    reader_names = {(m["kind"], m["name"]) for m in result["reader"]}
    assert ("user", f"{prefix}_carol_USER") in reader_names
```

Append to `tests/clickhouse/test_clickhouse_audit.py`:

```python
from iris.clickhouse.audit import (
    list_all_databases, list_all_grants, list_all_row_policies, list_all_users,
)


def test_list_all_users_returns_users_with_role_lists(ch_client, ch_settings, prefix):
    """Includes the username and the names of granted roles."""
    user = f"{prefix}_listusr"
    role = f"{prefix}_listrole"
    ch_client.command(f"CREATE USER `{user}` IDENTIFIED BY 'pw'")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    ch_client.command(f"GRANT `{role}` TO `{user}`")

    result = list_all_users(ch_client)
    by_name = {row["name"]: row for row in result}
    assert user in by_name
    assert role in by_name[user]["groups"]


def test_list_all_databases_returns_tier_counts(ch_client, ch_settings, prefix):
    """Each database row carries admin_count, writer_count, reader_count derived from system.role_grants."""
    from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
    from iris.clickhouse.grants import (
        TIER_DBADMIN, TIER_DBWRITER,
        create_tier_roles, grant_tier_to_user,
    )
    db = f"{prefix}_listdb"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{GLOBAL_ADMIN_ROLE}`")
    create_tier_roles(ch_client, database=db)
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBADMIN, username=f"{prefix}_listdb_alice")
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBWRITER, username=f"{prefix}_listdb_bob")

    result = list_all_databases(ch_client)
    by_name = {row["name"]: row for row in result}
    assert db in by_name
    assert by_name[db]["admin_count"] >= 1
    assert by_name[db]["writer_count"] >= 1
    assert by_name[db]["reader_count"] == 0


def test_list_all_row_policies_includes_seeded_policy(ch_client, ch_settings, prefix):
    """Returns full system.row_policies rows; seeded policy must appear."""
    from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
    from iris.clickhouse.grants import TIER_DBADMIN, tier_role_name
    db = f"{prefix}_listpol"
    table = "t"
    role = f"{prefix}_listpol_reader"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}` "
        + "(id UInt64, region String) ENGINE = MergeTree ORDER BY id"
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{tier_role_name(db, TIER_DBADMIN)}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{GLOBAL_ADMIN_ROLE}`")
    from iris.clickhouse.policies import add_row_policy
    add_row_policy(
        ch_client, database=db, table=table,
        column="region", role=role, value="EU",
    )

    result = list_all_row_policies(ch_client)
    seen = {(row["database"], row["table"]) for row in result}
    assert (db, table) in seen


def test_list_all_grants_includes_seeded_grant(ch_client, ch_settings, prefix):
    """Returns full system.grants rows; seeded grant must appear."""
    db = f"{prefix}_listgrants"
    user = f"{prefix}_listgrants_alice"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(f"CREATE USER `{user}` IDENTIFIED BY 'pw'")
    ch_client.command(f"GRANT SELECT ON `{db}`.* TO `{user}`")

    result = list_all_grants(ch_client)
    seen = {
        (row.get("user_name"), row.get("database"), row.get("access_type"))
        for row in result
    }
    assert (user, db, "SELECT") in seen
```

- [ ] **Step 3: Run to verify the tests fail (helpers don't exist yet)**

```bash
uv run pytest tests/clickhouse/test_clickhouse_grants.py::test_list_tier_members_returns_three_tier_dict tests/clickhouse/test_clickhouse_audit.py -k "list_all_" -v
```
Expected: FAIL with `ImportError` for the new helper names.

- [ ] **Step 4: Implement `list_tier_members` in `src/iris/clickhouse/grants.py`**

Append to `src/iris/clickhouse/grants.py` (after the existing `tier_role_name` and grant helpers):

```python
def list_tier_members(
    client: Client, *, database: str,
) -> dict[str, list[dict[str, str]]]:
    """Return tier-role members for ``database``, keyed by tier.

    Result shape: ``{"admin": [...], "reader": [...], "writer": [...]}``.
    Each entry is ``{"kind": "user" | "role", "name": <str>}`` — derived from
    ``system.role_grants`` rows that target the per-database tier role
    (``<database>_DBADMIN``, ``<database>_DBWRITER``, ``<database>_DBREADER``).
    """
    out: dict[str, list[dict[str, str]]] = {"admin": [], "reader": [], "writer": []}
    for tier_const, tier_key in (
        (TIER_DBADMIN, "admin"),
        (TIER_DBREADER, "reader"),
        (TIER_DBWRITER, "writer"),
    ):
        role = tier_role_name(database, tier_const)
        rows = client.query(
            "SELECT user_name, role_name FROM system.role_grants "
            + "WHERE granted_role_name = {r:String}",
            {"r": role},
        )
        for row in rows.named_results():
            u = row.get("user_name")
            r2 = row.get("role_name")
            if u:
                out[tier_key].append({"kind": "user", "name": cast(str, u)})
            elif r2:
                out[tier_key].append({"kind": "role", "name": cast(str, r2)})
    return out
```

If `cast` and `Client` aren't already imported in `grants.py`, add them at the top:

```python
from typing import cast
from clickhouse_connect.driver.client import Client
```

- [ ] **Step 5: Implement the 4 `list_all_*` helpers in `src/iris/clickhouse/audit.py`**

Append to `src/iris/clickhouse/audit.py`:

```python
def list_all_users(client: Client) -> list[dict[str, Any]]:
    """All CH users with their granted role names.

    Returns ``[{"name": <username>, "groups": [<role_name>, ...]}]``.
    The ``groups`` key is the list of role names granted to the user
    (group is iris's terminology in the auth feature; CH calls them roles).
    """
    rows = client.query("SELECT name FROM system.users ORDER BY name")
    users: list[dict[str, Any]] = []
    for row in rows.named_results():
        uname = cast(str, row["name"])
        role_rows = client.query(
            "SELECT granted_role_name FROM system.role_grants "
            + "WHERE user_name = {u:String}",
            {"u": uname},
        )
        roles = [cast(str, r["granted_role_name"]) for r in role_rows.named_results()]
        users.append({"name": uname, "groups": roles})
    return users


def list_all_databases(client: Client) -> list[dict[str, Any]]:
    """All databases with admin / writer / reader counts derived from
    ``system.role_grants`` against the per-database tier roles.

    Returns ``[{"name": <db>, "admin_count": int, "writer_count": int,
    "reader_count": int}]``.
    """
    from iris.clickhouse.grants import (
        TIER_DBADMIN, TIER_DBREADER, TIER_DBWRITER, tier_role_name,
    )
    db_rows = client.query("SELECT name FROM system.databases ORDER BY name")
    out: list[dict[str, Any]] = []
    for row in db_rows.named_results():
        db = cast(str, row["name"])
        counts: dict[str, int] = {}
        for tier_const, key in (
            (TIER_DBADMIN, "admin_count"),
            (TIER_DBWRITER, "writer_count"),
            (TIER_DBREADER, "reader_count"),
        ):
            role = tier_role_name(db, tier_const)
            count_rows = client.query(
                "SELECT count() AS c FROM system.role_grants "
                + "WHERE granted_role_name = {r:String}",
                {"r": role},
            )
            counts[key] = cast(int, next(count_rows.named_results())["c"])
        out.append({"name": db, **counts})
    return out


def list_all_row_policies(client: Client) -> list[dict[str, Any]]:
    """All rows from ``system.row_policies``, ordered by (database, table)."""
    rows = client.query(
        "SELECT * FROM system.row_policies ORDER BY database, table",
    )
    return list(rows.named_results())


def list_all_grants(client: Client) -> list[dict[str, Any]]:
    """All rows from ``system.grants``, ordered by (database, user, role)."""
    rows = client.query(
        "SELECT * FROM system.grants ORDER BY database, user_name, role_name",
    )
    return list(rows.named_results())
```

If `Any` and `cast` aren't already imported in `audit.py`, add them at the top:

```python
from typing import Any, cast
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_grants.py tests/clickhouse/test_clickhouse_audit.py -v 2>&1 | tail -20
```
Expected: all pass — existing + 5 new.

- [ ] **Step 7: Run gates on the modified source files**

```bash
uv run ruff check src/iris/clickhouse/grants.py src/iris/clickhouse/audit.py tests/clickhouse/test_clickhouse_grants.py tests/clickhouse/test_clickhouse_audit.py
uv run basedpyright --level warning src/iris/clickhouse/grants.py src/iris/clickhouse/audit.py tests/clickhouse/test_clickhouse_grants.py tests/clickhouse/test_clickhouse_audit.py
```
Expected: zero issues.

- [ ] **Step 8: Commit**

```bash
git add src/iris/clickhouse/grants.py src/iris/clickhouse/audit.py tests/clickhouse/test_clickhouse_grants.py tests/clickhouse/test_clickhouse_audit.py
git commit -m "$(cat <<'EOF'
feat(clickhouse): list_tier_members + 4 list_all_* inventory helpers

Five new sync helpers, one in grants.py and four in audit.py, each
taking a CH client as the first positional arg (matches the existing
audit.user_grants / policies.add_row_policy shape):

- grants.list_tier_members(client, *, database) — tier-role members
  for a database, grouped by tier {admin, reader, writer}.
- audit.list_all_users(client) — all CH users with their granted role
  names.
- audit.list_all_databases(client) — all databases with admin/writer/
  reader counts derived from system.role_grants.
- audit.list_all_row_policies(client) — full system.row_policies dump.
- audit.list_all_grants(client) — full system.grants dump.

These will be wrapped as async methods on AdminSession (4) and
DatabaseAdminSession (1, replacing list_admin_members) in the next
commit, restoring the typed-session boundary that the current
service.py implementations bypass via session._ch().

Each helper has a testcontainer-backed unit test that seeds the
relevant CH state under a prefix-namespaced entity and asserts the
helper sees it. 5 new tests total.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2 — Session methods + service.py + routes.py + test monkeypatch fixes

Single commit, several coordinated changes. End state: typed session methods replace the private `_ch()` access entirely; service.py becomes a thin aggregator; routes.py calls the typed methods directly; existing tests monkeypatch the typed methods.

**Files:**
- Modify: `src/iris/auth/views.py`
- Modify: `src/iris/features/authorization/service.py`
- Modify: `src/iris/features/authorization/routes.py`
- Create: `tests/auth/test_admin_inventory_methods.py`
- Create: `tests/auth/test_database_admin_list_members.py`
- Modify: `tests/features/test_authorization_admin_console.py`
- Modify: `tests/features/test_authorization_audit.py`

- [ ] **Step 1: Verify list_admin_members has no consumers other than service.py**

```bash
grep -rn "list_admin_members" src/ tests/
```
Expected: appears in `src/iris/auth/views.py` (definition), `src/iris/features/authorization/service.py` (caller), and possibly some tests. Note all caller sites — they'll be removed or updated in this task. If a test directly tests `list_admin_members`, plan to delete that test (it's superseded by the `list_members` test added in this task).

- [ ] **Step 2: Add the 5 new session methods + delete `list_admin_members`**

In `src/iris/auth/views.py`:

Find the existing `DatabaseAdminSession.list_admin_members` method and DELETE it. Then, in its place (or next to the other `list_*` methods on DatabaseAdminSession), add:

```python
    async def list_members(self) -> dict[str, list[dict[str, str]]]:
        """Return tier-role members for self.database, grouped by tier:
        {"admin": [...], "reader": [...], "writer": [...]}.
        Each entry is {"kind": "user" | "role", "name": <str>}.
        """
        client, _, _ = self._ch()
        return await asyncio.to_thread(
            grants.list_tier_members, client, database=self.database,
        )
```

In the `AdminSession` class (towards the bottom of the file), add 4 methods:

```python
    async def list_users(self) -> list[dict[str, Any]]:
        """All CH users with their granted role names."""
        client, _, _ = self._ch()
        return await asyncio.to_thread(audit.list_all_users, client)

    async def list_databases(self) -> list[dict[str, Any]]:
        """All databases with admin/writer/reader counts."""
        client, _, _ = self._ch()
        return await asyncio.to_thread(audit.list_all_databases, client)

    async def list_all_row_policies(self) -> list[dict[str, Any]]:
        """All system.row_policies rows."""
        client, _, _ = self._ch()
        return await asyncio.to_thread(audit.list_all_row_policies, client)

    async def list_all_grants(self) -> list[dict[str, Any]]:
        """All system.grants rows."""
        client, _, _ = self._ch()
        return await asyncio.to_thread(audit.list_all_grants, client)
```

`audit` and `grants` are already imported at the top of `views.py` (used by existing methods); no import changes needed.

- [ ] **Step 3: Add mock-based tests for the new session methods**

Create `tests/auth/test_admin_inventory_methods.py`:

```python
"""Tests for AdminSession.list_users / list_databases / list_all_row_policies / list_all_grants."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock


def _admin_session():
    from iris.auth.identity import User
    from iris.auth.rights import EMPTY_CAPABILITIES
    from iris.auth.views import AdminSession

    return AdminSession(
        id="x", user=User("s", "u", "U", ()),
        created_at=datetime.now(UTC), expires_at=datetime.now(UTC),
        data={}, capabilities=EMPTY_CAPABILITIES,
        client=MagicMock(), http_client=MagicMock(), settings=MagicMock(),
        store=MagicMock(),
    )


def test_list_users_calls_audit_helper(monkeypatch):
    captured = {}
    def fake(client):
        captured["client"] = client
        return [{"name": "alice", "groups": []}]
    monkeypatch.setattr("iris.auth.views.audit.list_all_users", fake)

    s = _admin_session()
    result = asyncio.run(s.list_users())
    assert result == [{"name": "alice", "groups": []}]
    assert captured["client"] is s.client


def test_list_databases_calls_audit_helper(monkeypatch):
    captured = {}
    def fake(client):
        captured["client"] = client
        return [{"name": "marketing", "admin_count": 1, "writer_count": 0, "reader_count": 0}]
    monkeypatch.setattr("iris.auth.views.audit.list_all_databases", fake)

    s = _admin_session()
    result = asyncio.run(s.list_databases())
    assert result[0]["name"] == "marketing"
    assert captured["client"] is s.client


def test_list_all_row_policies_calls_audit_helper(monkeypatch):
    captured = {}
    def fake(client):
        captured["client"] = client
        return [{"database": "marketing", "table": "events"}]
    monkeypatch.setattr("iris.auth.views.audit.list_all_row_policies", fake)

    s = _admin_session()
    result = asyncio.run(s.list_all_row_policies())
    assert result[0]["database"] == "marketing"
    assert captured["client"] is s.client


def test_list_all_grants_calls_audit_helper(monkeypatch):
    captured = {}
    def fake(client):
        captured["client"] = client
        return [{"user_name": "alice", "database": "marketing", "access_type": "SELECT"}]
    monkeypatch.setattr("iris.auth.views.audit.list_all_grants", fake)

    s = _admin_session()
    result = asyncio.run(s.list_all_grants())
    assert result[0]["user_name"] == "alice"
    assert captured["client"] is s.client
```

Create `tests/auth/test_database_admin_list_members.py`:

```python
"""Tests for DatabaseAdminSession.list_members."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock


def test_list_members_calls_grants_helper_with_self_database(monkeypatch):
    captured = {}
    def fake(client, *, database):
        captured["client"] = client
        captured["database"] = database
        return {"admin": [{"kind": "user", "name": "alice_USER"}],
                "reader": [], "writer": []}
    monkeypatch.setattr("iris.auth.views.grants.list_tier_members", fake)

    from iris.auth.identity import User
    from iris.auth.rights import EMPTY_CAPABILITIES
    from iris.auth.views import DatabaseAdminSession

    s = DatabaseAdminSession(
        id="x", user=User("s", "u", "U", ()),
        created_at=datetime.now(UTC), expires_at=datetime.now(UTC),
        data={}, capabilities=EMPTY_CAPABILITIES,
        client=MagicMock(), http_client=MagicMock(), settings=MagicMock(),
        store=MagicMock(), database="marketing",
    )

    result = asyncio.run(s.list_members())
    assert result["admin"][0]["name"] == "alice_USER"
    assert captured["database"] == "marketing"
    assert captured["client"] is s.client
```

- [ ] **Step 4: Rewrite `service.py`**

Replace the entire contents of `src/iris/features/authorization/service.py`:

```python
"""Read-side helpers for the Authorization feature.

Pure functions that take typed sessions and return template-ready
dicts. No ClickHouse access lives here — that's behind the typed
XxxSession methods. Consumers (the manage / admin_console intent
renderers) call the typed methods directly via the session passed
through the FastAPI dep chain.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from iris.auth.rights import Capabilities

if TYPE_CHECKING:
    from iris.auth.views import DatabaseAdminSession


def my_access_view(caps: Capabilities) -> dict[str, Any]:
    """Build the template context for the my_access render."""
    return {
        "reader_dbs": sorted(caps.db_reader),
        "writer_dbs": sorted(caps.db_writer),
        "admin_dbs": sorted(caps.db_admin),
        "can_create_database": caps.can_create_database,
        "is_admin": caps.is_admin,
    }


async def manage_view(session: "DatabaseAdminSession") -> dict[str, Any]:
    """Build the manage-page context for self.database."""
    members = await session.list_members()
    row_policies = await session.list_row_policies()
    audit = await session.list_grants()
    return {
        "members": members,
        "row_policies": row_policies,
        "audit": audit,
    }
```

The five `_ch()`-using functions (`list_members`, `list_all_users`, `list_all_databases`, `list_all_row_policies`, `list_all_grants`) are gone. `my_access_view` and `manage_view` are the only public surface.

- [ ] **Step 5: Update `routes.py` admin-console handlers**

In `src/iris/features/authorization/routes.py`, find each of the 5 admin-console routes (4 GETs + reprovision) and replace the `from iris.features.authorization.service import list_all_*` line and the call. Five edits:

```python
# Before (admin_users handler):
async def render_admin_users(...):
    from iris.features.authorization.service import list_all_users
    users = await list_all_users(admin)
    ...

# After:
async def render_admin_users(...):
    users = await admin.list_users()
    ...
```

Equivalent edits for `render_admin_databases` (`admin.list_databases()`), `render_admin_policies` (`admin.list_all_row_policies()`), `render_admin_audit` (`admin.list_all_grants()`), and `admin_reprovision_user` (also uses `list_all_users` to re-render the users sub-tab — switch to `admin.list_users()`).

After all five edits, no route in `routes.py` should reference any `service.list_*` name.

- [ ] **Step 6: Update existing test monkeypatches**

In `tests/features/test_authorization_admin_console.py`, find every `monkeypatch.setattr("iris.features.authorization.service.list_all_*", ...)` and update to the typed-session method. Specifically:

```python
# Before:
async def fake_users(_session): return [{"name": "alice", "groups": ["data-team"]}]
monkeypatch.setattr(
    "iris.features.authorization.service.list_all_users", fake_users,
)
```

becomes:

```python
async def fake_users(self): return [{"name": "alice", "groups": ["data-team"]}]
monkeypatch.setattr(
    "iris.auth.views.AdminSession.list_users", fake_users,
)
```

Note the signature change: the function now becomes a method (takes `self`, not `_session`).

The same edit pattern for `list_all_databases` → `AdminSession.list_databases`, `list_all_row_policies` → `AdminSession.list_all_row_policies`, `list_all_grants` → `AdminSession.list_all_grants`. Four total edits in this file.

In `tests/features/test_authorization_audit.py`, find the monkeypatch on `iris.features.authorization.service.list_members`:

```python
# Before:
monkeypatch.setattr(
    "iris.features.authorization.service.list_members",
    lambda s: fake_list_members(s),  # noqa: ARG005
)
```

Update to target the typed method:

```python
async def fake_list_members(self):
    return {"admin": [], "reader": [], "writer": []}
monkeypatch.setattr(
    "iris.auth.views.DatabaseAdminSession.list_members", fake_list_members,
)
```

In `tests/features/test_authorization_manage.py`: no changes needed (it monkeypatches `service.manage_view`, which survives unchanged).

- [ ] **Step 7: If a test directly tests `list_admin_members`, delete it**

```bash
grep -rn "list_admin_members" tests/
```

If matches exist, open each file and delete the test (it's superseded by `tests/auth/test_database_admin_list_members.py` which tests the replacement `list_members`). If grep returns nothing, skip.

- [ ] **Step 8: Run the full unit suite + gates**

```bash
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: all pass; no warnings; no errors. Test count grows by 5 (the new mock-based session method tests).

- [ ] **Step 9: Run the integration suite as a regression check (Authorization feature touches CH)**

```bash
uv run pytest tests/clickhouse/integration tests/auth/integration -q
```
Expected: 23 integration tests pass.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(features/authorization): typed session methods replace _ch() bypass

Five functions in src/iris/features/authorization/service.py
(list_members, list_all_users, list_all_databases, list_all_row_policies,
list_all_grants) used to call session._ch() to grab the raw CH client
and run queries directly — bypassing the XxxSession authorization
boundary and running as the iris service identity (which has admin
grants). This commit removes that bypass.

The five SQL bodies were moved to iris.clickhouse.{audit, grants} in
the previous commit. This commit:

1. Adds 5 typed session methods that wrap them:
   - DatabaseAdminSession.list_members() (replaces list_admin_members
     which had no other callers — the new method returns all three
     tiers in one shot, which is what every consumer needs anyway).
   - AdminSession.list_users(), list_databases(),
     list_all_row_policies(), list_all_grants().
2. Rewrites service.py: deletes the 5 _ch()-bypassing functions;
   manage_view becomes a thin aggregator over typed session methods
   (await session.list_members(), etc.). my_access_view unchanged.
3. Updates routes.py: 5 admin-console handlers call admin.list_*()
   directly instead of importing service.list_all_*.
4. Updates 2 existing test files to monkeypatch the typed session
   methods instead of the deleted service-layer functions.
5. Adds 5 new mock-based tests covering the typed session methods.

After this commit, src/ has zero # pyright: ignore[reportPrivateUsage]
and zero # noqa: SLF001. The next commit adds the convention to
CLAUDE.md and runs the final audit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3 — CLAUDE.md convention + final audit

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Find the insertion point in CLAUDE.md**

```bash
grep -n "^## Conventions\|^- \*\*Tests don't mock the database" CLAUDE.md
```
Expected: shows the "Conventions" section header and the last bullet ("Tests don't mock the database"). Insert the new bullet immediately after that one.

- [ ] **Step 2: Add the convention bullet**

In `CLAUDE.md`, find the bullet:

```markdown
- **Tests don't mock the database**: `tests/clickhouse/` uses a real CH testcontainer (session-scoped). Per-test isolation is the `prefix` fixture (UUID-prefixed entity names).
```

Immediately after that bullet (and before the next blank line / next section), insert:

```markdown
- **Don't access private fields across module boundaries.** A name with
  a leading underscore (`_field`, `_method`) is private to the module
  that defines it. Reaching into `obj._field` from another module — or
  adding `# pyright: ignore[reportPrivateUsage]` / `# noqa: SLF001` to
  suppress the warning — is forbidden in `src/`. If you need the
  functionality, propose a helper function (or method) on the owning
  module that exposes it through a proper public API. The suppression
  comment is the smell, not the fix. Tests are exempt by config
  (basedpyright + ruff disable both checks for `tests/`); the rule
  applies to `src/`.

  In iris specifically, the `XxxSession` hierarchy in `iris.auth.views`
  IS the authorization boundary; reaching into `session._ch()` from a
  feature module bypasses the entire tier model and is a security
  violation. If a feature needs CH access, add a sync SQL helper in
  `iris.clickhouse.<module>` (`audit.py` for read-only system queries,
  `grants.py` for tier-role helpers, `policies.py` for row policies).
  Wrap it as an async method on the right `XxxSession` subclass —
  `AdminSession` for global, `DatabaseAdminSession` for per-database,
  `DatabaseSession` for impersonated user queries. Routes and service
  code consume the typed method.
```

- [ ] **Step 3: Run the final audit (the headline of this whole spec)**

Three checks. ALL must return zero matches in `src/`.

```bash
echo "=== Check 1: reportPrivateUsage suppressions in src/ ==="
grep -rn "reportPrivateUsage" src/ && echo "FAIL — suppressions still present" || echo "PASS — zero matches"

echo
echo "=== Check 2: noqa: SLF001 in src/ ==="
grep -rn "SLF001" src/ && echo "FAIL — suppressions still present" || echo "PASS — zero matches"

echo
echo "=== Check 3: cross-module session-underscore access in src/ ==="
grep -rn "session\._\|admin\._\|creator\._\|db\._\|db_session\._" src/ && echo "FAIL — session-private access present" || echo "PASS — zero matches"
```

Expected: all three say `PASS — zero matches`. If any FAIL, the migration is not complete — investigate the remaining instance and fix before committing.

- [ ] **Step 4: Run the gates one more time as the final acceptance check**

```bash
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: all pass; zero issues. (Pyright `--level warning` is the strongest mechanical check that no module-crossing private access exists, since `reportPrivateUsage` warns on it without a suppression. If the gate is clean AND the grep of suppressions is zero, the invariant holds.)

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs(claude-md): no private access across module boundaries

Adds the general convention (no underscore-prefixed cross-module
access; the # pyright: ignore[reportPrivateUsage] / # noqa: SLF001
suppression comment is the smell, not the fix; propose a helper
function instead) to CLAUDE.md, with the iris-specific application
to the session authorization boundary called out as a security
implication.

Final audit: src/ has zero reportPrivateUsage suppressions, zero
SLF001 suppressions, zero session-underscore cross-module access.
basedpyright --level warning is clean (the reportPrivateUsage check
is the deepest mechanical safeguard — without suppressions it warns
on every module-crossing private access).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Recap

3 tasks, 3 commits. End state:

- 5 sync SQL helpers in `iris.clickhouse.audit` + `iris.clickhouse.grants`, each tested against the testcontainer.
- 5 async typed-session methods (4 on `AdminSession`, 1 on `DatabaseAdminSession` replacing `list_admin_members`), each tested with a mock-based wrapper.
- `service.py` is a thin aggregator over typed session methods; no `_ch()` calls; no `pyright: ignore` / `noqa: SLF001` suppressions.
- `routes.py` admin-console handlers call `admin.list_*()` directly.
- `CLAUDE.md` documents the rule in general, with iris-specific application.
- `src/` has zero `reportPrivateUsage` suppressions, zero `SLF001` suppressions, zero session-underscore cross-module access (verified by grep + basedpyright --level warning at every commit).
