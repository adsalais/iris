"""APIRouter for the Authorization feature.

Mounted at /feature/auth by install. Each phase fills in more routes:
Phase 3 added my_access; Phase 4 adds /manage routes; Phase 5 adds
/create_database; Phase 6 adds /admin_console sub-routes.
"""
from __future__ import annotations

from typing import Annotated

from datastar_py.consts import ElementPatchMode
from datastar_py.fastapi import DatastarResponse
from datastar_py.fastapi import ServerSentEventGenerator as SSE
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from iris.auth.csrf import verify_csrf_header
from iris.auth.deps import Session
from iris.auth.views import AuthSession, DatabaseAdminSession
from iris.shell.element_id import tab_panel_id
from iris.shell.tabs import find_tab

router = APIRouter(prefix="/feature/auth")


@router.get("/{tab_id}/render")
async def render(
    request: Request,
    session: Session,
    tab_id: str,
) -> Response:
    rec = find_tab(session.data, tab_id)
    if rec is None or rec.feature != "auth":
        raise HTTPException(status_code=404, detail="tab not found")

    from iris.features.authorization.intents import RENDER_BY_INTENT
    handler = RENDER_BY_INTENT.get(rec.intent)
    if handler is None:
        raise HTTPException(status_code=404, detail="unknown intent")
    return await handler(request, session, rec)


# ---------------------------------------------------------------------------
# manage members — 12 routes ({reader,writer,admin} × {user,group} × {POST,DELETE})
# ---------------------------------------------------------------------------


def _promote_to_db_admin(session: AuthSession, database: str) -> DatabaseAdminSession:
    if not session.capabilities.has_admin(database):
        raise HTTPException(status_code=403, detail="not a database admin")
    return DatabaseAdminSession(
        id=session.id, user=session.user,
        created_at=session.created_at, expires_at=session.expires_at,
        data=session.data, capabilities=session.capabilities,
        client=session.client, http_client=session.http_client,
        settings=session.settings, store=session.store,
        database=database,
    )


async def _members_route_common(
    session: AuthSession, tab_id: str,
) -> tuple[DatabaseAdminSession, str]:
    """Resolve tab → database → DatabaseAdminSession + panel_id."""
    rec = find_tab(session.data, tab_id)
    if rec is None or rec.feature != "auth" or rec.intent != "manage":
        raise HTTPException(status_code=404, detail="tab not found")
    database = rec.params.get("database", "")
    if not database:
        raise HTTPException(status_code=400, detail="database missing")
    db_session = _promote_to_db_admin(session, database)
    return db_session, tab_panel_id(rec.id)


async def _re_render_members(
    request: Request, db_session: DatabaseAdminSession, panel_id: str, tab_id: str,
) -> Response:
    from iris.features.authorization.service import list_members
    members = await list_members(db_session)
    templates = request.app.state.templates
    html = templates.get_template("authorization/_members_section.html").render(
        panel_id=panel_id, tab_id=tab_id, members=members,
    )
    return DatastarResponse(
        SSE.patch_elements(
            html, selector=f"#{panel_id}-members", mode=ElementPatchMode.OUTER,
        ),
    )


# Reader user
@router.post("/{tab_id}/members/reader/user")
async def grant_reader_user(
    request: Request, session: Session, tab_id: str,
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    db, panel_id = await _members_route_common(session, tab_id)
    await db.grant_reader(username)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.delete("/{tab_id}/members/reader/user")
async def revoke_reader_user(
    request: Request, session: Session, tab_id: str,
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    db, panel_id = await _members_route_common(session, tab_id)
    await db.revoke_reader(username)
    return await _re_render_members(request, db, panel_id, tab_id)


# Reader group
@router.post("/{tab_id}/members/reader/group")
async def grant_reader_group(
    request: Request, session: Session, tab_id: str,
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    db, panel_id = await _members_route_common(session, tab_id)
    await db.grant_reader_to_group(group)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.delete("/{tab_id}/members/reader/group")
async def revoke_reader_group(
    request: Request, session: Session, tab_id: str,
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    db, panel_id = await _members_route_common(session, tab_id)
    await db.revoke_reader_from_group(group)
    return await _re_render_members(request, db, panel_id, tab_id)


# Writer user
@router.post("/{tab_id}/members/writer/user")
async def grant_writer_user(
    request: Request, session: Session, tab_id: str,
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    db, panel_id = await _members_route_common(session, tab_id)
    await db.grant_writer(username)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.delete("/{tab_id}/members/writer/user")
async def revoke_writer_user(
    request: Request, session: Session, tab_id: str,
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    db, panel_id = await _members_route_common(session, tab_id)
    await db.revoke_writer(username)
    return await _re_render_members(request, db, panel_id, tab_id)


# Writer group
@router.post("/{tab_id}/members/writer/group")
async def grant_writer_group(
    request: Request, session: Session, tab_id: str,
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    db, panel_id = await _members_route_common(session, tab_id)
    await db.grant_writer_to_group(group)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.delete("/{tab_id}/members/writer/group")
async def revoke_writer_group(
    request: Request, session: Session, tab_id: str,
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    db, panel_id = await _members_route_common(session, tab_id)
    await db.revoke_writer_from_group(group)
    return await _re_render_members(request, db, panel_id, tab_id)


# Admin user
@router.post("/{tab_id}/members/admin/user")
async def grant_admin_user(
    request: Request, session: Session, tab_id: str,
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    db, panel_id = await _members_route_common(session, tab_id)
    await db.add_admin_user(username)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.delete("/{tab_id}/members/admin/user")
async def revoke_admin_user(
    request: Request, session: Session, tab_id: str,
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    db, panel_id = await _members_route_common(session, tab_id)
    await db.remove_admin_user(username)
    return await _re_render_members(request, db, panel_id, tab_id)


# Admin group
@router.post("/{tab_id}/members/admin/group")
async def grant_admin_group(
    request: Request, session: Session, tab_id: str,
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    db, panel_id = await _members_route_common(session, tab_id)
    await db.add_admin_group(group)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.delete("/{tab_id}/members/admin/group")
async def revoke_admin_group(
    request: Request, session: Session, tab_id: str,
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    db, panel_id = await _members_route_common(session, tab_id)
    await db.remove_admin_group(group)
    return await _re_render_members(request, db, panel_id, tab_id)
