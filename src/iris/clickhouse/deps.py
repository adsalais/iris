"""FastAPI dependencies that bridge iris.auth into iris.clickhouse.

Each handle provider checks the session's ``rights`` (already derived at login
by the post-login hook in ``iris.clickhouse.install``). No SQLite mapping or
per-DB admin store is consulted; ClickHouse is the source of truth.

Tier checks (``rights.is_admin``, ``rights.has_admin(database)``, …) live in
the alias deps in ``iris.auth.deps``; the handle providers below are not
authorization gates per se — they construct the per-request handle. The alias
type annotation on each handle provider's ``session`` parameter is what raises
401/403 when the session lacks the required tier.
"""
from __future__ import annotations

from fastapi import Request

from iris.auth.deps import (
    Session,
    SessionAdmin,
    SessionDatabaseAdmin,
    SessionDatabaseCreator,
)
from iris.clickhouse.handle import (
    ClickHouseAdminHandle,
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
    ClickHouseHandle,
)
from iris.clickhouse.identifiers import validate_identifier


async def get_clickhouse_handle(
    request: Request, session: Session
) -> ClickHouseHandle:
    """Per-request user-impersonating handle. Any logged-in user."""
    return ClickHouseHandle(
        client=request.app.state.clickhouse_client,
        http_client=request.app.state.clickhouse_http_client,
        username=session.user.username,
    )


async def require_clickhouse_admin(
    request: Request, session: SessionAdmin
) -> ClickHouseAdminHandle:
    """Admin handle. The ``SessionAdmin`` alias on the parameter raises 403 for
    non-admin sessions before the body runs."""
    return ClickHouseAdminHandle(
        client=request.app.state.clickhouse_client,
        http_client=request.app.state.clickhouse_http_client,
        username=session.user.username,
        settings=request.app.state.clickhouse_settings,
    )


async def require_clickhouse_database_creator(
    request: Request, session: SessionDatabaseCreator
) -> ClickHouseDatabaseCreatorHandle:
    """Database-creator handle. ``SessionDatabaseCreator`` admits is_admin or
    can_create_database."""
    return ClickHouseDatabaseCreatorHandle(
        client=request.app.state.clickhouse_client,
        settings=request.app.state.clickhouse_settings,
        username=session.user.username,
    )


async def require_clickhouse_database_admin(
    request: Request, database: str, session: SessionDatabaseAdmin
) -> ClickHouseDatabaseAdminHandle:
    """Per-database admin handle. ``database`` is bound from the calling
    route's path/query params by FastAPI; ``SessionDatabaseAdmin`` raises 403
    unless ``rights.has_admin(database)``."""
    validate_identifier(database, kind="database")
    return ClickHouseDatabaseAdminHandle(
        client=request.app.state.clickhouse_client,
        http_client=request.app.state.clickhouse_http_client,
        settings=request.app.state.clickhouse_settings,
        database=database,
        username=session.user.username,
    )
