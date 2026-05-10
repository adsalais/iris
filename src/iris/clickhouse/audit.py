"""Audit helpers — read-only queries over ClickHouse's RBAC system tables."""

from __future__ import annotations

from typing import Any, cast

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


def list_all_users(client: Client) -> list[dict[str, Any]]:
    """All CH users with the role names granted to them.

    Returns ``[{"name": <username>, "roles": [<role_name>, ...]}]``. The
    ``roles`` list contains every role granted to the user — per-user
    roles (``<u>_USER``), group roles (``<g>_GRP``), and any directly
    granted ad-hoc roles. Iris consumers that want only group memberships
    must filter by the ``_GRP`` suffix themselves.
    """
    # Single query: left-join users to their role grants and aggregate role
    # names per user. Replaces the prior per-user query loop (N+1).
    rows = client.query(
        """
        SELECT u.name AS name,
               groupArray(rg.granted_role_name) AS roles
        FROM system.users AS u
        LEFT JOIN system.role_grants AS rg ON rg.user_name = u.name
        GROUP BY u.name
        ORDER BY u.name
        """
    )
    out: list[dict[str, Any]] = []
    for row in rows.named_results():
        # groupArray over a LEFT JOIN that didn't match yields [""] in CH
        # (NULL coerced through Array(String)); strip empty placeholders.
        raw_roles = cast(list[str], row["roles"])
        roles = [r for r in raw_roles if r]
        out.append({"name": cast(str, row["name"]), "roles": roles})
    return out


def list_all_databases(client: Client) -> list[dict[str, Any]]:
    """All databases with admin / writer / reader counts derived from
    ``system.role_grants`` against the per-database tier roles.

    Returns ``[{"name": <db>, "admin_count": int, "writer_count": int,
    "reader_count": int}]``.
    """
    from iris.clickhouse.grants import (
        TIER_DBADMIN,
        TIER_DBREADER,
        TIER_DBWRITER,
    )
    # Two queries instead of 3 × N: one for the database list, one that
    # aggregates grant rows server-side by role name. We merge the two
    # results in Python by splitting each tier-role name into
    # ``<db>_<tier>``. Round-trip count is O(1) regardless of database
    # count.
    db_rows = client.query("SELECT name FROM system.databases ORDER BY name")
    suffix_to_key: dict[str, str] = {
        f"_{TIER_DBADMIN}": "admin_count",
        f"_{TIER_DBWRITER}": "writer_count",
        f"_{TIER_DBREADER}": "reader_count",
    }
    grant_rows = client.query(
        """
        SELECT granted_role_name AS role, count() AS c
        FROM system.role_grants
        WHERE endsWith(granted_role_name, {a:String})
           OR endsWith(granted_role_name, {w:String})
           OR endsWith(granted_role_name, {r:String})
        GROUP BY granted_role_name
        """,
        parameters={
            "a": f"_{TIER_DBADMIN}",
            "w": f"_{TIER_DBWRITER}",
            "r": f"_{TIER_DBREADER}",
        },
    )
    counts_by_db: dict[str, dict[str, int]] = {}
    for row in grant_rows.named_results():
        role = cast(str, row["role"])
        for suffix, key in suffix_to_key.items():
            if role.endswith(suffix):
                db = role[: -len(suffix)]
                counts_by_db.setdefault(db, {})[key] = cast(int, row["c"])
                break
    out: list[dict[str, Any]] = []
    for row in db_rows.named_results():
        db = cast(str, row["name"])
        c = counts_by_db.get(db, {})
        out.append({
            "name": db,
            "admin_count": c.get("admin_count", 0),
            "writer_count": c.get("writer_count", 0),
            "reader_count": c.get("reader_count", 0),
        })
    return out


def list_all_row_policies(client: Client) -> list[dict[str, Any]]:
    """All rows from ``system.row_policies``, ordered by (database, table)."""
    rows = client.query(
        "SELECT * FROM system.row_policies ORDER BY database, table",
    )
    return list(rows.named_results())


def list_all_grants(client: Client) -> list[dict[str, Any]]:
    """All rows from ``system.grants``, ordered by (database, user_name, role_name)."""
    rows = client.query(
        "SELECT * FROM system.grants ORDER BY database, user_name, role_name",
    )
    return list(rows.named_results())
