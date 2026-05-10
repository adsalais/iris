"""Row-policy add / revoke routes for the Authorization feature."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from iris.auth.csrf import verify_csrf_header
from iris.auth.deps import SessionDatabaseAdmin
from iris.features.authorization.routes._common import re_render_policies
from iris.shell.element_id import tab_panel_id

router = APIRouter()


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
    return await re_render_policies(request, db, tab_panel_id(tab_id), tab_id)


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
    return await re_render_policies(request, db, tab_panel_id(tab_id), tab_id)
