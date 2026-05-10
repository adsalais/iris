"""Admin-console sub-tab GET routes + the reprovision POST.

Each GET fetches an admin-scoped inventory via the typed ``AdminSession``
and renders it into a single ``#<panel_id>-subtab`` slot. The four GETs
share enough boilerplate that the bodies are one line each after the
helper does the work.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from iris.auth.csrf import verify_csrf_header
from iris.auth.deps import SessionAdmin
from iris.auth.views import AdminSession
from iris.features.authorization.routes._common import render_subtab_outer
from iris.shell.element_id import tab_panel_id

router = APIRouter()


async def _render_inventory_subtab(
    request: Request,
    admin: AdminSession,
    tab_id: str,
    *,
    template: str,
    ctx_key: str,
    fetch: Callable[[AdminSession], Awaitable[list[dict[str, object]]]],
) -> Response:
    """Common shape for the four inventory sub-tabs.

    ``fetch`` is the bound ``AdminSession`` coroutine that returns the
    inventory rows; ``ctx_key`` is the variable name the template
    expects (``users`` / ``databases`` / ``policies`` / ``grants``).
    """
    rows = await fetch(admin)
    panel_id = tab_panel_id(tab_id)
    return render_subtab_outer(request, template, panel_id, {
        "panel_id": panel_id, "tab_id": tab_id, ctx_key: rows,
    })


@router.get("/{tab_id}/admin/users")
async def admin_users(
    request: Request, admin: SessionAdmin, tab_id: str,
) -> Response:
    return await _render_inventory_subtab(
        request, admin, tab_id,
        template="authorization/_admin_users.html",
        ctx_key="users",
        fetch=lambda a: a.list_users(),
    )


@router.get("/{tab_id}/admin/databases")
async def admin_databases(
    request: Request, admin: SessionAdmin, tab_id: str,
) -> Response:
    return await _render_inventory_subtab(
        request, admin, tab_id,
        template="authorization/_admin_databases.html",
        ctx_key="databases",
        fetch=lambda a: a.list_databases(),
    )


@router.get("/{tab_id}/admin/policies")
async def admin_policies(
    request: Request, admin: SessionAdmin, tab_id: str,
) -> Response:
    return await _render_inventory_subtab(
        request, admin, tab_id,
        template="authorization/_admin_policies.html",
        ctx_key="policies",
        fetch=lambda a: a.list_all_row_policies(),
    )


@router.get("/{tab_id}/admin/audit")
async def admin_audit(
    request: Request, admin: SessionAdmin, tab_id: str,
) -> Response:
    return await _render_inventory_subtab(
        request, admin, tab_id,
        template="authorization/_admin_audit.html",
        ctx_key="grants",
        fetch=lambda a: a.list_all_grants(),
    )


@router.post("/{tab_id}/admin/users/{username}/reprovision")
async def admin_reprovision_user(
    request: Request, admin: SessionAdmin, tab_id: str, username: str,
    _: Annotated[None, Depends(verify_csrf_header)] = None,
) -> Response:
    # IdP groups aren't accessible from this code path; reprovision_user
    # rebuilds CH user identity + tier roles with empty groups.
    await admin.reprovision_user(username=username, groups=[])
    return await _render_inventory_subtab(
        request, admin, tab_id,
        template="authorization/_admin_users.html",
        ctx_key="users",
        fetch=lambda a: a.list_users(),
    )
