"""Tests for add_row_policy and revoke_row_policy."""

from __future__ import annotations

import pytest

from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
from iris.clickhouse.grants import TIER_DBADMIN, tier_role_name
from iris.clickhouse.identifiers import InvalidIdentifierError, policy_name
from iris.clickhouse.policies import add_row_policy, revoke_row_policy


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
