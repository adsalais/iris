# add_row_dict_policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a row-policy primitive that emits `has(dictGet('<dict>', '<attr>', <auth_id>), '<value>')` so iris can attach dict-keyed access policies to user databases. Mirrors the existing scalar `add_row_policy` (validation, wildcards, idempotency).

**Architecture:** Plain helper alongside `add_row_policy` in `iris.clickhouse.policies`. Identifier helpers (`dict_policy_name`, `validate_dict_name`) added to `iris.clickhouse.identifiers`. Async wrappers on `DatabaseAdminSession`. No new abstractions.

**Tech Stack:** Python 3.13, clickhouse-connect, pytest, basedpyright, ruff. CH 26.3 testcontainer for the integration assertions.

---

## Spec-to-plan refinements

- **§7.2 integration test consolidates into the unit-test file.** The spec puts the end-to-end test at `tests/clickhouse/integration/test_dict_policy_filters.py`, but that folder is reserved for tests that need both Keycloak + CH (`iris_app` / `keycloak_http` fixtures). The dict-policy filtering scenario only needs CH (the `ch_client` testcontainer fixture from `tests/clickhouse/conftest.py`). Putting it in the same file as the unit tests (`tests/clickhouse/test_clickhouse_dict_policies.py`) reuses the existing fixture and keeps it in the regular dev `pytest` sweep instead of the heavy `integration/` suite. Total file count unchanged from the spec; one fewer directory.

## File map

### New files

| Path | Responsibility |
|---|---|
| `tests/clickhouse/test_dict_policy_name.py` | Unit tests for `dict_policy_name` + `validate_dict_name`. |
| `tests/clickhouse/test_clickhouse_dict_policies.py` | Unit tests for `add_row_dict_policy` / `revoke_row_dict_policy` + the end-to-end policy-filtering test (consolidates §7.2). |
| `tests/auth/test_database_admin_dict_policies.py` | Unit tests for the `DatabaseAdminSession` wrappers. |

### Modified files

| Path | Change |
|---|---|
| `src/iris/clickhouse/identifiers.py` | Add `dict_policy_name(...)` and `validate_dict_name(...)` next to `policy_name`. |
| `src/iris/clickhouse/policies.py` | Add `add_row_dict_policy` + `revoke_row_dict_policy`. |
| `src/iris/auth/views.py` | Add `add_row_dict_policy` + `revoke_row_dict_policy` async wrappers on `DatabaseAdminSession`. |
| `CLAUDE.md` | Add an "Operator follow-ups" subsection under "Conventions" listing the three §6 responsibilities + the open admin-UI follow-up. |

---

## Task 1 — `dict_policy_name` + `validate_dict_name` in `identifiers.py`

**Files:**
- Modify: `src/iris/clickhouse/identifiers.py`
- Create: `tests/clickhouse/test_dict_policy_name.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/clickhouse/test_dict_policy_name.py`:

```python
"""Unit tests for dict_policy_name + validate_dict_name."""
from __future__ import annotations

import pytest

from iris.clickhouse.identifiers import (
    InvalidIdentifierError,
    dict_policy_name,
    validate_dict_name,
)


def test_dict_policy_name_format():
    name = dict_policy_name(
        database="marketing", table="events", role="readers_GRP", value="EU",
        dictionary="iris_dicts.auth_map",
        authorisations="authorisations", auth_id="auth_id",
    )
    # <db>_<table>_<role>_<slug>_<16hex>
    assert name.startswith("marketing_events_readers_GRP_EU_")
    suffix = name.rsplit("_", 1)[-1]
    assert len(suffix) == 16
    assert all(c in "0123456789abcdef" for c in suffix)


def test_dict_policy_name_distinct_for_different_dictionaries():
    n1 = dict_policy_name(
        database="d", table="t", role="r", value="v",
        dictionary="dict1", authorisations="a", auth_id="ai",
    )
    n2 = dict_policy_name(
        database="d", table="t", role="r", value="v",
        dictionary="dict2", authorisations="a", auth_id="ai",
    )
    assert n1 != n2


def test_dict_policy_name_distinct_for_different_attrs():
    n1 = dict_policy_name(
        database="d", table="t", role="r", value="v",
        dictionary="dict", authorisations="attr1", auth_id="ai",
    )
    n2 = dict_policy_name(
        database="d", table="t", role="r", value="v",
        dictionary="dict", authorisations="attr2", auth_id="ai",
    )
    assert n1 != n2


def test_dict_policy_name_distinct_for_different_auth_ids():
    n1 = dict_policy_name(
        database="d", table="t", role="r", value="v",
        dictionary="dict", authorisations="a", auth_id="auth_id_1",
    )
    n2 = dict_policy_name(
        database="d", table="t", role="r", value="v",
        dictionary="dict", authorisations="a", auth_id="auth_id_2",
    )
    assert n1 != n2


def test_dict_policy_name_validates_db_table_role():
    # Same identifier-validation rules as policy_name.
    with pytest.raises(InvalidIdentifierError):
        dict_policy_name(
            database="d-bad", table="t", role="r", value="v",
            dictionary="dict", authorisations="a", auth_id="ai",
        )


def test_validate_dict_name_accepts_simple_name():
    assert validate_dict_name("auth_dict") == "auth_dict"


def test_validate_dict_name_accepts_db_dot_dict():
    assert validate_dict_name("iris_dicts.auth_dict") == "iris_dicts.auth_dict"


def test_validate_dict_name_rejects_more_than_one_dot():
    with pytest.raises(InvalidIdentifierError):
        validate_dict_name("a.b.c")


def test_validate_dict_name_rejects_garbage_segments():
    with pytest.raises(InvalidIdentifierError):
        validate_dict_name("good.bad-segment")
    with pytest.raises(InvalidIdentifierError):
        validate_dict_name("bad-segment.good")
    with pytest.raises(InvalidIdentifierError):
        validate_dict_name("")
    with pytest.raises(InvalidIdentifierError):
        validate_dict_name(".")
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/clickhouse/test_dict_policy_name.py -v
```
Expected: FAIL with `ImportError: cannot import name 'dict_policy_name' from 'iris.clickhouse.identifiers'` and similar for `validate_dict_name`.

