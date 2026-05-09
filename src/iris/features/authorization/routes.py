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
from iris.auth.deps import (
    Session,
    SessionAdmin,
    SessionDatabaseAdmin,
    SessionDatabaseCreator,
)
from iris.auth.views import DatabaseAdminSession
from iris.shell.element_id import tab_panel_id
from iris.shell.tabs import find_tab

router = APIRouter(prefix="/feature/auth")


# ---------------------------------------------------------------------------
# Per-intent render routes — each intent has its own GET with the typed dep
# that gates its capability requirement. The shell's tab_render_url Jinja
# global builds the URL the panel hits.
# ---------------------------------------------------------------------------


@router.get("/{tab_id}/my_access")
async def render_my_access(
    request: Request,
    session: Session,
    tab_id: str,
) -> Response:
    from iris.features.authorization.service import my_access_view
    templates = request.app.state.templates
    ctx = my_access_view(session.capabilities)
    return templates.TemplateResponse(
        request, "authorization/my_access.html",
        {
            "user": session.user,
            "panel_id": tab_panel_id(tab_id),
            **ctx,
        },
    )


@router.get("/{tab_id}/manage")
async def render_manage(
    request: Request,
    db: SessionDatabaseAdmin,
    tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
) -> Response:
    from iris.features.authorization.service import manage_view
    templates = request.app.state.templates
    ctx = await manage_view(db)
    return templates.TemplateResponse(
        request, "authorization/manage.html",
        {
            "panel_id": tab_panel_id(tab_id),
            "tab_id": tab_id,
            "database": db.database,
            **ctx,
        },
    )


@router.get("/{tab_id}/create_database")
async def render_create_database(
    request: Request,
    creator: SessionDatabaseCreator,  # noqa: ARG001  # gates is_admin or can_create_database  # pyright: ignore[reportUnusedParameter]
    tab_id: str,
) -> Response:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "authorization/create_database.html",
        {"panel_id": tab_panel_id(tab_id), "tab_id": tab_id, "error": None},
    )


@router.get("/{tab_id}/admin_console")
async def render_admin_console(
    request: Request,
    admin: SessionAdmin,  # noqa: ARG001  # gates is_admin  # pyright: ignore[reportUnusedParameter]
    tab_id: str,
    subtab: Annotated[str, Query()] = "users",
) -> Response:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "authorization/admin_console.html",
        {
            "panel_id": tab_panel_id(tab_id),
            "tab_id": tab_id,
            "initial_subtab": subtab,
        },
    )


# ---------------------------------------------------------------------------
# manage members — 12 routes ({reader,writer,admin} × {user,group} × {POST,DELETE})
# ---------------------------------------------------------------------------


async def _re_render_members(
    request: Request, db_session: DatabaseAdminSession, panel_id: str, tab_id: str,
) -> Response:
    from iris.features.authorization.service import list_members
    members = await list_members(db_session)
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


