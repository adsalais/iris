"""Tests for ClickHouseDatabaseCreatorHandle against the CH testcontainer.

Verifies that ``create_database`` provisions the database, the three tier
roles, and grants DBADMIN to the calling user — the lifecycle spelled out
in the spec under "CH-side state".
"""
import asyncio

from iris.clickhouse.handle import ClickHouseDatabaseCreatorHandle


def test_create_database_creates_db_and_tier_roles(ch_client, ch_settings, prefix):
    user = f"{prefix}_creator"
    db = f"{prefix}_owned"
    handle = ClickHouseDatabaseCreatorHandle(
        client=ch_client, settings=ch_settings, username=user
    )
    asyncio.run(handle.create_database(db))

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
        """
        SELECT granted_role_name FROM system.role_grants
        WHERE role_name = {r:String}
        """,
        parameters={"r": user_role},
    ).result_rows
    assert any(g[0] == f"{db}_DBADMIN" for g in granted)


def test_create_database_idempotent(ch_client, ch_settings, prefix):
    user = f"{prefix}_creator2"
    db = f"{prefix}_idemp"
    handle = ClickHouseDatabaseCreatorHandle(
        client=ch_client, settings=ch_settings, username=user
    )
    asyncio.run(handle.create_database(db))
    asyncio.run(handle.create_database(db))  # must not raise
