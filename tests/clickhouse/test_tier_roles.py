from iris.clickhouse.grants import (
    TIER_DBADMIN,
    TIER_DBREADER,
    TIER_DBWRITER,
    create_tier_roles,
    drop_tier_roles,
    tier_role_name,
)


def test_tier_role_name_format():
    assert tier_role_name("finance", TIER_DBADMIN) == "finance_DBADMIN"
    assert tier_role_name("finance", TIER_DBWRITER) == "finance_DBWRITER"
    assert tier_role_name("finance", TIER_DBREADER) == "finance_DBREADER"


def test_create_tier_roles_creates_three_roles_and_grants(ch_client, prefix):
    db = f"{prefix}_finance"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    rows = ch_client.query(
        "SELECT name FROM system.roles WHERE name IN "
        "({a:String}, {w:String}, {r:String})",
        parameters={
            "a": f"{db}_DBADMIN",
            "w": f"{db}_DBWRITER",
            "r": f"{db}_DBREADER",
        },
    ).result_rows
    assert {r[0] for r in rows} == {f"{db}_DBADMIN", f"{db}_DBWRITER", f"{db}_DBREADER"}

    # admin role has at least one grant on db with grant_option=1
    admin_grants = ch_client.query(
        "SELECT access_type, grant_option, database FROM system.grants "
        "WHERE role_name = {r:String}",
        parameters={"r": f"{db}_DBADMIN"},
    ).result_rows
    assert any(g[1] == 1 and g[2] == db for g in admin_grants)

    # writer has SELECT, INSERT, ALTER UPDATE; reader has SELECT only
    w_types = {
        row[0]
        for row in ch_client.query(
            "SELECT access_type FROM system.grants WHERE role_name = {r:String}",
            parameters={"r": f"{db}_DBWRITER"},
        ).result_rows
    }
    assert w_types >= {"SELECT", "INSERT", "ALTER UPDATE"}
    r_types = {
        row[0]
        for row in ch_client.query(
            "SELECT access_type FROM system.grants WHERE role_name = {r:String}",
            parameters={"r": f"{db}_DBREADER"},
        ).result_rows
    }
    assert r_types == {"SELECT"}


def test_create_tier_roles_idempotent(ch_client, prefix):
    db = f"{prefix}_idemp"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    create_tier_roles(ch_client, database=db)


def test_drop_tier_roles_removes_them(ch_client, prefix):
    db = f"{prefix}_drop"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    drop_tier_roles(ch_client, database=db)
    rows = ch_client.query(
        "SELECT count() FROM system.roles WHERE name IN "
        "({a:String}, {w:String}, {r:String})",
        parameters={
            "a": f"{db}_DBADMIN",
            "w": f"{db}_DBWRITER",
            "r": f"{db}_DBREADER",
        },
    ).result_rows
    assert rows[0][0] == 0


def test_drop_tier_roles_idempotent(ch_client, prefix):
    db = f"{prefix}_drop_idemp"
    drop_tier_roles(ch_client, database=db)