- [ ] **Step 3: Implement in `src/iris/clickhouse/identifiers.py`**

Append after the existing `policy_name` function (around line 132):

```python
def validate_dict_name(name: str) -> str:
    """Validate ``<dict>`` or ``<db>.<dict>``. Return ``name`` unchanged on success.

    Both halves of a dotted name go through ``validate_identifier`` (using
    ``kind="database"`` for the left half and ``kind="table"`` for the right).
    More than one dot, or any segment failing identifier validation, raises
    ``InvalidIdentifierError``.

    Used by ``add_row_dict_policy`` to gate ``dictionary`` parameters before
    they're emitted as a SQL string literal in the policy USING clause.
    """
    parts = name.split(".")
    if len(parts) == 1:
        validate_identifier(parts[0], kind="table")
    elif len(parts) == 2:
        validate_identifier(parts[0], kind="database")
        validate_identifier(parts[1], kind="table")
    else:
        raise InvalidIdentifierError(
            f"dictionary name must be '<dict>' or '<db>.<dict>'; got {name!r}"
        )
    return name


def dict_policy_name(
    database: str,
    table: str,
    role: str,
    value: str,
    dictionary: str,
    authorisations: str,
    auth_id: str,
) -> str:
    """Build a row-policy name for a dict-keyed policy.

    Same shape as ``policy_name``: ``<db>_<table>_<role>_<slug>_<16charhash>``.
    The 16-char SHA-256 prefix incorporates ``value | dictionary |
    authorisations | auth_id`` (NUL-separated) so two dict policies on the
    same ``(database, table, role, value)`` tuple but using different
    dictionaries / attributes / auth_id columns get distinct names.

    The slug is derived from ``value`` only (matching the scalar
    ``policy_name`` behavior) so the human-readable portion of the name
    stays recognisable.
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(role, kind="role")
    slug = _SLUG_RE.sub("_", value).strip("_") or "v"
    digest_input = f"{value}\0{dictionary}\0{authorisations}\0{auth_id}"
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]
    return f"{database}_{table}_{role}_{slug}_{digest}"
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/clickhouse/test_dict_policy_name.py -v
```
Expected: PASS (9 tests).

- [ ] **Step 5: Run gates**

```bash
uv run ruff check src/iris/clickhouse/identifiers.py tests/clickhouse/test_dict_policy_name.py
uv run basedpyright --level warning src/iris/clickhouse/identifiers.py tests/clickhouse/test_dict_policy_name.py
```
Expected: zero errors, zero warnings.

- [ ] **Step 6: Commit**

