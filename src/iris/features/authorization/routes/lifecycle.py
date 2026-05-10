"""Database lifecycle routes: create_database submit + delete_database."""
from __future__ import annotations

from typing import Annotated

from datastar_py.consts import ElementPatchMode
from datastar_py.fastapi import DatastarResponse
from datastar_py.fastapi import ServerSentEventGenerator as SSE
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from iris.auth.csrf import verify_csrf_header
from iris.auth.deps import (
    SessionDatabaseAdmin,
    SessionDatabaseCreator,
)
from iris.shell.element_id import tab_panel_id
from iris.shell.tabs import TabRecord, find_tab, remove_tab, replace_tab

router = APIRouter()


@router.post("/{tab_id}/create_database")
async def submit_create_database(
    request: Request,
    creator: SessionDatabaseCreator,
    tab_id: str,
    name: Annotated[str, Query(min_length=0, max_length=64)],
    _: Annotated[None, Depends(verify_csrf_header)] = None,
) -> Response:
    rec = find_tab(creator.data, tab_id)
    if rec is None or rec.feature != "authorization" or rec.intent != "create_database":
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
        id=tab_id, feature="authorization", intent="manage",
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


@router.delete("/{tab_id}/database")
async def delete_database(
    db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    confirm: Annotated[str, Query(min_length=0, max_length=255)],
    _: Annotated[None, Depends(verify_csrf_header)] = None,
) -> Response:
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
