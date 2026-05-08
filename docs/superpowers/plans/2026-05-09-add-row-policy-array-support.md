# add_row_policy Array(String) support — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `iris.clickhouse.policies.add_row_policy` auto-detects array columns via `system.columns` and emits `has(<col>, <val>)` instead of `<col> = <val>`. Scope: `Array(String)` plus `Nullable` and `FixedString(N)` wrappers.

**Architecture:** Add a private `_column_type(client, ...)` helper that reads `system.columns`, and a private `_build_policy_filter(col_q, col_type, value)` helper that decides between `=` and `has(...)` based on the type string. `add_row_policy` calls both before issuing `CREATE ROW POLICY`. Other element types raise `TypeError`.

**Tech Stack:** Python 3.13, clickhouse-connect (binary protocol for the schema lookup), httpx (for the end-to-end policy-enforcement test), pytest + testcontainers.

**Spec:** `docs/superpowers/specs/2026-05-09-add-row-policy-array-support-design.md`.

**Conventions you must respect:**
- DDL safety: external strings flow through `validate_identifier` + `quote_identifier`. The new helpers DO NOT take user-supplied identifiers — they take already-validated/quoted forms or are called from inside `add_row_policy` after its own validation has run.
- `uv run pytest`, `uv run ruff check`, `uv run basedpyright --level error`, `uv run basedpyright --level warning` — ALL must be clean.
- The project's `reportImplicitStringConcatenation` rule forbids adjacent f-string literals on consecutive lines; collapse such patterns into single f-strings if they fire.
- Repo uses `from __future__ import annotations` consistently — preserve at top of `policies.py`.
- Tests live under `tests/`. No `__init__.py` files. Test file basenames must be unique. The existing `tests/clickhouse/test_clickhouse_policies.py` is the right home for new tests.
- Commit on the current branch with the exact commit messages each task specifies.

---

## File map

| File | Change |
|---|---|
| `src/iris/clickhouse/policies.py` | Add `import re` and `from typing import cast`. Add `_FIXED_STRING_RE`, `_column_type`, `_build_policy_filter`. Replace the hardcoded USING clause in `add_row_policy` with a call through the helpers. Extend `add_row_policy`'s docstring. |
| `tests/clickhouse/test_clickhouse_policies.py` | Add a `_setup_typed_table` helper that takes a column type. Add tests covering: scalar regression, four array-acceptance paths (select_filter spot-checks), two error paths, one end-to-end policy-enforcement test that proves the filter actually filters. |

---

## Task 1 — Add `_column_type`, `_build_policy_filter`, and `_FIXED_STRING_RE`

Pure helper additions to `policies.py`. `add_row_policy` is **not** modified yet — Task 2 wires it. This task lands the helpers plus their direct tests so we can test in isolation.

**Files:**
- Modify: `src/iris/clickhouse/policies.py`
- Modify: `tests/clickhouse/test_clickhouse_policies.py`

- [ ] **Step 1: Write failing tests for `_build_policy_filter` (pure-Python)**

Append to `tests/clickhouse/test_clickhouse_policies.py`:

```python
def _import_helpers():
    from iris.clickhouse.policies import (
        _build_policy_filter,
        _column_type,
    )

    return _build_policy_filter, _column_type


# ---- _build_policy_filter (pure Python; no CH) ---------------------------


def test_build_policy_filter_scalar_string_uses_equals():
    build, _ = _import_helpers()
    assert build("`region`", "String", "EU") == "`region` = 'EU'"


def test_build_policy_filter_array_of_string_uses_has():
    build, _ = _import_helpers()
    assert build("`tags`", "Array(String)", "EU") == "has(`tags`, 'EU')"


def test_build_policy_filter_array_of_nullable_string_uses_has():
    build, _ = _import_helpers()
    assert (
        build("`tags`", "Array(Nullable(String))", "EU") == "has(`tags`, 'EU')"
    )


def test_build_policy_filter_array_of_fixed_string_uses_has():
    build, _ = _import_helpers()
    assert (
        build("`tags`", "Array(FixedString(8))", "eu      ")
        == "has(`tags`, 'eu      ')"
    )


def test_build_policy_filter_array_of_nullable_fixed_string_uses_has():
    build, _ = _import_helpers()
    assert (
        build("`tags`", "Array(Nullable(FixedString(8)))", "eu      ")
        == "has(`tags`, 'eu      ')"
    )


def test_build_policy_filter_array_of_int_raises():
    build, _ = _import_helpers()
    with pytest.raises(TypeError, match=r"Array\(Int32\)"):
        build("`nums`", "Array(Int32)", "5")


def test_build_policy_filter_array_of_datetime_raises():
    build, _ = _import_helpers()
    with pytest.raises(TypeError, match=r"Array\(DateTime\)"):
        build("`dts`", "Array(DateTime)", "2026-05-09 12:00:00")


def test_build_policy_filter_quotes_value_with_apostrophe():
    """quote_string already escapes; verify the propagation works through
    both = and has(...) branches."""
    build, _ = _import_helpers()
    assert build("`region`", "String", "O'Brien") == "`region` = 'O\\'Brien'"
    assert (
        build("`tags`", "Array(String)", "O'Brien")
        == "has(`tags`, 'O\\'Brien')"
    )
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/clickhouse/test_clickhouse_policies.py -v -k "build_policy_filter"`
Expected: every test fails with `ImportError: cannot import name '_build_policy_filter'`.

- [ ] **Step 3: Add the helpers + regex constant + imports**

In `src/iris/clickhouse/policies.py`, replace the import block (lines 1-14) with:

```python
"""Row-policy CRUD helpers."""

from __future__ import annotations

import re
from typing import cast

from clickhouse_connect.driver.client import Client

from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
from iris.clickhouse.grants import TIER_DBADMIN, tier_role_name
from iris.clickhouse.identifiers import (
    policy_name,
    quote_identifier,
    quote_string,
    validate_identifier,
)


_FIXED_STRING_RE = re.compile(r"^FixedString\(\d+\)$")
```

Then append (after `revoke_row_policy`, at the bottom of the file):

```python
def _column_type(
    client: Client, *, database: str, table: str, column: str
) -> str:
    """Return the CH type string of ``<database>.<table>.<column>``.

    Reads ``system.columns``; raises ``ValueError`` if the column does
    not exist on that table. Used by ``add_row_policy`` to decide
    between ``<col> = <val>`` and ``has(<col>, <val>)``.
    """
    rows = client.query(
        "SELECT type FROM system.columns "
        "WHERE database = {d:String} AND table = {t:String} AND name = {c:String}",
        parameters={"d": database, "t": table, "c": column},
    ).result_rows
    if not rows:
        raise ValueError(
            f"column {database}.{table}.{column} does not exist"
        )
    return cast(str, rows[0][0])


def _build_policy_filter(
    col_q: str, col_type: str, value: str
) -> str:
    """Build the row-policy USING clause for ``col_q`` of CH type ``col_type``.

    For scalar columns: ``<col_q> = <quoted value>``.
    For ``Array(String)`` and the ``Nullable`` / ``FixedString(N)``
    variants: ``has(<col_q>, <quoted value>)``.

    Raises ``TypeError`` for Array element types other than String /
    Nullable(String) / FixedString(N) / Nullable(FixedString(N)).

    ``col_q`` is the already-backtick-quoted identifier (validated by
    ``add_row_policy``'s caller path); ``value`` is quoted into a SQL
    string literal here via ``quote_string`` (regardless of branch,
    since both branches need a quoted literal).
    """
    if col_type.startswith("Array(") and col_type.endswith(")"):
        inner = col_type[len("Array(") : -1].strip()
        if inner.startswith("Nullable(") and inner.endswith(")"):
            inner = inner[len("Nullable(") : -1].strip()
        if inner != "String" and not _FIXED_STRING_RE.match(inner):
            raise TypeError(
                f"add_row_policy supports Array(String) variants only; "
                f"got {col_type}. Extend add_row_policy or pass non-array "
                f"columns directly."
            )
        return f"has({col_q}, {quote_string(value)})"
    return f"{col_q} = {quote_string(value)}"
```