```bash
git add src/iris/clickhouse/identifiers.py tests/clickhouse/test_dict_policy_name.py
git commit -m "$(cat <<'EOF'
feat(clickhouse): dict_policy_name + validate_dict_name helpers

dict_policy_name mirrors policy_name's <db>_<table>_<role>_<slug>_<16hex>
shape but the SHA-256 prefix incorporates the dict params (dictionary,
authorisations, auth_id) so two dict policies on the same
(db, table, role, value) tuple but referencing different dicts get
distinct names. The slug stays derived from value only — keeps the
human-readable portion recognisable.

validate_dict_name accepts '<dict>' or '<db>.<dict>'; both halves go
through validate_identifier. Reserved suffixes are NOT enforced (dict
names are external operator-managed identifiers, not iris-synthesized
roles). Rejects more than one dot.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2 — `add_row_dict_policy` + `revoke_row_dict_policy` in `policies.py`

**Files:**
- Modify: `src/iris/clickhouse/policies.py`
- Create: `tests/clickhouse/test_clickhouse_dict_policies.py`

- [ ] **Step 1: Write the failing unit tests + the consolidated end-to-end test**

Create `tests/clickhouse/test_clickhouse_dict_policies.py`:

```python
"""Tests for add_row_dict_policy + revoke_row_dict_policy.

Includes both the unit tests (assert on system.row_policies state) and the
end-to-end test (assert that a real CH user with the role sees the right
filtered rows) — both use the same ch_client testcontainer fixture from
tests/clickhouse/conftest.py.
"""
from __future__ import annotations

import pytest

from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
from iris.clickhouse.grants import TIER_DBADMIN, tier_role_name
from iris.clickhouse.identifiers import (
    InvalidIdentifierError,
    dict_policy_name,
)
from iris.clickhouse.policies import (
    add_row_dict_policy,
    add_row_policy,
    revoke_row_dict_policy,
)


def _setup_protected_table(ch_client, db, table, role, auth_id_col="auth_id"):
    """Create a database, a protected table with id + region + auth_id columns,
    the role to gate, and the iris-synthesized roles the wildcards target."""
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        " ".join((
            f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}`",
            f"(id UInt64, region String, `{auth_id_col}` String)",
            "ENGINE = MergeTree ORDER BY id",
        ))
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    dba_role = tier_role_name(db, TIER_DBADMIN)
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{dba_role}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{GLOBAL_ADMIN_ROLE}`")


def _setup_dict(ch_client, dict_db, dict_name):
    """Create a dict source table + dict in `dict_db`. Caller fills the table
    and reloads the dict separately."""
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{dict_db}`")
    ch_client.command(
        " ".join((
            f"CREATE TABLE IF NOT EXISTS `{dict_db}`.`{dict_name}_src`",
            "(`key` String, `authorisations` Array(String))",
            "ENGINE = MergeTree ORDER BY `key`",
        ))
    )
    ch_client.command(
        " ".join((
            f"CREATE DICTIONARY IF NOT EXISTS `{dict_db}`.`{dict_name}`",
            "(`key` String, `authorisations` Array(String))",
            "PRIMARY KEY `key`",
            f"SOURCE(CLICKHOUSE(DB '{dict_db}' TABLE '{dict_name}_src'))",
            "LAYOUT(COMPLEX_KEY_HASHED())",
            "LIFETIME(MIN 0 MAX 0)",  # No caching — tests need fresh reads.
        ))
    )


def test_add_row_dict_policy_creates_named_policy_and_two_wildcards(
    ch_client, ch_settings, prefix
):
    db = f"{prefix}_dpol"
    table = "t"
    role = f"{prefix}_reader_dpol"
    dict_db = f"{prefix}_dicts"
    dict_name = "auth_map"
    _setup_protected_table(ch_client, db, table, role)
    _setup_dict(ch_client, dict_db, dict_name)

    add_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )

    expected_name = dict_policy_name(
        db, table, role, "public", f"{dict_db}.{dict_name}",
        "authorisations", "auth_id",
    )
    expected_global_admin_wildcard = f"{db}_{table}_{GLOBAL_ADMIN_ROLE}"
    expected_dbadmin_wildcard = f"{db}_{table}_{tier_role_name(db, TIER_DBADMIN)}"

    rows = list(
        ch_client.query(
            "SELECT short_name FROM system.row_policies "
            + "WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": db, "t": table},
        ).named_results()
    )
    names = {r["short_name"] for r in rows}
    assert expected_name in names
    assert expected_global_admin_wildcard in names
    assert expected_dbadmin_wildcard in names


def test_add_row_dict_policy_is_idempotent(ch_client, ch_settings, prefix):
    db = f"{prefix}_dpol2"
    table = "t"
    role = f"{prefix}_reader_dpol2"
    dict_db = f"{prefix}_dicts2"
    dict_name = "auth_map"
    _setup_protected_table(ch_client, db, table, role)
    _setup_dict(ch_client, dict_db, dict_name)

    add_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="v",
    )
    add_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="v",
    )
    count = next(
        ch_client.query(
            "SELECT count() AS c FROM system.row_policies "
            + "WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": db, "t": table},
        ).named_results()
    )["c"]
    # 1 named + 2 wildcards
    assert count == 3


def test_add_row_dict_policy_validates_inputs(ch_client, ch_settings):
    common = dict(
        client=ch_client, table="t", auth_id="auth_id",
        dictionary="dict", authorisations="authorisations",
        role="r", value="v",
    )
    with pytest.raises(InvalidIdentifierError):
        add_row_dict_policy(**common, database="bad-db")
    with pytest.raises(InvalidIdentifierError):
        add_row_dict_policy(**{**common, "database": "d", "table": "bad-table"})
    with pytest.raises(InvalidIdentifierError):
        add_row_dict_policy(**{**common, "database": "d", "auth_id": "bad-col"})
    with pytest.raises(InvalidIdentifierError):
        add_row_dict_policy(**{**common, "database": "d", "role": "bad-role"})
    with pytest.raises(InvalidIdentifierError):
        add_row_dict_policy(
            **{**common, "database": "d", "authorisations": "bad-attr"}
        )
    with pytest.raises(InvalidIdentifierError):
        add_row_dict_policy(**{**common, "database": "d", "dictionary": "a.b.c"})


def test_add_row_dict_policy_wildcards_no_op_when_scalar_already_present(
    ch_client, ch_settings, prefix
):
    """Locks in §5.2 of the spec: when add_row_policy seeded the iris_global_admin
    + <db>_DBADMIN wildcards on a table, a subsequent add_row_dict_policy on the
    same table must NOT duplicate or replace them — IF NOT EXISTS makes the
    wildcard CREATEs no-ops."""
    db = f"{prefix}_dpol3"
    table = "t"
    role_scalar = f"{prefix}_scalar_reader"
    role_dict = f"{prefix}_dict_reader"
    dict_db = f"{prefix}_dicts3"
    dict_name = "auth_map"
    _setup_protected_table(ch_client, db, table, role_scalar)
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role_dict}`")
    _setup_dict(ch_client, dict_db, dict_name)

    # Scalar policy first — seeds the two wildcards.
    add_row_policy(
        ch_client,
        database=db, table=table,
        column="region", role=role_scalar, value="EU",
    )

    # Snapshot wildcard rows (UUID + select_filter + apply_to_list).
    def _wildcard_rows():
        return list(
            ch_client.query(
                """
                SELECT short_name, id, select_filter
                FROM system.row_policies
                WHERE database = {d:String} AND table = {t:String}
                  AND short_name IN (
                    {ga:String}, {dba:String}
                  )
                ORDER BY short_name
                """,
                parameters={
                    "d": db, "t": table,
                    "ga": f"{db}_{table}_{GLOBAL_ADMIN_ROLE}",
                    "dba": f"{db}_{table}_{tier_role_name(db, TIER_DBADMIN)}",
                },
            ).named_results()
        )

    before = _wildcard_rows()
    assert len(before) == 2  # both wildcards present

    # Dict policy on the same table — wildcards must stay exactly as they were.
    add_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role_dict, value="public",
    )

    after = _wildcard_rows()
    assert len(after) == 2  # still exactly two
    # Same UUIDs (i.e. same rows, not replaced) and same USING clauses.
    assert [(r["short_name"], r["id"], r["select_filter"]) for r in before] \
        == [(r["short_name"], r["id"], r["select_filter"]) for r in after]


