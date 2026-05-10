"""Shared render helpers for the Authorization feature's sub-routers.

Lifted out of the per-area route modules so each sub-module stays focused
on its endpoints. The render helpers know nothing about which sub-router
calls them — they take the live ``request`` and the typed CH session and
produce a ``DatastarResponse``.
"""
from __future__ import annotations

from datastar_py.consts import ElementPatchMode
from datastar_py.fastapi import DatastarResponse
from datastar_py.fastapi import ServerSentEventGenerator as SSE
from fastapi import Request, Response

from iris.auth.views import DatabaseAdminSession


def render_panel_inner(
    request: Request, template_name: str, ctx: dict[str, object],
) -> Response:
    """Render a tab panel's body and return it as an SSE INNER patch.

    Why INNER: the panel wrapper ``<div id="tab-content-XXX" data-show=…
    data-init=…>`` is created by the shell at tab-open time and carries
    the visibility binding. OUTER mode (or a plain TemplateResponse,
    which Datastar morphs over the calling element) replaces the wrapper
    and strips data-show — every panel that loads its content this way
    then becomes permanently visible, so every open tab visually shows
    the most-recently-loaded content.
    """
    templates = request.app.state.templates
    html = templates.get_template(template_name).render(request=request, **ctx)
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{ctx['panel_id']}", mode=ElementPatchMode.INNER,
    ))


def render_subtab_outer(
    request: Request, template_name: str, panel_id: str, ctx: dict[str, object],
) -> Response:
    """Render a sub-tab fragment and OUTER-patch it onto ``#<panel_id>-subtab``.

    Used by every admin_console subtab handler and by the reprovision
    POST. OUTER is correct here because the target IS the fragment we
    own, not a wrapper with bindings on it.
    """
    templates = request.app.state.templates
    html = templates.get_template(template_name).render(request=request, **ctx)
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{panel_id}-subtab", mode=ElementPatchMode.OUTER,
    ))


async def re_render_members(
    request: Request, db_session: DatabaseAdminSession, panel_id: str, tab_id: str,
) -> Response:
    members = await db_session.list_members()
    templates = request.app.state.templates
    html = templates.get_template("authorization/_members_section.html").render(
        panel_id=panel_id, tab_id=tab_id, members=members,
        database=db_session.database,
    )
    return DatastarResponse(
        SSE.patch_elements(
            html, selector=f"#{panel_id}-members", mode=ElementPatchMode.OUTER,
        ),
    )


async def re_render_policies(
    request: Request, db_session: DatabaseAdminSession, panel_id: str, tab_id: str,
) -> Response:
    row_policies = await db_session.list_row_policies()
    templates = request.app.state.templates
    html = templates.get_template("authorization/_row_policies.html").render(
        panel_id=panel_id, tab_id=tab_id, row_policies=row_policies,
        database=db_session.database,
    )
    return DatastarResponse(
        SSE.patch_elements(
            html, selector=f"#{panel_id}-policies", mode=ElementPatchMode.OUTER,
        ),
    )
