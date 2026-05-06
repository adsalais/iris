"""FastAPI dependencies that bridge iris.auth into iris.clickhouse.

These deps are the only place in iris.clickhouse that imports from iris.auth.
The handle classes in handle.py and the rest of the package stay independent
of auth.
"""
from __future__ import annotations

from typing import Final

from fastapi import Request

from iris.auth.authz.core import CurrentMapping
from iris.auth.deps import Session
from iris.auth.exceptions import AuthForbidden, AuthorizationMisconfigured
from iris.clickhouse.handle import ClickHouseAdminHandle, ClickHouseHandle

CLICKHOUSE_ADMIN_ROLE: Final = "clickhouse_admin"


async def get_clickhouse_handle(
    request: Request, session: Session
) -> ClickHouseHandle:
    """Return a user-handle bound to the session's username. Any logged-in user."""
    return ClickHouseHandle(
        client=request.app.state.clickhouse_client,
        http_client=request.app.state.clickhouse_http_client,
        username=session.user.username,
    )


async def require_clickhouse_admin(
    request: Request,
    session: Session,
    mapping: CurrentMapping,
) -> ClickHouseAdminHandle:
    """Return an admin-handle. 403 unless the user has ``clickhouse_admin``.
    500 if ``clickhouse_admin`` is not defined in the role mapping."""
    if CLICKHOUSE_ADMIN_ROLE not in mapping.roles:
        raise AuthorizationMisconfigured(CLICKHOUSE_ADMIN_ROLE)
    if CLICKHOUSE_ADMIN_ROLE not in session.roles:
        raise AuthForbidden(
            needed=(CLICKHOUSE_ADMIN_ROLE,),
            have=tuple(sorted(session.roles)),
        )
    return ClickHouseAdminHandle(
        client=request.app.state.clickhouse_client,
        http_client=request.app.state.clickhouse_http_client,
        username=session.user.username,
        settings=request.app.state.clickhouse_settings,
    )
