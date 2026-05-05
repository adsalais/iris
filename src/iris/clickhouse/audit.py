"""Audit helpers — read-only queries over ClickHouse's RBAC system tables."""

from __future__ import annotations

from typing import Any

from clickhouse_connect.driver.client import Client

from iris.clickhouse.identifiers import validate_identifier


def user_grants(client: Client, *, username: str) -> list[dict[str, Any]]:
    """All direct grants on the named user (does not include grants inherited via roles)."""
    validate_identifier(username, kind="username")
    return list(
        client.query(
            "SELECT * FROM system.grants WHERE user_name = {u:String}",
            parameters={"u": username},
        ).named_results()
    )


def role_grants(client: Client, *, role: str) -> list[dict[str, Any]]:
    """All grants attached to the named role."""
    validate_identifier(role, kind="role")
    return list(
        client.query(
            "SELECT * FROM system.grants WHERE role_name = {r:String}",
            parameters={"r": role},
        ).named_results()
    )


def user_role_memberships(client: Client, *, username: str) -> list[dict[str, Any]]:
    """All roles granted to the named user (per-user role + group roles)."""
    validate_identifier(username, kind="username")
    return list(
        client.query(
            "SELECT * FROM system.role_grants WHERE user_name = {u:String}",
            parameters={"u": username},
        ).named_results()
    )


def role_row_policies(client: Client, *, role: str) -> list[dict[str, Any]]:
    """All row policies that apply to the named role.

    ``system.row_policies.apply_to_list`` is an ``Array(String)`` containing the
    grantee role/user names; ``has(...)`` filters rows where ``role`` is present.
    """
    validate_identifier(role, kind="role")
    return list(
        client.query(
            "SELECT * FROM system.row_policies WHERE has(apply_to_list, {r:String})",
            parameters={"r": role},
        ).named_results()
    )


def user_row_policies(client: Client, *, username: str) -> list[dict[str, Any]]:
    """All row policies that apply to the named user.

    Joins ``system.row_policies`` with the user's role memberships so policies
    granted via group roles are included alongside any policies attached
    directly to the username.
    """
    validate_identifier(username, kind="username")
    return list(
        client.query(
            """
            SELECT rp.*
            FROM system.row_policies AS rp
            ARRAY JOIN apply_to_list AS grantee
            WHERE grantee = {u:String}
               OR grantee IN (
                   SELECT granted_role_name FROM system.role_grants
                   WHERE user_name = {u:String}
               )
            """,
            parameters={"u": username},
        ).named_results()
    )


def table_row_policies(
    client: Client, *, database: str, table: str
) -> list[dict[str, Any]]:
    """All row policies attached to the given table."""
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    return list(
        client.query(
            "SELECT * FROM system.row_policies WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": database, "t": table},
        ).named_results()
    )