def test_revoke_row_dict_policy_drops_named_policy(ch_client, ch_settings, prefix):
    db = f"{prefix}_drev"
    table = "t"
    role = f"{prefix}_reader_drev"
    dict_db = f"{prefix}_dicts_drev"
    dict_name = "auth_map"
    _setup_protected_table(ch_client, db, table, role)
    _setup_dict(ch_client, dict_db, dict_name)

    add_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )
    revoke_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )
    expected_name = dict_policy_name(
        db, table, role, "public", f"{dict_db}.{dict_name}",
        "authorisations", "auth_id",
    )
    rows = list(
        ch_client.query(
            "SELECT short_name FROM system.row_policies "
            + "WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": db, "t": table},
        ).named_results()
    )
    names = {r["short_name"] for r in rows}
    assert expected_name not in names


def test_revoke_row_dict_policy_does_not_drop_wildcards(
    ch_client, ch_settings, prefix
):
    db = f"{prefix}_drev2"
    table = "t"
    role = f"{prefix}_reader_drev2"
    dict_db = f"{prefix}_dicts_drev2"
    dict_name = "auth_map"
    _setup_protected_table(ch_client, db, table, role)
    _setup_dict(ch_client, dict_db, dict_name)

    add_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )
    revoke_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )
    rows = list(
        ch_client.query(
            "SELECT short_name FROM system.row_policies "
            + "WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": db, "t": table},
        ).named_results()
    )
    names = {r["short_name"] for r in rows}
    assert f"{db}_{table}_{GLOBAL_ADMIN_ROLE}" in names
    assert f"{db}_{table}_{tier_role_name(db, TIER_DBADMIN)}" in names


