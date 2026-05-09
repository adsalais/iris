"""Read-side helpers for the Authorization feature.

Pure aggregators that take typed sessions and return template-ready
dicts. No ClickHouse access lives here — that's behind the typed
XxxSession methods. Consumers (the manage / admin_console intent
renderers) call the typed methods directly via the session passed
through the FastAPI dep chain.
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
    """Build the manage-page context for self.database."""
    members = await session.list_members()
    row_policies = await session.list_row_policies()
    audit = await session.list_grants()
    return {
        "members": members,
        "row_policies": row_policies,
        "audit": audit,
    }
