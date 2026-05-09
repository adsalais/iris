"""Tests for add_row_policy and revoke_row_policy."""

from __future__ import annotations

import asyncio
from typing import cast

import httpx
import pytest

from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
from iris.clickhouse.grants import TIER_DBADMIN, tier_role_name
from iris.clickhouse.identifiers import InvalidIdentifierError, policy_name
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
from iris.clickhouse.queries import query_as_user
from iris.clickhouse.users import USER_ROLE_SUFFIX, provision_user


def _setup_table(ch_client, db, table, role):
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        " ".join((
            f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}`",
            "(id UInt64, region String) ENGINE = MergeTree ORDER BY id",
        ))
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    # Tier-DBADMIN role must exist before add_row_policy creates a wildcard
    # that targets it.
    dba_role = tier_role_name(db, TIER_DBADMIN)
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{dba_role}`")
    # iris_global_admin must exist (created at iris launch via bootstrap_admin;
    # tests at this level haven't run that, so create explicitly).
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{GLOBAL_ADMIN_ROLE}`")


def _setup_typed_table(
    ch_client, db: str, table: str, role: str, column: str, column_type: str
) -> None:
    """Like _setup_table but the column name and type are caller-supplied,
    so each test can declare its own table shape (Array(String),
    Array(FixedString(8)), etc.)."""
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}` (id UInt64, `{column}` {column_type}) ENGINE = MergeTree ORDER BY id"
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    dba_role = tier_role_name(db, TIER_DBADMIN)
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{dba_role}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{GLOBAL_ADMIN_ROLE}`")


def test_add_row_policy_creates_named_policy_and_two_wildcards(
    ch_client, ch_settings, prefix
):
    db = f"{prefix}_pol"
    table = "t"
    role = f"{prefix}_writer_pol"
    _setup_table(ch_client, db, table, role)

    add_row_policy(
        ch_client,
        database=db,
        table=table,
        column="region",
        role=role,
        value="EU",
    )

    expected_name = policy_name(db, table, role, "EU")
    expected_global_admin_wildcard = f"{db}_{table}_{GLOBAL_ADMIN_ROLE}"
    expected_dbadmin_wildcard = f"{db}_{table}_{tier_role_name(db, TIER_DBADMIN)}"

    rows = list(
        ch_client.query(
            """
            SELECT short_name FROM system.row_policies
            WHERE database = {d:String} AND table = {t:String}
            """,
            parameters={"d": db, "t": table},
        ).named_results()
    )
    names = {r["short_name"] for r in rows}
    assert expected_name in names
    assert expected_global_admin_wildcard in names
    assert expected_dbadmin_wildcard in names


def test_add_row_policy_is_idempotent(ch_client, ch_settings, prefix):
    db = f"{prefix}_pol2"
    table = "t"
    role = f"{prefix}_writer_pol2"
    _setup_table(ch_client, db, table, role)

    add_row_policy(
        ch_client,
        database=db, table=table, column="region", role=role, value="EU",
    )
    add_row_policy(
        ch_client,
        database=db, table=table, column="region", role=role, value="EU",
    )

    n = list(
        ch_client.query(
            """
            SELECT count() AS n FROM system.row_policies
            WHERE database = {d:String} AND table = {t:String}
            """,
            parameters={"d": db, "t": table},
        ).named_results()
    )
    # exactly three policies: restrictive + two wildcards.
    assert n == [{"n": 3}]


def test_add_row_policy_validates_inputs(ch_client, ch_settings):
    with pytest.raises(InvalidIdentifierError):
        add_row_policy(
            ch_client,
            database="bad-db", table="t", column="c", role="r", value="v",
        )
    with pytest.raises(InvalidIdentifierError):
        add_row_policy(
            ch_client,
            database="db", table="bad table", column="c", role="r", value="v",
        )
    with pytest.raises(InvalidIdentifierError):
        add_row_policy(
            ch_client,
            database="db", table="t", column="bad column", role="r", value="v",
        )
    with pytest.raises(InvalidIdentifierError):
        add_row_policy(
            ch_client,
            database="db", table="t", column="c", role="bad role", value="v",
        )


def test_revoke_row_policy_drops_named_policy(ch_client, ch_settings, prefix):
    db = f"{prefix}_rev"
    table = "t"
    role = f"{prefix}_writer_rev"
    _setup_table(ch_client, db, table, role)

    add_row_policy(
        ch_client,
        database=db, table=table, column="region", role=role, value="EU",
    )
    revoke_row_policy(ch_client, database=db, table=table, role=role, value="EU")

    expected_name = policy_name(db, table, role, "EU")
    rows = list(
        ch_client.query(
            """
            SELECT short_name FROM system.row_policies
            WHERE database = {d:String} AND table = {t:String} AND short_name = {n:String}
            """,
            parameters={"d": db, "t": table, "n": expected_name},
        ).named_results()
    )
    assert rows == []


def test_revoke_row_policy_does_not_drop_wildcards(ch_client, ch_settings, prefix):
    db = f"{prefix}_rev2"
    table = "t"
    role = f"{prefix}_writer_rev2"
    _setup_table(ch_client, db, table, role)

    add_row_policy(
        ch_client,
        database=db, table=table, column="region", role=role, value="EU",
    )
    revoke_row_policy(ch_client, database=db, table=table, role=role, value="EU")

    global_admin_wildcard = f"{db}_{table}_{GLOBAL_ADMIN_ROLE}"
    dbadmin_wildcard = f"{db}_{table}_{tier_role_name(db, TIER_DBADMIN)}"
    rows = list(
        ch_client.query(
            """
            SELECT short_name FROM system.row_policies
            WHERE database = {d:String} AND table = {t:String}
              AND short_name IN ({a:String}, {b:String})
            """,
            parameters={
                "d": db, "t": table,
                "a": global_admin_wildcard,
                "b": dbadmin_wildcard,
            },
        ).named_results()
    )
    surviving = {r["short_name"] for r in rows}
    assert surviving == {global_admin_wildcard, dbadmin_wildcard}


def test_revoke_row_policy_is_idempotent(ch_client, ch_settings, prefix):
    db = f"{prefix}_rev3"
    table = "t"
    role = f"{prefix}_writer_rev3"
    _setup_table(ch_client, db, table, role)

    add_row_policy(
        ch_client,
        database=db, table=table, column="region", role=role, value="EU",
    )
    revoke_row_policy(ch_client, database=db, table=table, role=role, value="EU")
    revoke_row_policy(ch_client, database=db, table=table, role=role, value="EU")


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
    """quote_string uses SQL-standard double-single-quote escaping; verify
    the propagation works through both = and has(...) branches."""
    build, _ = _import_helpers()
    assert build("`region`", "String", "O'Brien") == "`region` = 'O''Brien'"
    assert (
        build("`tags`", "Array(String)", "O'Brien")
        == "has(`tags`, 'O''Brien')"
    )


# ---- _column_type (uses CH testcontainer) --------------------------------


def test_column_type_returns_string_for_string_column(
    ch_client, ch_settings, prefix
):
    _, column_type = _import_helpers()
    db = f"{prefix}_ct1"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE `{db}`.`t` (id UInt64, region String) ENGINE = MergeTree ORDER BY id"
    )
    assert column_type(ch_client, database=db, table="t", column="region") == "String"


def test_column_type_returns_array_string_for_array_column(
    ch_client, ch_settings, prefix
):
    _, column_type = _import_helpers()
    db = f"{prefix}_ct2"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE `{db}`.`t` (id UInt64, tags Array(String)) ENGINE = MergeTree ORDER BY id"
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
        f"CREATE TABLE `{db}`.`t` (id UInt64, tags Array(Nullable(String))) ENGINE = MergeTree ORDER BY id"
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


# ---- add_row_policy: select_filter dispatch ------------------------------


def _read_policy_filter(ch_client, db, table, role, value) -> str:
    """Return the SELECT filter clause CH stored for the named policy."""
    expected_name = policy_name(db, table, role, value)
    rows = list(
        ch_client.query(
            "SELECT select_filter FROM system.row_policies WHERE database = {d:String} AND table = {t:String} AND short_name = {n:String}",
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


# ---- end-to-end policy enforcement (Array(String) + query_as_user) -------


def test_add_row_policy_array_string_filter_works_end_to_end(
    ch_client, ch_settings, prefix
):
    """Wire up the full row-policy enforcement path:

    1. Build a table ``(id UInt64, tags Array(String))``.
    2. Insert two rows; only row id=1 has 'EU' in its tags.
    3. Provision a CH user via ``provision_user`` (creates the user,
       its per-user role, and the IMPERSONATE grant the connecting
       service identity needs to ``EXECUTE AS`` it).
    4. Grant the policy's role to the user's per-user role, and grant
       SELECT on the table to that role.
    5. Run ``add_row_policy(... value='EU')`` — emits ``has(tags, 'EU')``.
    6. Query ``SELECT id ORDER BY id`` as the user via ``query_as_user``.
    7. Assert exactly row id=1 comes back.
    """
    db = f"{prefix}_e2e"
    table = "t"
    role = f"{prefix}_role_e2e"
    test_user = f"{prefix}_user_e2e"

    # 1+2. Table + two rows, one with EU and one without.
    _setup_typed_table(ch_client, db, table, role, "tags", "Array(String)")
    ch_client.command(
        f"INSERT INTO `{db}`.`{table}` VALUES (1, ['EU','UK']), (2, ['US','CA'])"
    )

    # 3. CH user + per-user role + IMPERSONATE grant for iris_svc.
    provision_user(
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