def test_revoke_row_dict_policy_is_idempotent(ch_client, ch_settings, prefix):
    db = f"{prefix}_drev3"
    table = "t"
    role = f"{prefix}_reader_drev3"
    dict_db = f"{prefix}_dicts_drev3"
    dict_name = "auth_map"
    _setup_protected_table(ch_client, db, table, role)
    _setup_dict(ch_client, dict_db, dict_name)

    add_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )
    revoke_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )
    revoke_row_dict_policy(  # second call is a no-op (DROP IF EXISTS)
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )


def test_dict_policy_filters_real_user_query_end_to_end(
    ch_client, ch_settings, prefix
):
    """End-to-end: dict-keyed policy actually filters rows for the user's role.

    Mirrors the brainstorm experiment: protected table with auth_id col, dict
    that maps auth_id values to lists of tags, three policies (one per role)
    each gating a different tag, three users in distinct roles. Asserts each
    user sees only the rows their tag authorises.
    """
    db = f"{prefix}_filt"
    table = "t"
    dict_db = f"{prefix}_filt_dicts"
    dict_name = "auth_map"
    role_pub = f"{prefix}_pub"
    role_eu = f"{prefix}_eu"
    role_secret = f"{prefix}_secret"
    user_pub = f"{prefix}_alice"
    user_eu = f"{prefix}_bob"
    user_secret = f"{prefix}_eve"

    _setup_protected_table(ch_client, db, table, role_pub)
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role_eu}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role_secret}`")
    _setup_dict(ch_client, dict_db, dict_name)

    # Seed the dict source table and reload the dict.
    ch_client.command(
        f"INSERT INTO `{dict_db}`.`{dict_name}_src` VALUES "
        + "('rec-1', ['public']), "
        + "('rec-2', ['internal', 'eu_team']), "
        + "('rec-3', ['eu_team']), "
        + "('rec-4', ['secret'])"
    )
    ch_client.command(f"SYSTEM RELOAD DICTIONARY `{dict_db}`.`{dict_name}`")

    # Seed protected table.
    ch_client.command(
        f"INSERT INTO `{db}`.`{table}` (id, region, auth_id) VALUES "
        + "(10, 'eu', 'rec-1'), (20, 'eu', 'rec-2'), "
        + "(30, 'eu', 'rec-3'), (40, 'eu', 'rec-4')"
    )

    # Three users, each in their distinct gating role.
    for user, role in (
        (user_pub, role_pub),
        (user_eu, role_eu),
        (user_secret, role_secret),
    ):
        ch_client.command(
            f"CREATE USER `{user}` IDENTIFIED WITH no_password "
            + f"DEFAULT ROLE `{role}`"
        )
        ch_client.command(f"GRANT SELECT ON `{db}`.`{table}` TO `{role}`")
        ch_client.command(
            f"GRANT dictGet ON `{dict_db}`.`{dict_name}` TO `{role}`"
        )

    # Three dict policies, one per role, each gating a different tag.
    for role, value in (
        (role_pub, "public"),
        (role_eu, "eu_team"),
        (role_secret, "secret"),
    ):
        add_row_dict_policy(
            ch_client,
            database=db, table=table, auth_id="auth_id",
            dictionary=f"{dict_db}.{dict_name}",
            authorisations="authorisations",
            role=role, value=value,
        )

    # Query as each user via session-impersonation:
    # SET ROLE on a separate connection per user.
    import clickhouse_connect

    def _ids_for(user: str) -> list[int]:
        u = clickhouse_connect.get_client(
            host=ch_settings.host, port=ch_settings.port,
            username=user, password="", secure=False, verify=False,
        )
        try:
            rows = u.query(
                f"SELECT id FROM `{db}`.`{table}` ORDER BY id"
            ).result_rows
            return [int(r[0]) for r in rows]
        finally:
            u.close()

    assert _ids_for(user_pub) == [10]            # only rec-1 has 'public'
    assert _ids_for(user_eu) == [20, 30]         # rec-2 + rec-3 have 'eu_team'
    assert _ids_for(user_secret) == [40]         # only rec-4 has 'secret'
```

- [ ] **Step 2: Run to verify they fail**

```bash
uv run pytest tests/clickhouse/test_clickhouse_dict_policies.py -v
```
Expected: All FAIL with `ImportError: cannot import name 'add_row_dict_policy' from 'iris.clickhouse.policies'`.

- [ ] **Step 3: Implement in `src/iris/clickhouse/policies.py`**

Append after the existing `revoke_row_policy` (around line 127):