# Reader user
@router.post("/{tab_id}/members/reader/user")
async def grant_reader_user(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.grant_reader(username)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


@router.delete("/{tab_id}/members/reader/user")
async def revoke_reader_user(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.revoke_reader(username)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


# Reader group
@router.post("/{tab_id}/members/reader/group")
async def grant_reader_group(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.grant_reader_to_group(group)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


@router.delete("/{tab_id}/members/reader/group")
async def revoke_reader_group(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.revoke_reader_from_group(group)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


# Writer user
@router.post("/{tab_id}/members/writer/user")
async def grant_writer_user(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.grant_writer(username)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


@router.delete("/{tab_id}/members/writer/user")
async def revoke_writer_user(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.revoke_writer(username)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


# Writer group
@router.post("/{tab_id}/members/writer/group")
async def grant_writer_group(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.grant_writer_to_group(group)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


@router.delete("/{tab_id}/members/writer/group")
async def revoke_writer_group(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.revoke_writer_from_group(group)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


# Admin user
@router.post("/{tab_id}/members/admin/user")
async def grant_admin_user(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.add_admin_user(username)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


@router.delete("/{tab_id}/members/admin/user")
async def revoke_admin_user(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.remove_admin_user(username)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


# Admin group
@router.post("/{tab_id}/members/admin/group")
async def grant_admin_group(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.add_admin_group(group)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


@router.delete("/{tab_id}/members/admin/group")
async def revoke_admin_group(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.remove_admin_group(group)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


# ---------------------------------------------------------------------------
# manage row policies
# ---------------------------------------------------------------------------


async def _re_render_policies(
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


@router.post("/{tab_id}/policies")
async def add_policy(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    table: Annotated[str, Query(min_length=1, max_length=64)],
    column: Annotated[str, Query(min_length=1, max_length=64)],
    role: Annotated[str, Query(min_length=1, max_length=64)],
    value: Annotated[str, Query(min_length=0, max_length=4096)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.add_row_policy(table=table, column=column, role=role, value=value)
    return await _re_render_policies(request, db, tab_panel_id(tab_id), tab_id)


@router.delete("/{tab_id}/policies")
async def revoke_policy(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    table: Annotated[str, Query(min_length=1, max_length=64)],
    role: Annotated[str, Query(min_length=1, max_length=64)],
    value: Annotated[str, Query(min_length=0, max_length=4096)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.revoke_row_policy(table=table, role=role, value=value)
    return await _re_render_policies(request, db, tab_panel_id(tab_id), tab_id)


# ---------------------------------------------------------------------------
# admin_console — sub-tab GET routes
# ---------------------------------------------------------------------------


@router.get("/{tab_id}/admin/users")
async def admin_users(
    request: Request, admin: SessionAdmin, tab_id: str,
) -> Response:
    from iris.features.authorization.service import list_all_users
    users = await list_all_users(admin)
    panel_id = tab_panel_id(tab_id)
    templates = request.app.state.templates
    html = templates.get_template("authorization/_admin_users.html").render(
        panel_id=panel_id, tab_id=tab_id, users=users,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{panel_id}-subtab", mode=ElementPatchMode.OUTER,
    ))


@router.get("/{tab_id}/admin/databases")
async def admin_databases(
    request: Request, admin: SessionAdmin, tab_id: str,
) -> Response:
    from iris.features.authorization.service import list_all_databases
    databases = await list_all_databases(admin)
    panel_id = tab_panel_id(tab_id)
    templates = request.app.state.templates
    html = templates.get_template("authorization/_admin_databases.html").render(
        panel_id=panel_id, tab_id=tab_id, databases=databases,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{panel_id}-subtab", mode=ElementPatchMode.OUTER,
    ))


@router.get("/{tab_id}/admin/policies")
async def admin_policies(
    request: Request, admin: SessionAdmin, tab_id: str,
) -> Response:
    from iris.features.authorization.service import list_all_row_policies
    policies = await list_all_row_policies(admin)
    panel_id = tab_panel_id(tab_id)
    templates = request.app.state.templates
    html = templates.get_template("authorization/_admin_policies.html").render(
        panel_id=panel_id, tab_id=tab_id, policies=policies,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{panel_id}-subtab", mode=ElementPatchMode.OUTER,
    ))


@router.post("/{tab_id}/admin/users/{username}/reprovision")
async def admin_reprovision_user(
    request: Request, admin: SessionAdmin, tab_id: str, username: str,
    _: None = Depends(verify_csrf_header),
) -> Response:
    from iris.features.authorization.service import list_all_users
    # IdP groups aren't accessible from this code path; reprovision_user
    # rebuilds CH user identity + tier roles with empty groups.
    await admin.reprovision_user(username=username, groups=[])
    users = await list_all_users(admin)
    panel_id = tab_panel_id(tab_id)
    templates = request.app.state.templates
    html = templates.get_template("authorization/_admin_users.html").render(
        panel_id=panel_id, tab_id=tab_id, users=users,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{panel_id}-subtab", mode=ElementPatchMode.OUTER,
    ))


@router.get("/{tab_id}/admin/audit")
async def admin_audit(
    request: Request, admin: SessionAdmin, tab_id: str,
) -> Response:
    from iris.features.authorization.service import list_all_grants
    grants = await list_all_grants(admin)
    panel_id = tab_panel_id(tab_id)
    templates = request.app.state.templates
    html = templates.get_template("authorization/_admin_audit.html").render(
        panel_id=panel_id, tab_id=tab_id, grants=grants,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{panel_id}-subtab", mode=ElementPatchMode.OUTER,
    ))


# ---------------------------------------------------------------------------
# create_database — submit handler
# ---------------------------------------------------------------------------


@router.post("/{tab_id}/create_database")
async def submit_create_database(
    request: Request,
    creator: SessionDatabaseCreator,
    tab_id: str,
    name: Annotated[str, Query(min_length=0, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    from iris.shell.tabs import TabRecord, replace_tab

    rec = find_tab(creator.data, tab_id)
    if rec is None or rec.feature != "auth" or rec.intent != "create_database":
        raise HTTPException(status_code=404, detail="tab not found")

    templates = request.app.state.templates
    panel_id = tab_panel_id(tab_id)

    try:
        await creator.create_database(name)
    except (ValueError, RuntimeError) as e:
        # Re-render the form with the error inline. Validation errors
        # (InvalidIdentifierError <: ValueError) and CH-side errors all
        # surface as inline error fragments for the user to fix.
        html = templates.get_template("authorization/create_database.html").render(
            panel_id=panel_id, tab_id=tab_id, error=str(e),
        )
        return DatastarResponse(
            SSE.patch_elements(
                html, selector=f"#{panel_id}", mode=ElementPatchMode.OUTER,
            ),
        )

    # Success: re-target the existing tab to manage <new_db>.
    new_rec = TabRecord(
        id=tab_id, feature="auth", intent="manage",
        params={"database": name}, title=f"Manage {name}",
    )
    replace_tab(creator.data, tab_id, new_rec)
    await creator.persist_data()
    return DatastarResponse([
        SSE.patch_elements(
            templates.get_template("shell/_tab_strip.html").render(tab=new_rec.to_json()),
            selector=f"#tab-button-{tab_id}",
            mode=ElementPatchMode.OUTER,
        ),
        SSE.patch_elements(
            templates.get_template("shell/_tab_panel.html").render(tab=new_rec.to_json()),
            selector=f"#tab-content-{tab_id}",
            mode=ElementPatchMode.OUTER,
        ),
    ])


# ---------------------------------------------------------------------------
# danger zone — delete database
# ---------------------------------------------------------------------------


@router.delete("/{tab_id}/database")
async def delete_database(
    db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    confirm: Annotated[str, Query(min_length=0, max_length=255)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    from iris.shell.tabs import remove_tab

    if confirm != database:
        raise HTTPException(
            status_code=400,
            detail="confirmation does not match the database name",
        )

    await db.delete_database()

    remove_tab(db.data, tab_id)  # no-op if tab_id doesn't match an open tab
    await db.persist_data()
    return DatastarResponse([
        SSE.patch_elements(
            selector=f"#tab-button-{tab_id}", mode=ElementPatchMode.REMOVE,
        ),
        SSE.patch_elements(
            selector=f"#tab-content-{tab_id}", mode=ElementPatchMode.REMOVE,
        ),
    ])
