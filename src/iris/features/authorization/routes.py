"""APIRouter for the Authorization feature.

Mounted at /feature/auth by install. Each phase fills in more routes:
Phase 3 only handles the render-by-intent dispatch for my_access; Phase 4
adds /manage routes; Phase 5 adds /create_database; Phase 6 adds
/admin_console sub-routes.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from iris.auth.deps import Session
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
