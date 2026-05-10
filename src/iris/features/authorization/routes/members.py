"""Tier-member grant / revoke routes for the Authorization feature.

Two routes (``POST`` / ``DELETE``) parameterized on ``(tier, kind)``
collapse what would otherwise be twelve near-identical handlers. The
``_MEMBER_ACTIONS`` table maps each ``(tier, kind, op)`` triple to the
corresponding ``DatabaseAdminSession`` method, so the route layer is
purely dispatch — all SQL discipline stays in the typed session API.
"""
from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from iris.auth.csrf import verify_csrf_header
from iris.auth.deps import SessionDatabaseAdmin
from iris.auth.views import DatabaseAdminSession
from iris.features.authorization.routes._common import re_render_members
from iris.shell.element_id import tab_panel_id

router = APIRouter()

_TIERS: Final = frozenset({"reader", "writer", "admin"})
_KINDS: Final = frozenset({"user", "group"})

# (tier, kind, op) -> DatabaseAdminSession method name. The asymmetry
# between the reader/writer methods (``grant_*`` / ``revoke_*``) and the
# admin methods (``add_admin_*`` / ``remove_admin_*``) lives in the
# session API; we tolerate it here so this layer can stay a thin map.
_MEMBER_ACTIONS: Final[dict[tuple[str, str, str], str]] = {
    ("reader", "user", "grant"):   "grant_reader",
    ("reader", "user", "revoke"):  "revoke_reader",
    ("writer", "user", "grant"):   "grant_writer",
    ("writer", "user", "revoke"):  "revoke_writer",
    ("admin",  "user", "grant"):   "add_admin_user",
    ("admin",  "user", "revoke"):  "remove_admin_user",
    ("reader", "group", "grant"):  "grant_reader_to_group",
    ("reader", "group", "revoke"): "revoke_reader_from_group",
    ("writer", "group", "grant"):  "grant_writer_to_group",
    ("writer", "group", "revoke"): "revoke_writer_from_group",
    ("admin",  "group", "grant"):  "add_admin_group",
    ("admin",  "group", "revoke"): "remove_admin_group",
}


def _resolve_target(kind: str, username: str | None, group: str | None) -> str:
    if kind == "user":
        if not username:
            raise HTTPException(status_code=422, detail="username required")
        return username
    if not group:
        raise HTTPException(status_code=422, detail="group required")
    return group


async def _change_member(
    request: Request, db: DatabaseAdminSession, *, tab_id: str,
    tier: str, kind: str, op: str, target: str,
) -> Response:
    if tier not in _TIERS or kind not in _KINDS:
        raise HTTPException(status_code=404, detail="unknown member route")
    method_name = _MEMBER_ACTIONS[(tier, kind, op)]
    method = getattr(db, method_name)
    await method(target)
    return await re_render_members(request, db, tab_panel_id(tab_id), tab_id)


@router.post("/{tab_id}/members/{tier}/{kind}")
async def grant_member(
    request: Request,
    db: SessionDatabaseAdmin,
    tab_id: str,
    tier: str,
    kind: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    username: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    group: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    _: None = Depends(verify_csrf_header),
) -> Response:
    target = _resolve_target(kind, username, group)
    return await _change_member(
        request, db, tab_id=tab_id, tier=tier, kind=kind, op="grant", target=target,
    )


@router.delete("/{tab_id}/members/{tier}/{kind}")
async def revoke_member(
    request: Request,
    db: SessionDatabaseAdmin,
    tab_id: str,
    tier: str,
    kind: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
    username: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    group: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    _: None = Depends(verify_csrf_header),
) -> Response:
    target = _resolve_target(kind, username, group)
    return await _change_member(
        request, db, tab_id=tab_id, tier=tier, kind=kind, op="revoke", target=target,
    )
