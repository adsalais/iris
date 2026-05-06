"""FastAPI dependencies that bridge iris.auth into iris.clickhouse.

These deps are the only place in iris.clickhouse that imports from iris.auth.
The handle classes in handle.py and the rest of the package stay independent
of auth.
"""
from __future__ import annotations

from typing import Final

from fastapi import Depends, Request

from iris.auth.authz.core import CurrentMapping
from iris.auth.deps import require_session
from iris.auth.exceptions import AuthForbidden, AuthorizationMisconfigured
from iris.auth.session import SessionView
from iris.clickhouse.handle import (
    ClickHouseAdminHandle,
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
    ClickHouseHandle,
)
from iris.clickhouse.identifiers import validate_identifier

CLICKHOUSE_ADMIN_ROLE: Final = "clickhouse_admin"
CLICKHOUSE_DATABASE_CREATOR_ROLE: Final = "clickhouse_database_creator"


async def get_clickhouse_handle(
    request: Request, session: SessionView = Depends(require_session)
) -> ClickHouseHandle:
    """Return a user-handle bound to the session's username. Any logged-in user."""
    return ClickHouseHandle(
        client=request.app.state.clickhouse_client,
        http_client=request.app.state.clickhouse_http_client,
        username=session.user.username,
    )


async def require_clickhouse_admin(
    request: Request,
    mapping: CurrentMapping,
    session: SessionView = Depends(require_session),
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


async def require_clickhouse_database_creator(
    request: Request,
    mapping: CurrentMapping,
    session: SessionView = Depends(require_session),
) -> ClickHouseDatabaseCreatorHandle:
    """Return a database-creator handle. 403 unless the user has
    ``clickhouse_database_creator``. 500 if the role isn't defined."""
    if CLICKHOUSE_DATABASE_CREATOR_ROLE not in mapping.roles:
        raise AuthorizationMisconfigured(CLICKHOUSE_DATABASE_CREATOR_ROLE)
    if CLICKHOUSE_DATABASE_CREATOR_ROLE not in session.roles:
        raise AuthForbidden(
            needed=(CLICKHOUSE_DATABASE_CREATOR_ROLE,),
            have=tuple(sorted(session.roles)),
        )
    return ClickHouseDatabaseCreatorHandle(
        client=request.app.state.clickhouse_client,
        settings=request.app.state.clickhouse_settings,
        db_admin_store=request.app.state.clickhouse_database_admins,
        username=session.user.username,
    )


async def require_clickhouse_database_admin(
    request: Request,
    database: str,
    session: SessionView = Depends(require_session),
) -> ClickHouseDatabaseAdminHandle:
    """Return a per-database admin handle. ``database`` is bound from the
    calling route's path/query params by FastAPI. 403 unless the session is
    listed as admin of this database (or has clickhouse_admin)."""
    validate_identifier(database, kind="database")
    db_admin_store = request.app.state.clickhouse_database_admins
    is_admin = await db_admin_store.is_admin(
        database=database,
        username_lower=session.user.username.lower(),
        roles=session.roles,
    )
    if not is_admin:
        raise AuthForbidden(
            needed=(f"admin of database {database!r}",),
            have=tuple(sorted(session.roles)),
        )
    return ClickHouseDatabaseAdminHandle(
        client=request.app.state.clickhouse_client,
        http_client=request.app.state.clickhouse_http_client,
        settings=request.app.state.clickhouse_settings,
        db_admin_store=db_admin_store,
        authz_store=request.app.state.authz_store,
        database=database,
        username=session.user.username,
    )
