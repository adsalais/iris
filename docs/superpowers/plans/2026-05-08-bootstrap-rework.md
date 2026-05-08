# Bootstrap Rework + iris_global_admin Sentinel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `IRIS_BOOTSTRAP_USER` + `CLICKHOUSE_SERVICE_ADMIN_*` env vars with `CLICKHOUSE_ADMIN_USER` + `CLICKHOUSE_ADMIN_GROUP`, add a managed `iris_global_admin` sentinel role that wildcard row policies attach to (alongside `<database>_DBADMIN`), and clean up the lazy-import smell by moving `bootstrap_admin` from `iris.auth` to `iris.clickhouse`.

**Architecture:** A new `bootstrap_admin(client, *, admin_user, admin_group)` in `iris.clickhouse.bootstrap` always creates `iris_global_admin`, then optionally creates `<user>_USER` and/or `<group>_GRP` admin roles, granting `iris_global_admin` to each. `add_row_policy` emits three `CREATE ROW POLICY` statements: the restrictive policy plus per-table wildcards for `iris_global_admin` and `<database>_DBADMIN`. `iris.auth.bootstrap` is deleted; `install` is dropped from `iris.clickhouse.__init__`'s re-exports; Session methods in `iris.auth.identity` switch to top-level imports of `iris.clickhouse.handle.*_impl`.

**Tech Stack:** Python 3.13, FastAPI, frozen dataclasses, `clickhouse_connect`, `httpx`, pytest with the existing CH testcontainer (`tests/clickhouse/conftest.py`).

**Spec:** `docs/superpowers/specs/2026-05-08-bootstrap-rework-design.md`

---

## File Structure

### Modified files

- `src/iris/clickhouse/bootstrap.py` — replace `ensure_service_admin` with the new `bootstrap_admin(client, *, admin_user, admin_group)` that creates `iris_global_admin` + optional user/group admin roles. The old `ensure_service_admin` deletes (its job — creating a configured role granted to a configured user — disappears with `service_admin_role`).
- `src/iris/clickhouse/config.py` — `ClickHouseSettings` drops `service_admin_user` and `service_admin_role` fields. `from_env` no longer reads `CLICKHOUSE_SERVICE_ADMIN_*`.
- `src/iris/clickhouse/policies.py` — `add_row_policy` drops the `settings` parameter; emits three `CREATE ROW POLICY` statements (restrictive + two wildcards).
- `src/iris/clickhouse/users.py` — `init_user_rights` switches the IMPERSONATE grant from `settings.service_admin_user` to `settings.user`.
- `src/iris/clickhouse/install.py` — drops `from iris.auth.bootstrap import bootstrap_admin`; imports `bootstrap_admin` from `iris.clickhouse.bootstrap`. Reads `CLICKHOUSE_ADMIN_USER` and `CLICKHOUSE_ADMIN_GROUP` from `os.environ` directly. Drops the `auth_bootstrap_user` app.state read.
- `src/iris/clickhouse/__init__.py` — drops `install` from imports + `__all__`. Adds `bootstrap_admin` (it's the new bootstrap entry point worth exposing).
- `src/iris/clickhouse/handle.py` — `add_row_policy_impl` drops the `settings` parameter (since `add_row_policy` no longer takes one).
- `src/iris/auth/identity.py` — `AdminSession.add_row_policy` drops the `settings=self.settings` argument. **Replace lazy method-body imports with top-level imports** of `iris.clickhouse.handle.*_impl` functions.
- `src/iris/auth/config.py` — `AuthSettings` drops the `bootstrap_user` field. `from_env` no longer reads `IRIS_BOOTSTRAP_USER`.
- `src/iris/auth/routes.py` — drops the `app.state.auth_bootstrap_user = settings.bootstrap_user` line in `install`.
- `src/iris/auth/__init__.py` — drops `bootstrap_admin` import + export.
- `src/iris/app.py` — switches `from iris.clickhouse.install import install` (already this form, but verify since `__init__` change might have affected it).
- `tests/conftest.py` — drops `IRIS_BOOTSTRAP_USER`. Tests at this level don't need a bootstrap (no CH installed). Add `CLICKHOUSE_ADMIN_USER` setdefault for the conftest-level imports if any code still reads it (it doesn't, after the refactor).
- `tests/clickhouse/conftest.py` — drops `CLICKHOUSE_SERVICE_ADMIN_USER` / `CLICKHOUSE_SERVICE_ADMIN_ROLE` env-var setting. The svc user privilege grants stay; iris_svc remains the connection identity (`CLICKHOUSE_USER=iris_svc`).
- `tests/clickhouse/test_clickhouse_settings.py` — drops assertions about `service_admin_user` / `service_admin_role`. Asserts they're no longer required.
- `tests/clickhouse/test_clickhouse_bootstrap.py` — DELETE entirely. Replaced by tests in `test_bootstrap_admin.py` for the new `bootstrap_admin` shape.
- `tests/clickhouse/test_bootstrap_admin.py` — rewrites for the new function signature `bootstrap_admin(client, *, admin_user=None, admin_group=None)`. Adds tests for the group channel and for `iris_global_admin` creation.
- `tests/clickhouse/test_clickhouse_policies.py` — assertions for the new third `CREATE ROW POLICY` statement (`<database>_DBADMIN` wildcard) and the renamed second wildcard target (`iris_global_admin`).
- `tests/clickhouse/test_install.py` — drops assertions about `auth_bootstrap_user`. Adds an assertion that bootstrap creates `iris_global_admin`.
- `CLAUDE.md` — env-var section drops `CLICKHOUSE_SERVICE_ADMIN_*` and `IRIS_BOOTSTRAP_USER`, adds `CLICKHOUSE_ADMIN_USER` and `CLICKHOUSE_ADMIN_GROUP`. Bootstrap section explains the two-channel + `iris_global_admin` model.

### Deleted files

- `src/iris/auth/bootstrap.py` — moved to `iris.clickhouse.bootstrap`.
- `tests/clickhouse/test_clickhouse_bootstrap.py` — its function (`ensure_service_admin`) deletes; coverage moves to `test_bootstrap_admin.py`.

