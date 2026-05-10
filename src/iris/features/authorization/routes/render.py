"""Per-intent render routes for the Authorization feature.

Each intent has its own GET with the typed dep that gates its capability
requirement. The shell's ``tab_render_url`` Jinja global builds the URL
the panel hits.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from iris.auth.deps import (
    Session,
    SessionAdmin,
    SessionDatabaseAdmin,
    SessionDatabaseCreator,
)
from iris.features.authorization.routes._common import render_panel_inner
from iris.shell.element_id import tab_panel_id
from iris.shell.tabs import TabId

router = APIRouter()


@router.get("/{tab_id}/my_access")
async def render_my_access(
    request: Request,
    session: Session,
    tab_id: TabId,
) -> Response:
    from iris.features.authorization.service import my_access_view
    ctx = my_access_view(session.capabilities)
    return render_panel_inner(request, "authorization/my_access.html", {
        "user": session.user,
        "panel_id": tab_panel_id(tab_id),
        **ctx,
    })


@router.get("/{tab_id}/manage")
async def render_manage(
    request: Request,
    db: SessionDatabaseAdmin,
    tab_id: TabId,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
) -> Response:
    from iris.features.authorization.service import manage_view
    ctx = await manage_view(db)
    return render_panel_inner(request, "authorization/manage.html", {
        "panel_id": tab_panel_id(tab_id),
        "tab_id": tab_id,
        "database": db.database,
        **ctx,
    })


@router.get("/{tab_id}/create_database")
async def render_create_database(
    request: Request,
    creator: SessionDatabaseCreator,  # noqa: ARG001  # gates is_admin or can_create_database  # pyright: ignore[reportUnusedParameter]
    tab_id: TabId,
) -> Response:
    return render_panel_inner(request, "authorization/create_database.html", {
        "panel_id": tab_panel_id(tab_id), "tab_id": tab_id, "error": None,
    })


@router.get("/{tab_id}/admin_console")
async def render_admin_console(
    request: Request,
    admin: SessionAdmin,  # noqa: ARG001  # gates is_admin  # pyright: ignore[reportUnusedParameter]
    tab_id: TabId,
    subtab: Annotated[str, Query()] = "users",
) -> Response:
    return render_panel_inner(request, "authorization/admin_console.html", {
        "panel_id": tab_panel_id(tab_id),
        "tab_id": tab_id,
        "initial_subtab": subtab,
    })