- [ ] **Step 4: Run pure-Python tests to verify they pass**

Run: `uv run pytest tests/clickhouse/test_clickhouse_policies.py -v -k "build_policy_filter"`
Expected: all 8 build_policy_filter tests pass.

- [ ] **Step 5: Write failing tests for `_column_type` (CH-using)**

Append to `tests/clickhouse/test_clickhouse_policies.py`:

```python
# ---- _column_type (uses CH testcontainer) --------------------------------


def test_column_type_returns_string_for_string_column(
    ch_client, ch_settings, prefix
):
    _, column_type = _import_helpers()
    db = f"{prefix}_ct1"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE `{db}`.`t` (id UInt64, region String) "
        f"ENGINE = MergeTree ORDER BY id"
    )
    assert column_type(ch_client, database=db, table="t", column="region") == "String"


def test_column_type_returns_array_string_for_array_column(
    ch_client, ch_settings, prefix
):
    _, column_type = _import_helpers()
    db = f"{prefix}_ct2"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE `{db}`.`t` (id UInt64, tags Array(String)) "
        f"ENGINE = MergeTree ORDER BY id"
    )
    assert (
        column_type(ch_client, database=db, table="t", column="tags")
        == "Array(String)"
    )


def test_column_type_returns_nullable_array_for_nullable_array_column(
    ch_client, ch_settings, prefix
):
    _, column_type = _import_helpers()
    db = f"{prefix}_ct3"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE `{db}`.`t` (id UInt64, tags Array(Nullable(String))) "
        f"ENGINE = MergeTree ORDER BY id"
    )
    assert (
        column_type(ch_client, database=db, table="t", column="tags")
        == "Array(Nullable(String))"
    )


def test_column_type_raises_for_unknown_column(ch_client, ch_settings, prefix):
    _, column_type = _import_helpers()
    db = f"{prefix}_ct4"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE `{db}`.`t` (id UInt64) ENGINE = MergeTree ORDER BY id"
    )
    with pytest.raises(ValueError, match="does not exist"):
        column_type(ch_client, database=db, table="t", column="missing")


def test_column_type_raises_for_unknown_table(ch_client, ch_settings, prefix):
    _, column_type = _import_helpers()
    db = f"{prefix}_ct5"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    with pytest.raises(ValueError, match="does not exist"):
        column_type(ch_client, database=db, table="ghost", column="anything")
```

- [ ] **Step 6: Run all the new tests**

Run: `uv run pytest tests/clickhouse/test_clickhouse_policies.py -v -k "build_policy_filter or column_type"`
Expected: 13 tests pass (8 build_policy_filter + 5 column_type).

- [ ] **Step 7: Run the full pytest suite to confirm no regression**

Run: `uv run pytest -x`
Expected: all tests pass (was 370 + 13 new = 383).

- [ ] **Step 8: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add src/iris/clickhouse/policies.py tests/clickhouse/test_clickhouse_policies.py
git commit -m "feat(policies): add _column_type and _build_policy_filter helpers"
```

---

## Task 2 — Wire `add_row_policy` to use the helpers

Replace the hardcoded `<col> = <val>` clause with a call through `_column_type` + `_build_policy_filter`. Add `system.row_policies.select_filter` spot-checks for each variant.

**Files:**
- Modify: `src/iris/clickhouse/policies.py`
- Modify: `tests/clickhouse/test_clickhouse_policies.py`

- [ ] **Step 1: Add `_setup_typed_table` helper for varied column types**

In `tests/clickhouse/test_clickhouse_policies.py`, add this helper next to the existing `_setup_table` (don't replace it — the existing tests still use it):

```python
def _setup_typed_table(
    ch_client, db: str, table: str, role: str, column: str, column_type: str
) -> None:
    """Like _setup_table but the column name and type are caller-supplied,
    so each test can declare its own table shape (Array(String),
    Array(FixedString(8)), etc.)."""
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}` "
        f"(id UInt64, `{column}` {column_type}) "
        f"ENGINE = MergeTree ORDER BY id"
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    dba_role = tier_role_name(db, TIER_DBADMIN)
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{dba_role}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{GLOBAL_ADMIN_ROLE}`")
```

- [ ] **Step 2: Write the failing select_filter spot-check tests**

Append to `tests/clickhouse/test_clickhouse_policies.py`:

```python
# ---- add_row_policy: select_filter dispatch ------------------------------