### Order of operations

Tasks 1-2 are additive and self-contained (compile-clean each). Tasks 3-7 are the breakage window — the env-var rename, settings-field drop, and dependent module changes touch many files at once. Commit only at the end of Task 7. Task 8 is the smoke / lint / verification.

---

## Task 1: Add `bootstrap_admin` to `iris.clickhouse.bootstrap` (additive)

Move and reshape the existing `bootstrap_admin` from `iris.auth.bootstrap` to `iris.clickhouse.bootstrap`, alongside the existing `ensure_service_admin`. Take new kwargs `admin_user` and `admin_group`. Always create `iris_global_admin`. Keep the testcontainer `CURRENT GRANTS` fallback.

**Files:**
- Modify: `src/iris/clickhouse/bootstrap.py`
- Test: `tests/clickhouse/test_bootstrap_admin.py`

- [ ] **Step 1: Replace `src/iris/clickhouse/bootstrap.py`**

```python
"""ClickHouse-side bootstrap.

Two responsibilities, both idempotent:

- ``ensure_service_admin``: creates the configured CH role and grants it to the
  configured user, so iris's connection identity has the privileges it needs to
  manage RBAC. (Deprecated — to be removed once callers stop referencing it.)

- ``bootstrap_admin``: at iris launch, creates the ``iris_global_admin`` sentinel
  role and (optionally) bootstraps an admin user role + admin group role from
  ``CLICKHOUSE_ADMIN_USER`` / ``CLICKHOUSE_ADMIN_GROUP`` env vars. Each admin role
  is granted full admin privileges plus ``iris_global_admin`` (so wildcard row
  policies on ``iris_global_admin`` apply to every admin's effective role set).
"""

from __future__ import annotations

import logging
from typing import cast

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import DatabaseError

from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.identifiers import quote_identifier
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX

logger = logging.getLogger("iris.clickhouse.bootstrap")

GLOBAL_ADMIN_ROLE = "iris_global_admin"


def ensure_service_admin(client: Client, settings: ClickHouseSettings) -> None:
    """DEPRECATED — kept for the breakage window only.

    The ``service_admin_role`` concept goes away in this refactor. This function
    becomes a no-op once ``ClickHouseSettings`` drops ``service_admin_user`` /
    ``service_admin_role`` (Task 3). Don't extend it.
    """
    role = quote_identifier(settings.service_admin_role, kind="service_admin_role")
    user = quote_identifier(settings.service_admin_user, kind="service_admin_user")
    client.command(f"CREATE ROLE IF NOT EXISTS {role}")
    client.command(f"GRANT {role} TO {user}")


def _has_admin_role_with_suffix(client: Client, suffix: str) -> bool:
    """Detect whether some role with the given suffix already holds the admin
    marker (ROLE ADMIN at global scope with grant_option=1)."""
    rows = client.query(
        """
        SELECT count() FROM system.grants
        WHERE access_type = 'ROLE ADMIN'
          AND grant_option = 1
          AND database IS NULL
          AND endsWith(role_name, {suffix:String})
        """,
        parameters={"suffix": suffix},
    ).result_rows
    return cast(int, rows[0][0]) > 0


def _grant_full_admin(client: Client, *, role_q: str) -> None:
    """``GRANT ALL ON *.* WITH GRANT OPTION``, with a ``CURRENT GRANTS`` fallback
    for the testcontainer's missing NAMED COLLECTION ADMIN privilege."""
    try:
        client.command(f"GRANT ALL ON *.* TO {role_q} WITH GRANT OPTION")
    except DatabaseError as err:
        if "NAMED COLLECTION ADMIN" not in str(err):
            raise
        client.command(
            f"GRANT CURRENT GRANTS ON *.* TO {role_q} WITH GRANT OPTION"
        )


def bootstrap_admin(
    client: Client,
    *,
    admin_user: str | None = None,
    admin_group: str | None = None,
) -> None:
    """Bootstrap iris's admin tier on ClickHouse. Idempotent across both channels.

    Always creates the ``iris_global_admin`` sentinel role (no privileges of its
    own — wildcard row policies attach to it). When ``admin_user`` is supplied
    AND no role with the ``_USER`` suffix already holds admin, creates
    ``<admin_user>_USER`` with full admin grants and ``iris_global_admin``
    granted to it. When ``admin_group`` is supplied, the same for
    ``<admin_group>_GRP``.

    Both channels are independently idempotent: re-running with an existing
    admin in the channel is a no-op. Wiping CH and restarting re-triggers
    both.
    """
    global_admin_q = quote_identifier(GLOBAL_ADMIN_ROLE, kind="role")
    client.command(f"CREATE ROLE IF NOT EXISTS {global_admin_q}")

    if admin_user and not _has_admin_role_with_suffix(client, USER_ROLE_SUFFIX):
        role = f"{admin_user}{USER_ROLE_SUFFIX}"
        role_q = quote_identifier(role, kind="role")
        client.command(f"CREATE ROLE IF NOT EXISTS {role_q}")
        _grant_full_admin(client, role_q=role_q)
        client.command(f"GRANT {global_admin_q} TO {role_q}")
        logger.info("bootstrap: seeded admin role for user=%s", admin_user)

    if admin_group and not _has_admin_role_with_suffix(client, GROUP_ROLE_SUFFIX):
        role = f"{admin_group}{GROUP_ROLE_SUFFIX}"
        role_q = quote_identifier(role, kind="role")
        client.command(f"CREATE ROLE IF NOT EXISTS {role_q}")
        _grant_full_admin(client, role_q=role_q)
        client.command(f"GRANT {global_admin_q} TO {role_q}")
        logger.info("bootstrap: seeded admin role for group=%s", admin_group)
```

- [ ] **Step 2: Rewrite `tests/clickhouse/test_bootstrap_admin.py`**