```python
def add_row_dict_policy(
    client: Client,
    *,
    database: str,
    table: str,
    auth_id: str,
    dictionary: str,
    authorisations: str,
    role: str,
    value: str,
) -> None:
    """Create a restrictive row policy gated by an external CH dictionary.

    Builds USING ``has(dictGet('<dictionary>', '<authorisations>',
    <auth_id_q>), <value_quoted>)`` so a row is visible to ``<role>`` iff the
    dict's per-row array (looked up by the row's ``<auth_id>`` column value)
    contains ``<value>``.

    Also creates the same two wildcard policies as ``add_row_policy`` —
    ``iris_global_admin`` and ``<database>_DBADMIN`` get ``USING 1`` so
    admins continue to see every row. Names are deterministic and match
    the scalar version, so calling both helpers on the same table is a
    no-op for the wildcard CREATEs (``IF NOT EXISTS`` makes it idempotent).

    Operator responsibilities (NOT done by iris):

    1. Create the dict source table.
    2. Create the dictionary (``CREATE DICTIONARY ...``).
    3. ``GRANT dictGet ON <dictionary> TO <role>``. Without this grant, the
       per-row evaluation raises ``Code: 497`` server-side and the user
       sees zero rows from the policy's perspective. Iris does NOT issue
       this grant — see ``CLAUDE.md`` § Operator follow-ups.
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(auth_id, kind="column")
    validate_identifier(role, kind="role")
    validate_identifier(authorisations, kind="column")
    validate_dict_name(dictionary)

    db_q = quote_identifier(database, kind="database")
    table_q = quote_identifier(table, kind="table")
    auth_id_q = quote_identifier(auth_id, kind="column")
    role_q = quote_identifier(role, kind="role")

    dict_lit = quote_sql_literal(dictionary)
    auth_attr_lit = quote_sql_literal(authorisations)
    value_lit = quote_sql_literal(value)

    clause = (
        f"has(dictGet({dict_lit}, {auth_attr_lit}, {auth_id_q}), {value_lit})"
    )

    name = dict_policy_name(
        database, table, role, value, dictionary, authorisations, auth_id,
    )
    name_q = quote_identifier(name, kind="policy")
    client.command(
        " ".join((
            f"CREATE ROW POLICY IF NOT EXISTS {name_q} ON {db_q}.{table_q}",
            f"FOR SELECT USING {clause} TO {role_q}",
        ))
    )

    # 2. iris_global_admin wildcard (deterministic name, idempotent — same
    #    name as add_row_policy emits, so a table that already had a scalar
    #    policy gets a no-op here).
    ga_name = f"{database}_{table}_{GLOBAL_ADMIN_ROLE}"
    ga_name_q = quote_identifier(ga_name, kind="policy")
    ga_role_q = quote_identifier(GLOBAL_ADMIN_ROLE, kind="role")
    client.command(
        " ".join((
            f"CREATE ROW POLICY IF NOT EXISTS {ga_name_q} ON {db_q}.{table_q}",
            f"FOR SELECT USING 1 TO {ga_role_q}",
        ))
    )

    # 3. <database>_DBADMIN wildcard (same idempotency story).
    dba_role = tier_role_name(database, TIER_DBADMIN)
    dba_name = f"{database}_{table}_{dba_role}"
    dba_name_q = quote_identifier(dba_name, kind="policy")
    dba_role_q = quote_identifier(dba_role, kind="role")
    client.command(
        " ".join((
            f"CREATE ROW POLICY IF NOT EXISTS {dba_name_q} ON {db_q}.{table_q}",
            f"FOR SELECT USING 1 TO {dba_role_q}",
        ))
    )


def revoke_row_dict_policy(
    client: Client,
    *,
    database: str,
    table: str,
    auth_id: str,
    dictionary: str,
    authorisations: str,
    role: str,
    value: str,
) -> None:
    """Drop the named restrictive dict policy created by ``add_row_dict_policy``.

    Wildcards on ``iris_global_admin`` and ``<database>_DBADMIN`` are NOT
    dropped — same rule as ``revoke_row_policy``.
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(auth_id, kind="column")
    validate_identifier(role, kind="role")
    validate_identifier(authorisations, kind="column")
    validate_dict_name(dictionary)

    db_q = quote_identifier(database, kind="database")
    table_q = quote_identifier(table, kind="table")
    name_q = quote_identifier(
        dict_policy_name(
            database, table, role, value, dictionary, authorisations, auth_id,
        ),
        kind="policy",
    )
    client.command(f"DROP ROW POLICY IF EXISTS {name_q} ON {db_q}.{table_q}")
```

Update the import block at the top of `src/iris/clickhouse/policies.py` to include the new helpers:

