"""Tests for grant_select_to_database and grant_insert_update_to_table."""

from __future__ import annotations

from iris.clickhouse.grants import grant_select_to_database, grant_insert_update_to_table


def test_grant_select_to_database(ch_client, prefix):
    db = f"{prefix}_db"
    role = f"{prefix}_reader"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")

    grant_select_to_database(ch_client, database=db, role=role)

    rows = list(
        ch_client.query(
            "SELECT access_type FROM system.grants WHERE role_name = {r:String} " +
            "AND database = {d:String} AND access_type = 'SELECT'",
            parameters={"r": role, "d": db},
        ).named_results()
    )
    assert rows == [{"access_type": "SELECT"}]


def test_grant_select_to_database_is_idempotent(ch_client, prefix):
    db = f"{prefix}_db_i"
    role = f"{prefix}_reader_i"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")

    grant_select_to_database(ch_client, database=db, role=role)
    grant_select_to_database(ch_client, database=db, role=role)

    n = list(
        ch_client.query(
            "SELECT count() AS n FROM system.grants WHERE role_name = {r:String} " +
            "AND database = {d:String} AND access_type = 'SELECT'",
            parameters={"r": role, "d": db},
        ).named_results()
    )
    assert n == [{"n": 1}]


def test_grant_insert_update_to_table(ch_client, ch_settings, prefix):
    db = f"{prefix}_iu"
    table = "t"
    role = f"{prefix}_writer"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}` (id UInt64, region String) ENGINE = MergeTree ORDER BY id"
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")

    grant_insert_update_to_table(ch_client, database=db, table=table, role=role)

    rows = list(
        ch_client.query(
            "SELECT access_type FROM system.grants WHERE role_name = {r:String} AND database = {d:String} AND table = {t:String}",
            parameters={"r": role, "d": db, "t": table},
        ).named_results()
    )
    access_types = {row["access_type"] for row in rows}
    assert "INSERT" in access_types
    # ALTER UPDATE shows up either as 'ALTER UPDATE' or 'UPDATE' depending on CH version.
    assert any("UPDATE" in t for t in access_types), access_types


def test_grant_insert_update_to_table_is_idempotent(ch_client, ch_settings, prefix):
    db = f"{prefix}_iu2"
    table = "t"
    role = f"{prefix}_writer2"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}` (id UInt64) ENGINE = MergeTree ORDER BY id"
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")

    grant_insert_update_to_table(ch_client, database=db, table=table, role=role)
    grant_insert_update_to_table(ch_client, database=db, table=table, role=role)

    rows = list(
        ch_client.query(
            "SELECT access_type, count() AS n FROM system.grants WHERE role_name = {r:String} AND database = {d:String} AND table = {t:String} GROUP BY access_type",
            parameters={"r": role, "d": db, "t": table},
        ).named_results()
    )
    for row in rows:
        assert row["n"] == 1


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
