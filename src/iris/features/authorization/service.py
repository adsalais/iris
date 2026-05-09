"""Read-side helpers for the Authorization feature.

Pure functions that take a Capabilities (or other inputs) and return data
suitable for templates. No FastAPI imports here — keeps testing easy and
makes the layering explicit (routes → service → CH).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from iris.auth.rights import Capabilities

if TYPE_CHECKING:
    from iris.auth.views import DatabaseAdminSession


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
    """Build the manage-page context. Phase 4.1 stub — only admin members
    are populated; reader/writer members and row policies/audit fill in
    in subsequent tasks of Phase 4.
    """
    members = await session.list_admin_members()
    return {
        "members": {
            "admin": members,
            "reader": [],
            "writer": [],
        },
        "row_policies": [],
        "audit": [],
    }
