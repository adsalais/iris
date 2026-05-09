"""Intent render functions for the Authorization feature.

The shell's per-feature route /feature/auth/{tab_id}/render dispatches
on tab.intent into RENDER_BY_INTENT. Phase 3 adds my_access; Phase 4 adds
manage; Phase 5 adds create_database; Phase 6 adds admin_console (and
its four sub-tab handlers).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import Request, Response

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


RENDER_BY_INTENT: dict[str, IntentHandler] = {
    "my_access": render_my_access,
}