def _read_policy_filter(ch_client, db, table, role, value) -> str:
    """Return the SELECT filter clause CH stored for the named policy."""
    expected_name = policy_name(db, table, role, value)
    rows = list(
        ch_client.query(
            """
            SELECT select_filter FROM system.row_policies
            WHERE database = {d:String} AND table = {t:String}
              AND short_name = {n:String}
            """,
            parameters={"d": db, "t": table, "n": expected_name},
        ).named_results()
    )
    assert len(rows) == 1, f"policy {expected_name} not found"
    return cast(str, rows[0]["select_filter"])


def test_add_row_policy_string_column_uses_equals(
    ch_client, ch_settings, prefix
):
    """Regression: scalar String column still uses ``<col> = <val>``."""
    db = f"{prefix}_eq"
    table = "t"
    role = f"{prefix}_role_eq"
    _setup_typed_table(ch_client, db, table, role, "region", "String")
    add_row_policy(
        ch_client,
        database=db, table=table, column="region", role=role, value="EU",
    )
    filt = _read_policy_filter(ch_client, db, table, role, "EU")
    assert "=" in filt
    assert "has(" not in filt
    assert "'EU'" in filt


def test_add_row_policy_array_string_uses_has(
    ch_client, ch_settings, prefix
):
    db = f"{prefix}_arr_s"
    table = "t"
    role = f"{prefix}_role_arr_s"
    _setup_typed_table(ch_client, db, table, role, "tags", "Array(String)")
    add_row_policy(
        ch_client,
        database=db, table=table, column="tags", role=role, value="EU",
    )
    filt = _read_policy_filter(ch_client, db, table, role, "EU")
    assert "has(" in filt
    assert "'EU'" in filt


def test_add_row_policy_nullable_array_string_uses_has(
    ch_client, ch_settings, prefix
):
    db = f"{prefix}_arr_ns"
    table = "t"
    role = f"{prefix}_role_arr_ns"
    _setup_typed_table(
        ch_client, db, table, role, "tags", "Array(Nullable(String))"
    )
    add_row_policy(
        ch_client,
        database=db, table=table, column="tags", role=role, value="EU",
    )
    filt = _read_policy_filter(ch_client, db, table, role, "EU")
    assert "has(" in filt


def test_add_row_policy_array_fixed_string_uses_has(
    ch_client, ch_settings, prefix
):
    db = f"{prefix}_arr_fs"
    table = "t"
    role = f"{prefix}_role_arr_fs"
    _setup_typed_table(
        ch_client, db, table, role, "tags", "Array(FixedString(8))"
    )
    add_row_policy(
        ch_client,
        database=db, table=table, column="tags", role=role,
        value="eu      ",  # FixedString(8): caller pads to 8 chars
    )
    filt = _read_policy_filter(ch_client, db, table, role, "eu      ")
    assert "has(" in filt


def test_add_row_policy_array_int_raises(ch_client, ch_settings, prefix):
    db = f"{prefix}_arr_i"
    table = "t"
    role = f"{prefix}_role_arr_i"
    _setup_typed_table(ch_client, db, table, role, "nums", "Array(Int32)")
    with pytest.raises(TypeError, match=r"Array\(Int32\)"):
        add_row_policy(
            ch_client,
            database=db, table=table, column="nums", role=role, value="5",
        )


