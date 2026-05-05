"""Tests for add_row_policy and revoke_row_policy."""

from __future__ import annotations

import pytest

from iris.clickhouse.identifiers import InvalidIdentifierError, policy_name
from iris.clickhouse.policies import add_row_policy


def _setup_table(ch_client, db, table, role):
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}` (id UInt64, region String) ENGINE = MergeTree ORDER BY id")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")


def test_add_row_policy_creates_named_policy_and_wildcard(ch_client, ch_settings, prefix):
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
        settings=ch_settings,
    )

    expected_name = policy_name(db, table, role, "EU")
    expected_wildcard = f"{db}_{table}_{ch_settings.service_admin_role}"

    rows = list(
        ch_client.query(
            "SELECT short_name FROM system.row_policies WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": db, "t": table},
        ).named_results()
    )
    names = {r["short_name"] for r in rows}
    assert expected_name in names
    assert expected_wildcard in names


def test_add_row_policy_is_idempotent(ch_client, ch_settings, prefix):
    db = f"{prefix}_pol2"
    table = "t"
    role = f"{prefix}_writer_pol2"
    _setup_table(ch_client, db, table, role)

    add_row_policy(
        ch_client,
        database=db, table=table, column="region", role=role, value="EU",
        settings=ch_settings,
    )
    add_row_policy(
        ch_client,
        database=db, table=table, column="region", role=role, value="EU",
        settings=ch_settings,
    )

    n = list(
        ch_client.query(
            "SELECT count() AS n FROM system.row_policies WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": db, "t": table},
        ).named_results()
    )
    # exactly two policies: the named one and the wildcard.
    assert n == [{"n": 2}]


def test_add_row_policy_validates_inputs(ch_client, ch_settings):
    with pytest.raises(InvalidIdentifierError):
        add_row_policy(
            ch_client,
            database="bad-db", table="t", column="c", role="r", value="v",
            settings=ch_settings,
        )
    with pytest.raises(InvalidIdentifierError):
        add_row_policy(
            ch_client,
            database="db", table="bad table", column="c", role="r", value="v",
            settings=ch_settings,
        )
    with pytest.raises(InvalidIdentifierError):
        add_row_policy(
            ch_client,
            database="db", table="t", column="bad column", role="r", value="v",
            settings=ch_settings,
        )
    with pytest.raises(InvalidIdentifierError):
        add_row_policy(
            ch_client,
            database="db", table="t", column="c", role="bad role", value="v",
            settings=ch_settings,
        )