```python
"""Tests for iris.clickhouse.bootstrap.bootstrap_admin against the CH testcontainer.

Bootstrap inspects global CH state (any role with the appropriate suffix
holding ROLE ADMIN+WGO at global scope counts as "admin already present").
The session-scoped container shares state across tests, so tests that
depend on "no admin exists" clear matching roles at setup.
"""
from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE, bootstrap_admin
from iris.clickhouse.rights import derive_rights


def _drop_admin_roles_with_suffix(ch_client, suffix: str) -> None:
    rows = ch_client.query(
        """
        SELECT DISTINCT role_name FROM system.grants
        WHERE access_type = 'ROLE ADMIN'
          AND grant_option = 1
          AND database IS NULL
          AND endsWith(role_name, {s:String})
        """,
        parameters={"s": suffix},
    ).result_rows
    for (name,) in rows:
        ch_client.command(f"DROP ROLE IF EXISTS `{name}`")


def test_bootstrap_creates_global_admin_role_unconditionally(ch_client):
    bootstrap_admin(ch_client)
    rows = ch_client.query(
        "SELECT count() FROM system.roles WHERE name = {n:String}",
        parameters={"n": GLOBAL_ADMIN_ROLE},
    ).result_rows
    assert rows[0][0] == 1


def test_bootstrap_user_channel_creates_admin_user_role(ch_client, prefix):
    _drop_admin_roles_with_suffix(ch_client, "_USER")
    user = f"{prefix}_first_admin"
    bootstrap_admin(ch_client, admin_user=user)

    # _USER role exists with admin grants
    r = derive_rights(ch_client, username=user, groups=[])
    assert r.is_admin is True

    # iris_global_admin is granted to the user role
    granted = ch_client.query(
        """
        SELECT granted_role_name FROM system.role_grants
        WHERE role_name = {r:String}
        """,
        parameters={"r": f"{user}_USER"},
    ).result_rows
    assert any(g[0] == GLOBAL_ADMIN_ROLE for g in granted)


def test_bootstrap_group_channel_creates_admin_group_role(ch_client, prefix):
    _drop_admin_roles_with_suffix(ch_client, "_GRP")
    group = f"{prefix}_iris_admin"
    bootstrap_admin(ch_client, admin_group=group)

    group_role = f"{group}_GRP"

    # group _GRP role exists with admin grants — verify by simulating membership
    rows = ch_client.query(
        """
        SELECT count() FROM system.grants
        WHERE role_name = {r:String}
          AND access_type = 'ROLE ADMIN'
          AND grant_option = 1
        """,
        parameters={"r": group_role},
    ).result_rows
    assert rows[0][0] == 1

    # iris_global_admin is granted to the group role
    granted = ch_client.query(
        """
        SELECT granted_role_name FROM system.role_grants
        WHERE role_name = {r:String}
        """,
        parameters={"r": group_role},
    ).result_rows
    assert any(g[0] == GLOBAL_ADMIN_ROLE for g in granted)


def test_bootstrap_user_channel_skips_when_admin_already_exists(ch_client, prefix):
    _drop_admin_roles_with_suffix(ch_client, "_USER")
    a = f"{prefix}_existing_admin"
    b = f"{prefix}_second_admin"
    bootstrap_admin(ch_client, admin_user=a)
    bootstrap_admin(ch_client, admin_user=b)
    # Second call must skip — b shouldn't end up admin.
    r = derive_rights(ch_client, username=b, groups=[])
    assert r.is_admin is False


def test_bootstrap_group_channel_skips_when_admin_already_exists(ch_client, prefix):
    _drop_admin_roles_with_suffix(ch_client, "_GRP")
    a = f"{prefix}_existing_grp"
    b = f"{prefix}_second_grp"
    bootstrap_admin(ch_client, admin_group=a)
    bootstrap_admin(ch_client, admin_group=b)
    rows = ch_client.query(
        """
        SELECT count() FROM system.grants
        WHERE role_name = {r:String}
          AND access_type = 'ROLE ADMIN'
          AND grant_option = 1
        """,
        parameters={"r": f"{b}_GRP"},
    ).result_rows
    assert rows[0][0] == 0


def test_bootstrap_idempotent_for_same_inputs(ch_client, prefix):
    """Two calls in a row don't error. Whether the user actually ends up admin
    depends on prior session state — the contract is "no error on re-run"."""
    user = f"{prefix}_repeat"
    bootstrap_admin(ch_client, admin_user=user)
    bootstrap_admin(ch_client, admin_user=user)


def test_bootstrap_both_channels_independent(ch_client, prefix):
    _drop_admin_roles_with_suffix(ch_client, "_USER")
    _drop_admin_roles_with_suffix(ch_client, "_GRP")
    user = f"{prefix}_both_user"
    group = f"{prefix}_both_grp"
    bootstrap_admin(ch_client, admin_user=user, admin_group=group)

    # Both roles got admin
    r = derive_rights(ch_client, username=user, groups=[])
    assert r.is_admin is True

    rows = ch_client.query(
        """
        SELECT count() FROM system.grants
        WHERE role_name = {r:String}
          AND access_type = 'ROLE ADMIN'
          AND grant_option = 1
        """,
        parameters={"r": f"{group}_GRP"},
    ).result_rows
    assert rows[0][0] == 1
```

- [ ] **Step 3: Run the targeted test**

Run: `uv run pytest tests/clickhouse/test_bootstrap_admin.py -v`

Expected: 7 tests pass. The new `bootstrap_admin` works against the testcontainer (whose svc user has `CURRENT GRANTS WITH GRANT OPTION`, triggering the fallback path).

If a test fails because the existing `iris.auth.bootstrap.bootstrap_admin` module is still in use by `iris.clickhouse.install` and somehow shadowing the new symbol — that's expected; this task is additive and the install wiring still imports the old function. Tests in this file import from the new location, so they should be fine.

- [ ] **Step 4: Commit**

```bash
git add src/iris/clickhouse/bootstrap.py tests/clickhouse/test_bootstrap_admin.py
git commit -m "$(cat <<'EOF'
feat(clickhouse): bootstrap_admin with two-channel admin + iris_global_admin

Move bootstrap_admin from iris.auth.bootstrap to iris.clickhouse.bootstrap
and reshape its signature: takes admin_user= and admin_group= kwargs and
always creates the iris_global_admin sentinel role. The admin user/group
roles get admin grants AND iris_global_admin granted to them, so wildcard
row policies on iris_global_admin apply to every admin's effective role
set.

Additive: the old ensure_service_admin still exists in this module
(deprecated, deletes in Task 3) and iris.auth.bootstrap.bootstrap_admin
is still in use by iris.clickhouse.install. Tasks 3-7 close this gap.
EOF
)"
```

