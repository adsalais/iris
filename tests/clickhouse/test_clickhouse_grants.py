"""Tests for grant_select_to_database and grant_insert_update_to_table."""

from __future__ import annotations

from iris.clickhouse.grants import grant_select_to_database


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
