"""Read-side helpers for the Authorization feature.

Pure functions that take a Capabilities (or other inputs) and return data
suitable for templates. No FastAPI imports here — keeps testing easy and
makes the layering explicit (routes → service → CH).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from iris.auth.rights import Capabilities

if TYPE_CHECKING:
    from iris.auth.views import AdminSession, DatabaseAdminSession


def my_access_view(caps: Capabilities) -> dict[str, Any]:
    """Build the template context for the my_access render."""
    return {
        "reader_dbs": sorted(caps.db_reader),
        "writer_dbs": sorted(caps.db_writer),
        "admin_dbs": sorted(caps.db_admin),
        "can_create_database": caps.can_create_database,
        "is_admin": caps.is_admin,
    }


async def manage_view(session: "DatabaseAdminSession") -> dict[str, Any]:
    """Build the manage-page context."""
    members = await list_members(session)
    row_policies = await session.list_row_policies()
    audit = await session.list_grants()
    return {
        "members": members,
        "row_policies": row_policies,
        "audit": audit,
    }


async def list_members(
    session: "DatabaseAdminSession",
) -> dict[str, list[dict[str, str]]]:
    """Return {tier: [{kind, name}]} across reader/writer/admin tiers.

    Admins are read via the existing list_admin_members method on
    DatabaseAdminSession. Reader/writer tiers query system.role_grants for
    the tier role name (tier_role_name(database, tier)) directly.
    """
    from iris.clickhouse.grants import (
        TIER_DBREADER,
        TIER_DBWRITER,
        tier_role_name,
    )

    client = session._ch()[0]  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    db = session.database
    members: dict[str, list[dict[str, str]]] = {
        "admin": [],
        "reader": [],
        "writer": [],
    }

    members["admin"] = await session.list_admin_members()

    for tier_const, tier_key in (
        (TIER_DBREADER, "reader"),
        (TIER_DBWRITER, "writer"),
    ):
        role = tier_role_name(db, tier_const)

        def _q(role: str = role) -> list[dict[str, str]]:
            rows = client.query(
                "SELECT user_name, role_name FROM system.role_grants "
                + "WHERE granted_role_name = {r:String}",
                {"r": role},
            )
            out: list[dict[str, str]] = []
            for row in rows.named_results():
                u = row.get("user_name")
                r2 = row.get("role_name")
                if u:
                    out.append({"kind": "user", "name": u})
                elif r2:
                    out.append({"kind": "role", "name": r2})
            return out
        members[tier_key] = await asyncio.to_thread(_q)
    return members


async def list_all_users(session: "AdminSession") -> list[dict[str, Any]]:
    """All users with their CH role memberships."""
    client = session._ch()[0]  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    def _q() -> list[dict[str, Any]]:
        rows = client.query("SELECT name FROM system.users ORDER BY name")
        users: list[dict[str, Any]] = []
        for row in rows.named_results():
            uname = row["name"]
            role_rows = client.query(
                "SELECT granted_role_name FROM system.role_grants "
                + "WHERE user_name = {u:String}",
                {"u": uname},
            )
            roles = [r["granted_role_name"] for r in role_rows.named_results()]
            users.append({"name": uname, "groups": roles})
        return users
    return await asyncio.to_thread(_q)


async def list_all_databases(session: "AdminSession") -> list[dict[str, Any]]:
    """All databases with admin/writer/reader counts derived from system.role_grants."""
    from iris.clickhouse.grants import (
        TIER_DBADMIN,
        TIER_DBREADER,
        TIER_DBWRITER,
        tier_role_name,
    )

    client = session._ch()[0]  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    def _q() -> list[dict[str, Any]]:
        db_rows = client.query("SELECT name FROM system.databases ORDER BY name")
        out: list[dict[str, Any]] = []
        for row in db_rows.named_results():
            db = row["name"]
            counts: dict[str, Any] = {}
            for tier_const, key in (
                (TIER_DBADMIN, "admin_count"),
                (TIER_DBWRITER, "writer_count"),
                (TIER_DBREADER, "reader_count"),
            ):
                role = tier_role_name(db, tier_const)
                count_rows = client.query(
                    "SELECT count() AS c FROM system.role_grants "
                    + "WHERE granted_role_name = {r:String}",
                    {"r": role},
                )
                counts[key] = next(count_rows.named_results())["c"]
            out.append({"name": db, **counts})
        return out
    return await asyncio.to_thread(_q)


async def list_all_row_policies(session: "AdminSession") -> list[dict[str, Any]]:
    client = session._ch()[0]  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    def _q() -> list[dict[str, Any]]:
        rows = client.query(
            "SELECT * FROM system.row_policies ORDER BY database, table",
        )
        return list(rows.named_results())
    return await asyncio.to_thread(_q)


async def list_all_grants(session: "AdminSession") -> list[dict[str, Any]]:
    client = session._ch()[0]  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    def _q() -> list[dict[str, Any]]:
        rows = client.query(
            "SELECT * FROM system.grants ORDER BY database, user_name, role_name",
        )
        return list(rows.named_results())
    return await asyncio.to_thread(_q)
