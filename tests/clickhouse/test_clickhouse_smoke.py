"""Phase-0 surface verification: every DDL/audit query the module relies on,
exercised end-to-end against the testcontainer."""

from __future__ import annotations

import uuid


def _u() -> str:
    return "smoke_" + uuid.uuid4().hex[:8]


def test_smoke_full_ddl_surface(ch_client) -> None:
    user = _u()
    role = f"{user}_USER"
    grp = f"{user}_GRP"
    db = f"{user}_db"
    table = "t"
    admin = "smoke_admin_" + uuid.uuid4().hex[:6]

    # Users / roles
    ch_client.command(f"CREATE USER IF NOT EXISTS `{user}` IDENTIFIED WITH no_password")
    ch_client.command(
        f"CREATE USER IF NOT EXISTS `{admin}` IDENTIFIED WITH no_password"
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{grp}`")
    ch_client.command(f"GRANT `{role}` TO `{user}`")
    ch_client.command(f"GRANT `{grp}` TO `{user}`")
    ch_client.command(f"REVOKE `{grp}` FROM `{user}`")

    # IMPERSONATE — the syntax our spec uses
    ch_client.command(f"GRANT IMPERSONATE ON `{user}` TO `{admin}`")

    # Database, table, row policies
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        (
            f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}` "
            "(id UInt64, region String) ENGINE = MergeTree ORDER BY id"
        )
    )
    ch_client.command(f"GRANT SELECT ON `{db}`.* TO `{role}`")
    ch_client.command(f"GRANT INSERT ON `{db}`.`{table}` TO `{role}`")
    ch_client.command(f"GRANT ALTER UPDATE ON `{db}`.`{table}` TO `{role}`")
    ch_client.command(
        (
            f"CREATE ROW POLICY IF NOT EXISTS `{user}_p1` ON `{db}`.`{table}` "
            f"FOR SELECT USING `region` = 'EU' TO `{role}`"
        )
    )
    ch_client.command(
        (
            f"CREATE ROW POLICY IF NOT EXISTS `{user}_wild` ON `{db}`.`{table}` "
            f"FOR SELECT USING 1 TO `{role}`"
        )
    )
    ch_client.command(
        f"DROP ROW POLICY IF EXISTS `{user}_p1` ON `{db}`.`{table}`"
    )

    # Every audit query the module uses
    rows = list(
        ch_client.query(
            "SELECT * FROM system.grants WHERE user_name = {u:String}",
            parameters={"u": admin},
        ).named_results()
    )
    assert any(r["access_type"] == "IMPERSONATE" for r in rows), rows

    rows = list(
        ch_client.query(
            "SELECT * FROM system.grants WHERE role_name = {r:String}",
            parameters={"r": role},
        ).named_results()
    )
    assert {row["access_type"] for row in rows} >= {"SELECT", "INSERT"}

    rows = list(
        ch_client.query(
            "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String}",
            parameters={"u": user},
        ).named_results()
    )
    granted = {r["granted_role_name"] for r in rows}
    assert role in granted
    assert grp not in granted

    rows = list(
        ch_client.query(
            (
                "SELECT name FROM system.row_policies "
                "WHERE database = {d:String} AND table = {t:String}"
            ),
            parameters={"d": db, "t": table},
        ).named_results()
    )
    names = {r["name"] for r in rows}
    # system.row_policies.name includes the full qualified form like
    # "policy_name ON database.table"
    assert any("_wild" in n for n in names), f"Expected _wild policy in {names}"
    assert not any("_p1" in n for n in names), f"Expected _p1 policy NOT in {names}"


def test_smoke_select_one_via_named_results(ch_client) -> None:
    rows = list(ch_client.query("SELECT 1 AS one").named_results())
    assert rows == [{"one": 1}]
