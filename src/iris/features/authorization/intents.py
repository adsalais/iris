"""Intent render functions for the Authorization feature.

The shell's per-feature route /feature/auth/{tab_id}/render dispatches
on tab.intent into RENDER_BY_INTENT. Phase 3 added my_access; Phase 4
adds manage; Phase 5 adds create_database; Phase 6 adds admin_console
(and its four sub-tab handlers).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, Response

if TYPE_CHECKING:
    from iris.auth.views import AuthSession
    from iris.shell.tabs import TabRecord

IntentHandler = Callable[[Request, "AuthSession", "TabRecord"], Awaitable[Response]]


async def render_my_access(
    request: Request,
    session: "AuthSession",
    rec: "TabRecord",
) -> Response:
    from iris.features.authorization.service import my_access_view
    from iris.shell.element_id import tab_panel_id

    templates = request.app.state.templates
    ctx = my_access_view(session.capabilities)
    return templates.TemplateResponse(
        request,
        "authorization/my_access.html",
        {
            "user": session.user,
            "panel_id": tab_panel_id(rec.id),
            **ctx,
        },
    )


async def render_manage(
    request: Request,
    session: "AuthSession",
    rec: "TabRecord",
) -> Response:
    from iris.auth.views import DatabaseAdminSession
    from iris.features.authorization.service import manage_view
    from iris.shell.element_id import tab_panel_id

    database = rec.params.get("database", "")
    if not database:
        raise HTTPException(status_code=400, detail="database param required")

    if not session.capabilities.has_admin(database):
        raise HTTPException(status_code=403, detail="not a database admin")

    db_session = DatabaseAdminSession(
        id=session.id, user=session.user,
        created_at=session.created_at, expires_at=session.expires_at,
        data=session.data, capabilities=session.capabilities,
        client=session.client, http_client=session.http_client,
        settings=session.settings, store=session.store,
        database=database,
    )

    templates = request.app.state.templates
    ctx = await manage_view(db_session)
    return templates.TemplateResponse(
        request,
        "authorization/manage.html",
        {
            "panel_id": tab_panel_id(rec.id),
            "tab_id": rec.id,
            "database": database,
            **ctx,
        },
    )


RENDER_BY_INTENT: dict[str, IntentHandler] = {
    "my_access": render_my_access,
    "manage": render_manage,
}
