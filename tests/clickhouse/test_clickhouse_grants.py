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


def test_revoke_tier_from_user_does_not_create_role(ch_client, prefix):
    """Revoke must not pre-create the user-role for an unknown username:
    that would leak state for any value an attacker submits.
    """
    from iris.clickhouse.grants import (
        TIER_DBREADER,
        create_tier_roles,
        revoke_tier_from_user,
    )

    db = f"{prefix}_revoke_no_leak"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    nonexistent_user = f"{prefix}_ghost"

    revoke_tier_from_user(
        ch_client, database=db, tier=TIER_DBREADER, username=nonexistent_user
    )

    rows = ch_client.query(
        "SELECT count() FROM system.roles WHERE name = {n:String}",
        parameters={"n": f"{nonexistent_user}_USER"},
    ).result_rows
    assert rows[0][0] == 0, (
        f"revoke must not have created role {nonexistent_user}_USER"
    )


def test_revoke_tier_from_group_does_not_create_role(ch_client, prefix):
    from iris.clickhouse.grants import (
        TIER_DBREADER,
        create_tier_roles,
        revoke_tier_from_group,
    )

    db = f"{prefix}_revoke_no_leak_grp"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    nonexistent_group = f"{prefix}_ghost_grp"

    revoke_tier_from_group(
        ch_client, database=db, tier=TIER_DBREADER, group=nonexistent_group
    )

    rows = ch_client.query(
        "SELECT count() FROM system.roles WHERE name = {n:String}",
        parameters={"n": f"{nonexistent_group}_GRP"},
    ).result_rows
    assert rows[0][0] == 0


def test_list_tier_members_returns_three_tier_dict(ch_client, ch_settings, prefix):
    """Tier-role membership grouped by tier, returning {admin, reader, writer}."""
    from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
    from iris.clickhouse.grants import (
        TIER_DBADMIN,
        TIER_DBREADER,
        TIER_DBWRITER,
        create_tier_roles,
        grant_tier_to_group,
        grant_tier_to_user,
        list_tier_members,
    )

    db = f"{prefix}_listmem"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{GLOBAL_ADMIN_ROLE}`")
    create_tier_roles(ch_client, database=db)

    grant_tier_to_user(
        ch_client, database=db, tier=TIER_DBADMIN, username=f"{prefix}_alice",
    )
    grant_tier_to_user(
        ch_client, database=db, tier=TIER_DBWRITER, username=f"{prefix}_bob",
    )
    grant_tier_to_user(
        ch_client, database=db, tier=TIER_DBREADER, username=f"{prefix}_carol",
    )
    grant_tier_to_group(
        ch_client, database=db, tier=TIER_DBADMIN, group=f"{prefix}_group_x",
    )

    result = list_tier_members(ch_client, database=db)

    assert set(result.keys()) == {"admin", "reader", "writer"}
    admin_names = {(m["kind"], m["name"]) for m in result["admin"]}
    assert ("role", f"{prefix}_alice_USER") in admin_names
    assert ("role", f"{prefix}_group_x_GRP") in admin_names
    writer_names = {(m["kind"], m["name"]) for m in result["writer"]}
    assert ("role", f"{prefix}_bob_USER") in writer_names
    reader_names = {(m["kind"], m["name"]) for m in result["reader"]}
    assert ("role", f"{prefix}_carol_USER") in reader_names