---

## Task 2: Add the `iris_global_admin` + `<database>_DBADMIN` wildcards in `add_row_policy_impl` (still using settings.service_admin_role for the old wildcard, additive)

This task adds the two new wildcards alongside the existing `service_admin_role` wildcard. The policies module gains the new behavior; the old wildcard stays for now. Settings unchanged. After Task 5 drops `service_admin_role`, the old wildcard goes too.

Wait — this is awkward to stage cleanly because the two new wildcards refer to literal strings (no `settings` change needed) while the old wildcard reads `settings.service_admin_role`. Let me restructure.

**Actually — combine Tasks 2 and 3 below into the breakage window.** This task is a no-op; skip directly to Task 3.

---

## Task 3: Replace `add_row_policy` and `add_row_policy_impl` to drop `settings` and emit the three statements

The breakage window starts here. Several modules need to update in lockstep.

**Files:**
- Modify: `src/iris/clickhouse/policies.py`
- Modify: `src/iris/clickhouse/handle.py`
- Modify: `src/iris/auth/identity.py`
- Test: `tests/clickhouse/test_clickhouse_policies.py`

- [ ] **Step 1: Replace `src/iris/clickhouse/policies.py`**

```python
"""Row-policy CRUD helpers."""

from __future__ import annotations

from clickhouse_connect.driver.client import Client

from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
from iris.clickhouse.grants import TIER_DBADMIN, tier_role_name
from iris.clickhouse.identifiers import (
    policy_name,
    quote_identifier,
    quote_string,
    validate_identifier,
)


def add_row_policy(
    client: Client,
    *,
    database: str,
    table: str,
    column: str,
    role: str,
    value: str,
) -> None:
    """Create a row policy ``<column> = <value>`` for ``<role>`` on ``<database>.<table>``.

    Also ensures two ``USING 1`` wildcard policies exist on the same table:

    - One for ``iris_global_admin`` (every global admin sees all rows).
    - One for ``<database>_DBADMIN`` (every per-database admin sees all rows).

    Names of the wildcard policies are deterministic so re-runs are idempotent
    via ``CREATE ROW POLICY IF NOT EXISTS``. The wildcards persist after the
    last restrictive policy is revoked — this matches the prior service-admin
    wildcard behavior.
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(column, kind="column")
    validate_identifier(role, kind="role")

    db_q = quote_identifier(database, kind="database")
    table_q = quote_identifier(table, kind="table")
    column_q = quote_identifier(column, kind="column")
    role_q = quote_identifier(role, kind="role")

    # 1. The restrictive policy the caller asked for.
    name = policy_name(database, table, role, value)
    name_q = quote_identifier(name, kind="policy")
    client.command(
        f"CREATE ROW POLICY IF NOT EXISTS {name_q} ON {db_q}.{table_q} "
        f"FOR SELECT USING {column_q} = {quote_string(value)} TO {role_q}"
    )

    # 2. The iris_global_admin wildcard (deterministic name, idempotent).
    ga_name = f"{database}_{table}_{GLOBAL_ADMIN_ROLE}"
    ga_name_q = quote_identifier(ga_name, kind="policy")
    ga_role_q = quote_identifier(GLOBAL_ADMIN_ROLE, kind="role")
    client.command(
        f"CREATE ROW POLICY IF NOT EXISTS {ga_name_q} ON {db_q}.{table_q} "
        f"FOR SELECT USING 1 TO {ga_role_q}"
    )

    # 3. The <database>_DBADMIN wildcard (deterministic name, idempotent).
    dba_role = tier_role_name(database, TIER_DBADMIN)
    dba_name = f"{database}_{table}_{dba_role}"
    dba_name_q = quote_identifier(dba_name, kind="policy")
    dba_role_q = quote_identifier(dba_role, kind="role")
    client.command(
        f"CREATE ROW POLICY IF NOT EXISTS {dba_name_q} ON {db_q}.{table_q} "
        f"FOR SELECT USING 1 TO {dba_role_q}"
    )


def revoke_row_policy(
    client: Client,
    *,
    database: str,
    table: str,
    role: str,
    value: str,
) -> None:
    """Drop the named restrictive row policy created by ``add_row_policy``.

    Wildcards on ``iris_global_admin`` and ``<database>_DBADMIN`` are *not*
    dropped — they may apply to other restrictive policies on the same table,
    and persist intentionally so admins continue to see all rows.
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(role, kind="role")

    db_q = quote_identifier(database, kind="database")
    table_q = quote_identifier(table, kind="table")
    name_q = quote_identifier(policy_name(database, table, role, value), kind="policy")
    client.command(f"DROP ROW POLICY IF EXISTS {name_q} ON {db_q}.{table_q}")
```

- [ ] **Step 2: Update `add_row_policy_impl` in `src/iris/clickhouse/handle.py`**

Find:

```python
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
```

Replace with:

```python
async def add_row_policy_impl(
    client: Client,
    *,
    database: str,
    table: str,
    column: str,
    role: str,
    value: str,
) -> None:
    await asyncio.to_thread(
        add_row_policy,
        client,
        database=database,
        table=table,
        column=column,
        role=role,
        value=value,
    )
```

The `settings` parameter goes away. `add_row_policy` no longer needs it.

- [ ] **Step 3: Update `AdminSession.add_row_policy` in `src/iris/auth/identity.py`**

Find:

```python
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
```

Replace with:

```python
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
        )
```

(The lazy `from ... import` stays for now — Task 7 hoists it to the top of the file.)

- [ ] **Step 4: Update `tests/clickhouse/test_clickhouse_policies.py`**

Read it first: `cat tests/clickhouse/test_clickhouse_policies.py`