def test_add_row_policy_unknown_column_raises(
    ch_client, ch_settings, prefix
):
    db = f"{prefix}_unk"
    table = "t"
    role = f"{prefix}_role_unk"
    _setup_typed_table(
        ch_client, db, table, role, "region", "String"
    )
    with pytest.raises(ValueError, match="does not exist"):
        add_row_policy(
            ch_client,
            database=db, table=table, column="missing", role=role, value="v",
        )
```

The `cast` and `policy_name` already exist as imports in this file (`from iris.clickhouse.identifiers import ... policy_name`). `cast` does NOT — add it to the imports at the top of the test file:

Replace the existing top-of-file imports section with:

```python
"""Tests for add_row_policy and revoke_row_policy."""

from __future__ import annotations

from typing import cast

import pytest

from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
from iris.clickhouse.grants import TIER_DBADMIN, tier_role_name
from iris.clickhouse.identifiers import InvalidIdentifierError, policy_name
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
```

- [ ] **Step 3: Run new tests to verify failure**

Run: `uv run pytest tests/clickhouse/test_clickhouse_policies.py -v -k "string_column_uses_equals or array_string_uses_has or nullable_array_string or array_fixed_string or array_int_raises or unknown_column_raises"`
Expected: failures — `add_row_policy` still emits `=` for arrays (so `array_string_uses_has` fails because `select_filter` won't contain `has(`); `array_int_raises` fails because the existing code never inspects the type; `unknown_column_raises` fails because the existing code never checks the column.

- [ ] **Step 4: Wire `add_row_policy` to use the helpers**

In `src/iris/clickhouse/policies.py`, replace the body of `add_row_policy` (the part that creates the restrictive policy — the wildcards stay unchanged). The full new function:

```python
def add_row_policy(
    client: Client,
    *,
    database: str,
    table: str,
    column: str,
    role: str,
    value: str,
) -> None:
    """Create a restrictive row policy for ``<role>`` on ``<database>.<table>``.

    The USING clause depends on the column's CH type:

    - Scalar columns (``String`` etc.): ``<column> = <value>``.
    - ``Array(String)`` and the ``Nullable`` / ``FixedString(N)`` variants:
      ``has(<column>, <value>)`` so a row matches when ``<value>`` is
      contained in the array.

    Other Array element types (``Array(Int32)``, ``Array(DateTime)``, etc.)
    raise ``TypeError`` — extend ``_build_policy_filter`` if you need them.
    A column that doesn't exist on ``<database>.<table>`` raises
    ``ValueError``.

    Also ensures two ``USING 1`` wildcard policies exist on the same table:

    - One for ``iris_global_admin`` (every global admin sees all rows).
    - One for ``<database>_DBADMIN`` (every per-database admin sees all rows).

    Names of the wildcard policies are deterministic so re-runs are
    idempotent via ``CREATE ROW POLICY IF NOT EXISTS``. The wildcards
    persist after the last restrictive policy is revoked — this matches
    the prior service-admin wildcard behavior.

    Note: ``FixedString(N)`` values must be right-padded to N bytes by
    the caller (CH stores them that way and ``has`` does not auto-pad).
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(column, kind="column")
    validate_identifier(role, kind="role")

    db_q = quote_identifier(database, kind="database")
    table_q = quote_identifier(table, kind="table")
    column_q = quote_identifier(column, kind="column")
    role_q = quote_identifier(role, kind="role")

    # 1. The restrictive policy the caller asked for. Inspect the column's
    # CH type so the USING clause is correct for both scalar and Array
    # columns.
    col_type = _column_type(
        client, database=database, table=table, column=column
    )
    clause = _build_policy_filter(column_q, col_type, value)
    name = policy_name(database, table, role, value)
    name_q = quote_identifier(name, kind="policy")
    client.command(
        " ".join((
            f"CREATE ROW POLICY IF NOT EXISTS {name_q} ON {db_q}.{table_q}",
            f"FOR SELECT USING {clause} TO {role_q}",
        ))
    )

    # 2. The iris_global_admin wildcard (deterministic name, idempotent).
    ga_name = f"{database}_{table}_{GLOBAL_ADMIN_ROLE}"
    ga_name_q = quote_identifier(ga_name, kind="policy")
    ga_role_q = quote_identifier(GLOBAL_ADMIN_ROLE, kind="role")
    client.command(
        " ".join((
            f"CREATE ROW POLICY IF NOT EXISTS {ga_name_q} ON {db_q}.{table_q}",
            f"FOR SELECT USING 1 TO {ga_role_q}",
        ))
    )

    # 3. The <database>_DBADMIN wildcard (deterministic name, idempotent).
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
```

The two wildcard `client.command(...)` blocks are unchanged. `revoke_row_policy` is unchanged.

Note: the existing `test_add_row_policy_validates_inputs` test in the same file calls `add_row_policy` with arguments where `database="bad-db"` etc. — these still raise `InvalidIdentifierError` from `validate_identifier`, BEFORE `_column_type` runs. That test still passes unchanged.

- [ ] **Step 5: Run new tests + the existing policy tests**

Run: `uv run pytest tests/clickhouse/test_clickhouse_policies.py -v`
Expected: every test in the file passes — the 6 pre-existing tests, the 13 from Task 1, plus the 6 new in this task (25 total). The number is not load-bearing; the gate is "no failures, no errors."

- [ ] **Step 6: Run the full pytest suite**

Run: `uv run pytest -x`
Expected: all tests pass.

- [ ] **Step 7: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/iris/clickhouse/policies.py tests/clickhouse/test_clickhouse_policies.py
git commit -m "fix(policies): add_row_policy emits has() for Array(String) columns"
```

---

## Task 3 — End-to-end semantic test: the policy actually filters

Verify that after `add_row_policy`, a non-admin user querying via `query_as_user` sees only rows where the value is in the array. This is the load-bearing test — it proves CH actually applies the `has(...)` clause as we intended.

**Files:**
- Modify: `tests/clickhouse/test_clickhouse_policies.py`

- [ ] **Step 1: Write the failing end-to-end test**

Append to `tests/clickhouse/test_clickhouse_policies.py`:

```python
# ---- end-to-end policy enforcement (Array(String) + query_as_user) -------


def test_add_row_policy_array_string_filter_works_end_to_end(
    ch_client, ch_settings, prefix
):
    """Wire up the full row-policy enforcement path:

    1. Build a table ``(id UInt64, tags Array(String))``.
    2. Insert two rows; only row id=1 has 'EU' in its tags.
    3. Provision a CH user via ``init_user_rights`` (creates the user,
       its per-user role, and the IMPERSONATE grant the connecting
       service identity needs to ``EXECUTE AS`` it).
    4. Grant the policy's role to the user's per-user role, and grant
       SELECT on the table to that role.
    5. Run ``add_row_policy(... value='EU')`` — emits ``has(tags, 'EU')``.
    6. Query ``SELECT id ORDER BY id`` as the user via ``query_as_user``.
    7. Assert exactly row id=1 comes back.
    """
    import asyncio
    import httpx

    from iris.clickhouse.queries import query_as_user
    from iris.clickhouse.users import USER_ROLE_SUFFIX, init_user_rights

    db = f"{prefix}_e2e"
    table = "t"
    role = f"{prefix}_role_e2e"
    test_user = f"{prefix}_user_e2e"

    # 1+2. Table + two rows, one with EU and one without.
    _setup_typed_table(ch_client, db, table, role, "tags", "Array(String)")
    ch_client.command(
        f"INSERT INTO `{db}`.`{table}` VALUES "
        f"(1, ['EU','UK']), (2, ['US','CA'])"
    )

    # 3. CH user + per-user role + IMPERSONATE grant for iris_svc.
    init_user_rights(
        ch_client, username=test_user, groups=[], settings=ch_settings,
    )

    # 4. Make the user inherit `role` and have SELECT on the table.
    user_role = f"{test_user}{USER_ROLE_SUFFIX}"
    ch_client.command(f"GRANT `{role}` TO `{user_role}`")
    ch_client.command(f"GRANT SELECT ON `{db}`.`{table}` TO `{role}`")

    # 5. Add the policy. has(tags, 'EU') should land in select_filter.
    add_row_policy(
        ch_client,
        database=db, table=table, column="tags", role=role, value="EU",
    )

    # 6+7. Query as the test user; only row 1 is allowed by the policy.
    base_url = f"http://{ch_settings.host}:{ch_settings.port}"

    async def _run() -> list[dict[str, object]]:
        async with httpx.AsyncClient(
            base_url=base_url,
            auth=(ch_settings.user, ch_settings.password),
            timeout=httpx.Timeout(30.0),
        ) as http:
            return await query_as_user(
                http,
                username=test_user,
                sql=f"SELECT id FROM `{db}`.`{table}` ORDER BY id",
            )

    rows = asyncio.run(_run())
    assert rows == [{"id": 1}], f"policy did not filter as expected: {rows}"
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `uv run pytest tests/clickhouse/test_clickhouse_policies.py::test_add_row_policy_array_string_filter_works_end_to_end -v`
Expected: PASS. (Tasks 1 and 2 already shipped the `has(...)` clause; this test confirms the end-to-end semantic. If Tasks 1+2 had been wrong, this test would have failed by returning both rows or zero rows.)

If it fails — the policy emitted didn't match how you think CH applies row policies — escalate (BLOCKED). Don't paper over.

- [ ] **Step 3: Run the full pytest suite**

Run: `uv run pytest -x`
Expected: all tests pass.

- [ ] **Step 4: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean. If ruff complains about the in-test imports being below the top imports (E402), hoist them to the top of the file (a precedent set by earlier tasks in this codebase).

- [ ] **Step 5: Commit**

```bash
git add tests/clickhouse/test_clickhouse_policies.py
git commit -m "test(policies): end-to-end Array(String) row-policy enforcement"
```

---

## Final verification

After all three tasks land:

- [ ] **Run the entire suite once more from clean.**

```bash
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
uv run pytest
```

Expected: all clean, all green.

- [ ] **Skim `git log --oneline main..HEAD`** — should be 3 commits, each with a descriptive subject.

- [ ] **Sanity-check the dispatch:**

```bash
grep -nE "_build_policy_filter|_column_type|has\(" src/iris/clickhouse/policies.py
```

You should see both helpers defined and `_build_policy_filter` called from `add_row_policy`. The `has(` literal appears inside `_build_policy_filter`'s `f"has({col_q}, ...)"` branch.

---

## Self-review notes

This plan was checked against the spec section-by-section.

| Spec section | Tasks covering it |
|---|---|
| `_column_type` definition + behavior | Task 1 |
| `_FIXED_STRING_RE` constant | Task 1 |
| `_build_policy_filter` definition + dispatch | Task 1 |
| Inner-type peel: `Nullable` and `FixedString(N)` accepted | Task 1 (build_policy_filter tests) |
| Reject `Array(Int*)` / `Array(DateTime)` etc. with TypeError | Task 1 (unit) + Task 2 (integration via add_row_policy) |
| Reject unknown column with ValueError | Task 1 (column_type test) + Task 2 (integration via add_row_policy) |
| Wire `add_row_policy` to dispatch | Task 2 |
| `add_row_policy` docstring update | Task 2 |
| Scalar-`=` regression test | Task 2 |
| `select_filter` spot-check tests for the four accepted variants | Task 2 |
| End-to-end policy enforcement against CH | Task 3 |
| `revoke_row_policy` unchanged | Implicit — neither task modifies it; existing tests pass |
| Wildcard `USING 1` policies unchanged | Implicit — Task 2's wired function preserves the wildcard `client.command(...)` blocks verbatim |

No placeholders. Every code block is complete. Names referenced consistently across tasks: `_column_type`, `_build_policy_filter`, `_FIXED_STRING_RE`, `_setup_typed_table`, `_read_policy_filter`.

The end-to-end test in Task 3 relies on `init_user_rights` for the user-provisioning chain (matches the iris production pattern). It uses `query_as_user` from `iris.clickhouse.queries`, which already exists and was hardened by the typed-marshaller work earlier.