```python
from iris.clickhouse.identifiers import (
    dict_policy_name,
    is_fixed_string_type,
    policy_name,
    quote_identifier,
    quote_sql_literal,
    validate_dict_name,
    validate_identifier,
)
```

- [ ] **Step 4: Run to verify the tests pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_dict_policies.py -v
```
Expected: PASS (8 tests including the end-to-end filter test).

- [ ] **Step 5: Run gates + the existing scalar policy tests (regression check)**

```bash
uv run ruff check src/iris/clickhouse/policies.py tests/clickhouse/test_clickhouse_dict_policies.py
uv run basedpyright --level warning src/iris/clickhouse/policies.py tests/clickhouse/test_clickhouse_dict_policies.py
uv run pytest tests/clickhouse/test_clickhouse_policies.py tests/clickhouse/test_clickhouse_dict_policies.py -v
```
Expected: zero ruff/pyright issues; all scalar + dict policy tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/iris/clickhouse/policies.py tests/clickhouse/test_clickhouse_dict_policies.py
git commit -m "$(cat <<'EOF'
feat(clickhouse): add_row_dict_policy + revoke_row_dict_policy

New row-policy primitive that builds USING has(dictGet('<dict>', '<attr>',
<auth_id>), '<value>') so a row is visible to <role> iff the dict's
per-row array (looked up by the row's auth_id column value) contains
<value>. Mirrors add_row_policy's wildcard preservation, identifier
validation, and idempotency.

Operator responsibilities (documented in the docstring + CLAUDE.md):
the dict source table, the dict itself, and GRANT dictGet ON <dict> TO
<role> are NOT created by iris — the helper assumes the operator wired
those up. A missing dictGet grant silently filters everything for the
affected role.

Tests cover: named policy + wildcard creation, idempotency on re-run,
input validation, the §5.2 wildcard-no-op-when-scalar-already-present
contract, revoke drops only the named policy (not wildcards), revoke
idempotency, and an end-to-end test with three users in three roles
that asserts each user sees only the rows their role's tag authorises.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3 — `DatabaseAdminSession` async wrappers

**Files:**
- Modify: `src/iris/auth/views.py`
- Create: `tests/auth/test_database_admin_dict_policies.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/auth/test_database_admin_dict_policies.py`:

```python
"""Tests for the DatabaseAdminSession.add_row_dict_policy / revoke wrappers."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock


def _session() -> "DatabaseAdminSession":  # type: ignore[name-defined]
    from iris.auth.identity import User
    from iris.auth.rights import EMPTY_CAPABILITIES
    from iris.auth.views import DatabaseAdminSession

    return DatabaseAdminSession(
        id="x", user=User("s", "u", "U", ()),
        created_at=datetime.now(UTC), expires_at=datetime.now(UTC),
        data={}, capabilities=EMPTY_CAPABILITIES,
        client=MagicMock(), http_client=MagicMock(), settings=MagicMock(),
        store=MagicMock(), database="marketing",
    )


def test_add_row_dict_policy_calls_policies_helper(monkeypatch):
    captured = {}
    def fake_add(client, *, database, table, auth_id, dictionary,
                 authorisations, role, value):  # noqa: ARG001
        captured["args"] = (
            database, table, auth_id, dictionary, authorisations, role, value,
        )
    monkeypatch.setattr(
        "iris.auth.views.policies.add_row_dict_policy", fake_add,
    )
    s = _session()
    asyncio.run(s.add_row_dict_policy(
        table="events", auth_id="auth_id",
        dictionary="iris_dicts.auth_map", authorisations="authorisations",
        role="readers_GRP", value="public",
    ))
    assert captured["args"] == (
        "marketing", "events", "auth_id",
        "iris_dicts.auth_map", "authorisations",
        "readers_GRP", "public",
    )


def test_revoke_row_dict_policy_calls_policies_helper(monkeypatch):
    captured = {}
    def fake_revoke(client, *, database, table, auth_id, dictionary,
                    authorisations, role, value):  # noqa: ARG001
        captured["args"] = (
            database, table, auth_id, dictionary, authorisations, role, value,
        )
    monkeypatch.setattr(
        "iris.auth.views.policies.revoke_row_dict_policy", fake_revoke,
    )
    s = _session()
    asyncio.run(s.revoke_row_dict_policy(
        table="events", auth_id="auth_id",
        dictionary="iris_dicts.auth_map", authorisations="authorisations",
        role="readers_GRP", value="public",
    ))
    assert captured["args"] == (
        "marketing", "events", "auth_id",
        "iris_dicts.auth_map", "authorisations",
        "readers_GRP", "public",
    )
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/auth/test_database_admin_dict_policies.py -v
```
Expected: FAIL with `AttributeError: 'DatabaseAdminSession' object has no attribute 'add_row_dict_policy'`.

- [ ] **Step 3: Implement in `src/iris/auth/views.py`**

Find the existing `revoke_row_policy` method on `DatabaseAdminSession` (around line 380) and append after it:

```python
    async def add_row_dict_policy(
        self,
        *,
        table: str,
        auth_id: str,
        dictionary: str,
        authorisations: str,
        role: str,
        value: str,
    ) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            policies.add_row_dict_policy, client,
            database=self.database, table=table, auth_id=auth_id,
            dictionary=dictionary, authorisations=authorisations,
            role=role, value=value,
        )

    async def revoke_row_dict_policy(
        self,
        *,
        table: str,
        auth_id: str,
        dictionary: str,
        authorisations: str,
        role: str,
        value: str,
    ) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            policies.revoke_row_dict_policy, client,
            database=self.database, table=table, auth_id=auth_id,
            dictionary=dictionary, authorisations=authorisations,
            role=role, value=value,
        )
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/auth/test_database_admin_dict_policies.py -v
```
Expected: PASS (2 tests).

- [ ] **Step 5: Run gates + full unit suite (regression check)**

```bash
uv run ruff check src/iris/auth/views.py tests/auth/test_database_admin_dict_policies.py
uv run basedpyright --level warning src/iris/auth/views.py tests/auth/test_database_admin_dict_policies.py
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
```
Expected: zero issues; all unit tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/iris/auth/views.py tests/auth/test_database_admin_dict_policies.py
git commit -m "$(cat <<'EOF'
feat(auth): DatabaseAdminSession.add_row_dict_policy + revoke wrapper

Two new async methods on DatabaseAdminSession that delegate to the new
iris.clickhouse.policies.{add,revoke}_row_dict_policy helpers, scoped to
self.database (existing wrapper convention). Same kwargs the underlying
helpers take, minus database.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4 — `CLAUDE.md` Operator follow-ups subsection

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Locate the insertion point**

```bash
grep -n "^## " CLAUDE.md
```

The "Operator follow-ups" subsection lands at the end of the existing **## Conventions** section (between the bullet list there and the next `## Architecture & Datastar integration` heading). If the file structure has shifted, place it at the end of the most-relevant operator-facing section instead.

- [ ] **Step 2: Add the subsection**

In `CLAUDE.md`, find the last bullet of the `## Conventions` section (the one starting `- **Tests don't mock the database**:`). After it, insert a blank line and then:

```markdown
### Operator follow-ups

These are NOT done by iris — call them out for operators wiring up new features:

- **Dict-keyed row policies (`add_row_dict_policy`)** require, BEFORE the policy is useful:
  1. The dict source table exists (any database; arbitrary schema as long as it has the key column and an `Array(String)` attribute column).
  2. The dictionary exists (`CREATE DICTIONARY ...`) with a layout (`COMPLEX_KEY_HASHED` for `String` keys) and a `LIFETIME` matching how often the underlying data changes.
  3. `GRANT dictGet ON <dictionary> TO <role>` for every role the policy is attached to. Without this grant, the per-row evaluation raises `Code: 497` server-side and the user sees zero rows from the policy's perspective (CH treats it as "policy did not match", not a hard error).
- **Open: surface missing-`dictGet` grants in the admin UI.** When the Authorization feature gains awareness of dict policies, the per-database admin view should warn when a role with a dict policy on a table lacks `dictGet` on the referenced dict. Until then, the operator runs `SELECT * FROM system.grants WHERE access_type = 'dictGet'` to verify.
```

- [ ] **Step 3: Verify gates still pass (no code change)**

```bash
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
uv run ruff check
uv run basedpyright --level warning
```
Expected: zero failures, zero issues.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs(claude-md): Operator follow-ups subsection — dict-policy responsibilities

Documents the three operator responsibilities for dict-keyed row policies
(dict source table creation, dict creation with appropriate LIFETIME,
GRANT dictGet to gating roles). Also captures the open follow-up:
surface missing-dictGet grants in the future admin UI.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Recap

4 tasks, 4 commits. End state:

- `iris.clickhouse.identifiers` exposes `dict_policy_name` + `validate_dict_name`.
- `iris.clickhouse.policies` exposes `add_row_dict_policy` + `revoke_row_dict_policy`, with the same wildcard preservation and idempotency as the scalar versions.
- `iris.auth.views.DatabaseAdminSession` has matching async wrappers.
- `CLAUDE.md` has an "Operator follow-ups" subsection capturing the three dict-policy responsibilities + the open admin-UI follow-up.
- 19 new tests across 3 files (8 unit + 1 end-to-end + 8 helper + 2 wrapper). Plus the existing scalar policy + auth tests still pass — verified by the regression sweep in Tasks 2 and 3.