For each test that calls `add_row_policy(...)`, drop the `settings=...` argument. For each test that asserts about the second wildcard policy (the old service_admin_role one), update the expected role name to `iris_global_admin` and add a separate assertion for the third wildcard targeting `<database>_DBADMIN`.

Concrete pattern: if a test expects two policies on a table after one `add_row_policy` call (one restrictive + one wildcard), update it to expect three (restrictive + iris_global_admin wildcard + DBADMIN wildcard). The DBADMIN role name pattern is `<database>_DBADMIN` — for a test using `database="orders"`, the third role is `orders_DBADMIN`.

If the test references `settings.service_admin_role` for assertions, replace with the literal string `"iris_global_admin"`.

- [ ] **Step 5: Defer commit**

The build is broken — `add_row_policy` and `add_row_policy_impl` no longer accept `settings`, but `iris.clickhouse.policies` still imports `ClickHouseSettings` (unused — drop the import) and `iris.clickhouse.handle.py` still imports `ClickHouseSettings` (still used by `query_as_service_impl` and `reprovision_user_impl` — keep). Continue to Task 4.

---

## Task 4: Drop `service_admin_user` and `service_admin_role` from `ClickHouseSettings`; switch IMPERSONATE grantee in `init_user_rights`

**Files:**
- Modify: `src/iris/clickhouse/config.py`
- Modify: `src/iris/clickhouse/users.py`
- Test: `tests/clickhouse/test_clickhouse_settings.py`

- [ ] **Step 1: Replace `src/iris/clickhouse/config.py`**

```python
"""Settings for the ClickHouse module, loaded from the process environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _required(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ValueError(f"{name} is required")
    return val


def _get_bool(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("true", "1"):
        return True
    if raw in ("false", "0"):
        return False
    raise ValueError(f"{name} must be 'true' or 'false', got {raw!r}")


@dataclass(frozen=True, slots=True)
class ClickHouseSettings:
    host: str
    port: int
    user: str
    password: str
    secure: bool
    verify: bool
    ca_cert_path: str | None

    @classmethod
    def from_env(cls) -> "ClickHouseSettings":
        host = _required("CLICKHOUSE_HOST")
        port_raw = _required("CLICKHOUSE_PORT")
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ValueError(
                f"CLICKHOUSE_PORT must be an integer, got {port_raw!r}"
            ) from exc
        user = _required("CLICKHOUSE_USER")
        password = _required("CLICKHOUSE_PASSWORD")
        secure = _get_bool("CLICKHOUSE_SECURE")
        verify = _get_bool("CLICKHOUSE_VERIFY")
        ca_cert_path = os.environ.get("CLICKHOUSE_CA_CERT_PATH", "").strip() or None

        return cls(
            host=host,
            port=port,
            user=user,
            password=password,
            secure=secure,
            verify=verify,
            ca_cert_path=ca_cert_path,
        )
```

- [ ] **Step 2: Update `src/iris/clickhouse/users.py`**

Find the last block of `init_user_rights`:

```python
    service_admin_q = quote_identifier(
        settings.service_admin_user, kind="service_admin_user"
    )
    client.command(f"GRANT IMPERSONATE ON {user_q} TO {service_admin_q}")
```

Replace with:

```python
    # The IMPERSONATE grantee is the CH user iris connects as. After dropping
    # CLICKHOUSE_SERVICE_ADMIN_USER, that's just settings.user.
    impersonator_q = quote_identifier(settings.user, kind="user")
    client.command(f"GRANT IMPERSONATE ON {user_q} TO {impersonator_q}")
```

- [ ] **Step 3: Update `tests/clickhouse/test_clickhouse_settings.py`**

Read it first: `cat tests/clickhouse/test_clickhouse_settings.py`

Drop every line that sets `CLICKHOUSE_SERVICE_ADMIN_USER` or `CLICKHOUSE_SERVICE_ADMIN_ROLE` or asserts about the corresponding fields. Pattern: any test setting these env vars should have those `env.setenv(...)` lines removed. Any test asserting `s.service_admin_user == ...` or `s.service_admin_role == ...` should have those assertions removed.

If the test expected `CLICKHOUSE_SERVICE_ADMIN_USER` to be a "required" env var that raises when missing, that test should be deleted (the var no longer exists, so it can't be required-or-missing).

- [ ] **Step 4: Defer commit** — continue to Task 5.

---

## Task 5: Drop `ensure_service_admin` and update `iris.clickhouse.bootstrap`

**Files:**
- Modify: `src/iris/clickhouse/bootstrap.py`
- Delete: `tests/clickhouse/test_clickhouse_bootstrap.py`

- [ ] **Step 1: Drop `ensure_service_admin` from `src/iris/clickhouse/bootstrap.py`**

Open `src/iris/clickhouse/bootstrap.py` and delete the `ensure_service_admin` function. Also drop the `from iris.clickhouse.config import ClickHouseSettings` import (no longer referenced after Task 4). Drop the file's docstring section about the deprecated function.

The remaining file should match Task 1's content minus the deprecated function.

- [ ] **Step 2: Delete `tests/clickhouse/test_clickhouse_bootstrap.py`**

```bash
git rm tests/clickhouse/test_clickhouse_bootstrap.py
```

Coverage moves to `test_bootstrap_admin.py`.

- [ ] **Step 3: Defer commit** — continue to Task 6.

---

## Task 6: Wire the new bootstrap into `iris.clickhouse.install`; drop `iris.auth.bootstrap`

**Files:**
- Modify: `src/iris/clickhouse/install.py`
- Modify: `src/iris/auth/__init__.py`
- Modify: `src/iris/auth/config.py`
- Modify: `src/iris/auth/routes.py`
- Delete: `src/iris/auth/bootstrap.py`
- Modify: `tests/conftest.py`
- Modify: `tests/clickhouse/conftest.py`

- [ ] **Step 1: Replace `src/iris/clickhouse/install.py`**

```python
"""Wire iris.clickhouse into a FastAPI app.

Builds the shared clickhouse-connect Client and a shared httpx.AsyncClient for
impersonated queries (see iris.clickhouse.handle for why both are needed),
runs the CH-side bootstrap (creates iris_global_admin sentinel + optional
admin user/group roles from CLICKHOUSE_ADMIN_USER / CLICKHOUSE_ADMIN_GROUP),
stashes everything on app.state, and registers a post-login provisioning hook
so init_user_rights + derive_rights run once per real authentication.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from fastapi import FastAPI

from iris.auth.identity import User
from iris.auth.sessions import SessionStore
from iris.clickhouse.bootstrap import bootstrap_admin
from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.rights import derive_rights
from iris.clickhouse.users import init_user_rights

logger = logging.getLogger("iris.clickhouse")


def install(app: FastAPI) -> None:
    settings = ClickHouseSettings.from_env()
    client = build_client(settings)

    admin_user = os.environ.get("CLICKHOUSE_ADMIN_USER", "").strip() or None
    admin_group = os.environ.get("CLICKHOUSE_ADMIN_GROUP", "").strip() or None
    bootstrap_admin(client, admin_user=admin_user, admin_group=admin_group)

    scheme = "https" if settings.secure else "http"
    base_url = f"{scheme}://{settings.host}:{settings.port}"
    verify: bool | str = settings.ca_cert_path if settings.ca_cert_path else settings.verify
    http_client = httpx.AsyncClient(
        base_url=base_url,
        auth=(settings.user, settings.password),
        verify=verify,
        timeout=httpx.Timeout(30.0),
    )

    app.state.clickhouse_client = client
    app.state.clickhouse_settings = settings
    app.state.clickhouse_http_client = http_client

    async def _close_http() -> None:
        await http_client.aclose()

    app.state.clickhouse_close_http = _close_http

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
            (
                "clickhouse: provisioned user=%s groups=%s "
                "rights=admin:%s creator:%s reader:%d writer:%d db_admin:%d"
            ),
            user.username,
            list(user.groups),
            rights.is_admin,
            rights.can_create_database,
            len(rights.db_reader),
            len(rights.db_writer),
            len(rights.db_admin),
        )

    if not hasattr(app.state, "post_login_hooks"):
        app.state.post_login_hooks = []
    app.state.post_login_hooks.append(_provision_on_login)
```

- [ ] **Step 2: Update `src/iris/auth/__init__.py`**

Drop the `bootstrap_admin` import and export. The new content:

```python
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
    "install",
]
```

- [ ] **Step 3: Update `src/iris/auth/config.py`**

Drop the `bootstrap_user` field from `AuthSettings`:

Find:

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
    bootstrap_user: str | None
    oidc: OIDCSettings | None
    ldap: LDAPSettings | None
    mock: MockSettings | None
```

Replace with (drop the `bootstrap_user` line):

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
    oidc: OIDCSettings | None
    ldap: LDAPSettings | None
    mock: MockSettings | None
```

In `from_env`, find the `bootstrap_user = ...` block and remove it. In the `cls(...)` call, remove the `bootstrap_user=bootstrap_user,` line.

- [ ] **Step 4: Update `src/iris/auth/routes.py`**

Find: `app.state.auth_bootstrap_user = settings.bootstrap_user`

Delete that line entirely.

- [ ] **Step 5: Delete `src/iris/auth/bootstrap.py`**

```bash
git rm src/iris/auth/bootstrap.py
```

- [ ] **Step 6: Update `tests/conftest.py`**

Drop:

```python
os.environ.setdefault("IRIS_BOOTSTRAP_USER", "alice")
```

The auth-only test app (built with `install_clickhouse=False`) doesn't run the CH bootstrap, so neither `CLICKHOUSE_ADMIN_USER` nor `IRIS_BOOTSTRAP_USER` matters at this level.

- [ ] **Step 7: Update `tests/clickhouse/conftest.py`**

The conftest currently sets `CLICKHOUSE_SERVICE_ADMIN_USER` and `CLICKHOUSE_SERVICE_ADMIN_ROLE` env vars in the `ch_settings` fixture. Find:

```python
    monkeypatch.setenv("CLICKHOUSE_SERVICE_ADMIN_USER", _SVC_USER)
    monkeypatch.setenv("CLICKHOUSE_SERVICE_ADMIN_ROLE", "service_admin_role")
```

Delete those two lines. The svc user grants set up at the top of the fixture (which include `CURRENT GRANTS WITH GRANT OPTION`, `DROP ROLE`, etc.) stay — those are testcontainer-bootstrap concerns, not iris config.

- [ ] **Step 8: Defer commit** — continue to Task 7.

---

## Task 7: Drop `install` from `iris.clickhouse.__init__`; replace lazy imports in `iris.auth.identity` with top-level

**Files:**
- Modify: `src/iris/clickhouse/__init__.py`
- Modify: `src/iris/auth/identity.py`
- Modify: `src/iris/app.py` (if it imports `install` from `iris.clickhouse`, switch to submodule)

- [ ] **Step 1: Update `src/iris/clickhouse/__init__.py`**

Drop `install` from imports + `__all__`. Add `bootstrap_admin` (it's the new bootstrap entry point) and `GLOBAL_ADMIN_ROLE` (operator-facing constant).

Replace the content with:

```python
"""ClickHouse provisioning, audit helpers, and per-tier ops.

Public surface — see ``CLAUDE.md`` for usage. ``iris.clickhouse`` no longer
hosts FastAPI handle providers; the Session subclasses in
``iris.auth.identity`` carry the per-tier method surface, calling into the
``*_impl`` functions in ``iris.clickhouse.handle``.

The ``install`` function lives in ``iris.clickhouse.install`` but is *not*
re-exported from this package: callers (only ``iris.app:build_app``) do
``from iris.clickhouse.install import install``. Removing it from this
``__init__`` breaks an old module-load cycle where importing the package
triggered loading ``iris.auth.bootstrap`` via ``install``.
"""

from iris.clickhouse.audit import (
    role_grants,
    role_row_policies,
    table_row_policies,
    user_grants,
    user_role_memberships,
    user_row_policies,
)
from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE, bootstrap_admin
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
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
from iris.clickhouse.rights import derive_rights
from iris.clickhouse.users import init_user_rights

__all__ = [
    "ClickHouseSettings",
    "GLOBAL_ADMIN_ROLE",
    "TIER_DBADMIN",
    "TIER_DBREADER",
    "TIER_DBWRITER",
    "add_row_policy",
    "bootstrap_admin",
    "build_client",
    "create_tier_roles",
    "derive_rights",
    "drop_tier_roles",
    "grant_insert_update_to_table",
    "grant_select_to_database",
    "grant_tier_to_group",
    "grant_tier_to_user",
    "init_user_rights",
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

Note: `install` no longer in `__all__` — the public-surface test (`tests/clickhouse/test_clickhouse_identifiers.py`) needs the same removal. Update that test's `expected` set in this step too:

Find in `tests/clickhouse/test_clickhouse_identifiers.py`:

```python
    expected = {
        "ClickHouseSettings",
        "TIER_DBADMIN",
        ...
        "install",
        ...
    }
```

Drop `"install"` from the set, add `"GLOBAL_ADMIN_ROLE"` and `"bootstrap_admin"`.

- [ ] **Step 2: Verify `src/iris/app.py` already uses the submodule import**

Run: `grep -n 'iris.clickhouse' src/iris/app.py`

Expected output should already show:

```
from iris.clickhouse.install import install as install_clickhouse_fn
```

If `src/iris/app.py` instead does `from iris.clickhouse import install`, change it to `from iris.clickhouse.install import install`.

- [ ] **Step 3: Replace lazy imports in `src/iris/auth/identity.py` with top-level imports**

Open `src/iris/auth/identity.py`. At the top of the file, after the existing imports, add a single import of every `*_impl` function used by Session methods:

```python
from iris.clickhouse.handle import (
    add_admin_group_impl,
    add_admin_user_impl,
    add_row_policy_impl,
    create_database_impl,
    delete_database_impl,
    grant_insert_update_to_table_impl,
    grant_reader_impl,
    grant_reader_to_group_impl,
    grant_select_to_database_impl,
    grant_writer_impl,
    grant_writer_to_group_impl,
    list_admin_members_impl,
    list_grants_impl,
    list_row_policies_impl,
    query_as_service_impl,
    query_as_user_impl,
    remove_admin_group_impl,
    remove_admin_user_impl,
    reprovision_user_impl,
    revoke_reader_from_group_impl,
    revoke_reader_impl,
    revoke_row_policy_impl,
    revoke_writer_from_group_impl,
    revoke_writer_impl,
    role_grants_impl,
    role_row_policies_impl,
    table_row_policies_impl,
    user_grants_impl,
    user_role_memberships_impl,
    user_row_policies_impl,
)
```

Then in every method body that previously had `from iris.clickhouse.handle import <impl>`, delete the lazy import line. The `await <impl>(...)` call at the bottom of each method stays.

Pattern (apply to all ~24 methods across the four Session subclasses):

OLD:
```python
    async def grant_reader(self, username: str) -> None:
        from iris.clickhouse.handle import grant_reader_impl
        await grant_reader_impl(
            self.client, database=self.database, username=username
        )
```

NEW:
```python
    async def grant_reader(self, username: str) -> None:
        await grant_reader_impl(
            self.client, database=self.database, username=username
        )
```

- [ ] **Step 4: Run the full suite and typecheck**

Run:

```bash
uv run pytest --ignore=tests/auth/integration
uv run basedpyright --level error
uv run basedpyright --level warning
uv run ruff check
```

Expected: all green. If anything fails, the most likely causes are:

- A test still references `service_admin_user` / `service_admin_role` on `ClickHouseSettings` (constructor or attribute access). Drop the field reference.
- A test still passes `settings=...` to `add_row_policy` or `add_row_policy_impl`. Drop the kwarg.
- A test references `iris.auth.bootstrap` or `IRIS_BOOTSTRAP_USER`. Update or delete.
- The public-surface test in `test_clickhouse_identifiers.py` doesn't match the new `__all__`. Update the expected set.

For each failure, the fix is mechanical — match the message to the change in this plan and apply.

- [ ] **Step 5: Commit the breakage-window batch**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(clickhouse): drop SERVICE_ADMIN env vars; wildcards on iris_global_admin + DBADMIN

CLICKHOUSE_SERVICE_ADMIN_USER and CLICKHOUSE_SERVICE_ADMIN_ROLE are gone.
The connection identity is just CLICKHOUSE_USER (also the IMPERSONATE
grantee). The wildcard role concept moves to a managed sentinel
iris_global_admin: bootstrap creates it; admin user/group roles get it
granted; add_row_policy emits wildcards for it AND <database>_DBADMIN
on every table.

CLICKHOUSE_ADMIN_USER replaces IRIS_BOOTSTRAP_USER. CLICKHOUSE_ADMIN_GROUP
is new — bootstraps a per-IdP-group admin role.

Architectural cleanup: bootstrap_admin moves from iris.auth.bootstrap
to iris.clickhouse.bootstrap (the env vars are CLICKHOUSE_ now anyway).
iris.clickhouse.__init__ no longer re-exports install — the import
cycle that forced lazy imports in iris.auth.identity is gone, and
Session methods now use top-level imports of *_impl functions.
EOF
)"
```

---

## Task 8: Update CLAUDE.md + final verification

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the env-var section in CLAUDE.md**

Find the ClickHouse env-var block. It currently includes `CLICKHOUSE_SERVICE_ADMIN_USER` and `CLICKHOUSE_SERVICE_ADMIN_ROLE`, plus the prior `IRIS_BOOTSTRAP_USER` doc.

Replace those lines with:

```
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=iris_service          # CH login iris connects as; also the IMPERSONATE grantee
CLICKHOUSE_PASSWORD=replace-me
CLICKHOUSE_SECURE=true
CLICKHOUSE_VERIFY=true
# CLICKHOUSE_CA_CERT_PATH=/etc/ssl/certs/ca-bundle.crt

CLICKHOUSE_ADMIN_USER=                # IdP username of bootstrap admin (e.g. alice)
CLICKHOUSE_ADMIN_GROUP=               # IdP group name of bootstrap admins (e.g. iris_admin)
```

The corresponding entries in the env-var table near the top of the auth/clickhouse sections also drop `IRIS_BOOTSTRAP_USER`. Replace any reference to it with the two new vars.

- [ ] **Step 2: Update the bootstrap section in CLAUDE.md**

Find the section that explains the bootstrap (under Authentication > Authorization or under ClickHouse > Bootstrap). Replace its body to describe the new two-channel bootstrap:

> **Bootstrap.** At iris launch, after building the CH client, `bootstrap_admin` (in `iris.clickhouse.bootstrap`) always creates the `iris_global_admin` sentinel role. If `CLICKHOUSE_ADMIN_USER=alice` is set and no `_USER`-suffixed role currently holds admin, iris creates `alice_USER` with `GRANT ALL ON *.* WITH GRANT OPTION` plus `iris_global_admin` granted to it. If `CLICKHOUSE_ADMIN_GROUP=iris_admin` is set and no `_GRP`-suffixed role currently holds admin, iris creates `iris_admin_GRP` the same way. Both channels are independently idempotent.
>
> When alice (whose IdP username matches `CLICKHOUSE_ADMIN_USER`) logs in for the first time, the existing `init_user_rights` post-login hook creates her CH user and grants `alice_USER` — already an admin from bootstrap — to it. Same for bob whose IdP groups include `CLICKHOUSE_ADMIN_GROUP=iris_admin`: his CH user gets `iris_admin_GRP`, already an admin.

- [ ] **Step 3: Update the row-policies section in CLAUDE.md**

Find the explanation of the wildcard service-admin role and replace it. The new content:

> **Row-policy wildcards.** Once any restrictive row policy exists on a table, ClickHouse default-denies users without a matching policy. iris's `add_row_policy(database, table, column, role, value)` therefore creates three policies per call: the restrictive one for the target role, plus two `USING 1` wildcards — one for `iris_global_admin` (so all global admins see all rows) and one for `<database>_DBADMIN` (so all per-database admins of that database see all rows). The wildcards have deterministic names and are idempotent; subsequent calls for the same table no-op via `CREATE ROW POLICY IF NOT EXISTS`. They persist after the last restrictive policy is revoked.

- [ ] **Step 4: Drop any reference to `iris.auth.bootstrap`**

Run: `grep -n 'iris\.auth\.bootstrap\|iris\.auth import bootstrap_admin\|IRIS_BOOTSTRAP_USER\|service_admin_user\|service_admin_role\|CLICKHOUSE_SERVICE_ADMIN' CLAUDE.md`

Expected: empty after the edits above. Fix any remaining stale references.

- [ ] **Step 5: Run the full verification**

Run:

```bash
uv run pytest
uv run basedpyright --level error
uv run basedpyright --level warning
uv run ruff check
```

Expected: all green, including the integration tier (`tests/auth/integration/`).

- [ ] **Step 6: Smoke-test via TestClient**

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

Expected output: `whoami OK: Alice`.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE): bootstrap rework + iris_global_admin sentinel"
```

---

## Self-Review

- **Spec coverage check:**
  - Env vars (drop `_SERVICE_*`, add `CLICKHOUSE_ADMIN_USER` + `CLICKHOUSE_ADMIN_GROUP`) — Tasks 4, 6.
  - `iris_global_admin` sentinel — Task 1 (created), Task 3 (wildcard target), Task 7 (exported).
  - Two-channel bootstrap behavior — Task 1 (impl), Task 6 (wired into install).
  - Wildcards on `iris_global_admin` AND `<database>_DBADMIN` per `add_row_policy` — Task 3.
  - File moves: `iris.auth.bootstrap` → `iris.clickhouse.bootstrap` — Tasks 1 (added), 6 (deleted).
  - Drop `install` from `iris.clickhouse.__init__` — Task 7.
  - Top-level imports in `iris.auth.identity` — Task 7.
  - Drop `service_admin_user` / `service_admin_role` from `ClickHouseSettings` — Task 4.
  - Drop `bootstrap_user` from `AuthSettings` — Task 6.
  - Migration runbook — covered by spec, no implementation task needed.

- **Placeholder scan:**
  - Task 3 Step 4 says "Read it first" then describes the pattern — concrete enough; the change is mechanical (drop `settings=...` kwargs, update one role name) and the engineer can match patterns from the test file's existing structure. Not a placeholder.
  - Task 4 Step 3 says "Drop every line that sets ... or asserts about ..." — the engineer reads the file and matches the env-var name. Concrete.
  - Task 7 Step 4 has explicit fix patterns for the three most likely failures. Engineers troubleshoot from those.
  - No "TBD" / "TODO" / "implement later" anywhere.

- **Type/method consistency:**
  - `bootstrap_admin(client, *, admin_user=None, admin_group=None)` — Task 1 defines, Task 6 calls it.
  - `GLOBAL_ADMIN_ROLE` constant — Task 1 defines, Task 3 imports + uses, Task 7 re-exports.
  - `add_row_policy(client, *, database, table, column, role, value)` (no `settings`) — Task 3 defines, Tasks 3+ callers updated.
  - `add_row_policy_impl(client, *, database, table, column, role, value)` (no `settings`) — Task 3 defines, Task 3 step 3 callers updated.
  - `tier_role_name(database, TIER_DBADMIN)` — Task 3 imports from `iris.clickhouse.grants`, returns `<database>_DBADMIN`.
  - `_has_admin_role_with_suffix(client, suffix)` — Task 1 defines (private to module).
  - `USER_ROLE_SUFFIX` / `GROUP_ROLE_SUFFIX` — used in Task 1 (`bootstrap_admin`), already defined in `iris.clickhouse.users`.
  - Field renames in `ClickHouseSettings`: drops `service_admin_user` and `service_admin_role`. All consumers updated in Tasks 3, 4.

- **Order check:** Tasks 1 + 8 commit cleanly. Tasks 3-7 form the breakage window. Plan flags this in the file-structure preamble and at each affected task. The single big commit at end of Task 7 closes the window.
