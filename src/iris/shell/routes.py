"""Shell routes: home page, tab lifecycle, feature-render proxy."""
from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from datastar_py.consts import ElementPatchMode
from datastar_py.fastapi import DatastarResponse, read_signals
from datastar_py.fastapi import ServerSentEventGenerator as SSE
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse

from iris.auth.csrf import (
    attach_csrf_cookie,
    mint_csrf_token,
    verify_csrf_header,
)
from iris.auth.deps import Session
from iris.shell.contributions import Contributions
from iris.shell.intent_dispatch import (
    IntentDispatcher,
    IntentForbidden,
    IntentNotFound,
)
from iris.shell.nav_render import render_nav
from iris.shell.tabs import (
    TabCapExceeded,
    TabId,
    TabRecord,
    append_tab,
    find_tab,
    find_temporary_tab,
    list_tabs,
    new_tab_id,
    remove_tab,
    replace_tab,
)

logger = logging.getLogger("iris.shell")


def install_routes(app: FastAPI) -> None:

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request, session: Session) -> Response:
        contribs: Contributions = request.app.state.contributions
        templates = request.app.state.templates

        nav_html = render_nav(contribs, session.capabilities)
        tabs = list_tabs(session.data)
        # Restore last-active tab if still present; otherwise fall back to
        # the leftmost. Persisted by open_tab / retarget_tab / close_tab.
        stored_active = session.data.get("active_tab_id", "")
        tab_ids = {t.id for t in tabs}
        if stored_active in tab_ids:
            active_tab_id = stored_active
        else:
            active_tab_id = tabs[0].id if tabs else ""
        tabs_signal = {t.id: {"temporary": t.temporary} for t in tabs}

        csrf = mint_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "shell/shell.html",
            {
                "user": session.user,
                "nav_html": nav_html,
                "tabs": [t.to_json() for t in tabs],
                "tabs_signal": tabs_signal,
                "active_tab_id": active_tab_id,
                "csrf_token": csrf,
            },
        )
        attach_csrf_cookie(request, response, csrf)
        return response

    @app.post("/api/tabs")
    async def open_tab(
        request: Request,
        session: Session,
        feature: Annotated[str, Query(max_length=64)],
        intent: Annotated[str, Query(max_length=64)],
        params: Annotated[str, Query(max_length=4096)] = "{}",
        temporary: Annotated[bool, Query()] = False,
        from_tab: Annotated[str, Query(max_length=32)] = "",
        _: None = Depends(verify_csrf_header),
    ) -> Response:
        dispatcher: IntentDispatcher = request.app.state.intent_dispatcher
        templates = request.app.state.templates
        try:
            params_dict = json.loads(params)
            if not isinstance(params_dict, dict):
                msg = "params must be a JSON object"
                raise ValueError(msg)
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"invalid params: {e}") from e

        try:
            spec = dispatcher.check(feature, intent, session.capabilities)
        except IntentNotFound as e:
            raise HTTPException(status_code=400, detail="unknown intent") from e
        except IntentForbidden as e:
            raise HTTPException(status_code=403, detail="intent forbidden") from e

        events: list[Any] = []

        # If the open was triggered from inside `from_tab` (a button in its
        # content panel) and that tab is currently temporary, the user has
        # interacted with it — promote it before any temp-tab replacement
        # logic runs. Without this step the next branch would remove it.
        if from_tab:
            origin = find_tab(session.data, from_tab)
            if origin is not None and origin.temporary:
                replace_tab(session.data, from_tab, TabRecord(
                    id=origin.id, feature=origin.feature, intent=origin.intent,
                    params=origin.params, title=origin.title, temporary=False,
                ))
                events.append(SSE.patch_signals({
                    "tabs": {from_tab: {"temporary": False}},
                }))

        # Replace the existing temp tab (if any) when the new one is also
        # temporary. After the from_tab promote above, this only matches a
        # *different* temp tab — the preview-tab invariant is "at most one
        # temp tab at a time".
        if temporary:
            existing_temp = find_temporary_tab(session.data)
            if existing_temp is not None:
                remove_tab(session.data, existing_temp.id)
                events.append(SSE.patch_elements(
                    selector=f"#tab-button-{existing_temp.id}",
                    mode=ElementPatchMode.REMOVE,
                ))
                events.append(SSE.patch_elements(
                    selector=f"#tab-content-{existing_temp.id}",
                    mode=ElementPatchMode.REMOVE,
                ))

        tab_id = new_tab_id()
        rec = TabRecord(
            id=tab_id, feature=feature, intent=intent,
            params=params_dict, title=spec.title(params_dict), temporary=temporary,
        )
        try:
            append_tab(session.data, rec)
        except TabCapExceeded as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        session.data["active_tab_id"] = tab_id
        await session.persist_data()

        button_html = templates.get_template("shell/_tab_strip.html").render(
            tab=rec.to_json()
        )
        panel_html = templates.get_template("shell/_tab_panel.html").render(
            tab=rec.to_json()
        )
        events.extend([
            SSE.patch_elements(button_html, selector="#tab-strip", mode=ElementPatchMode.APPEND),
            SSE.patch_elements(panel_html, selector="#tab-content", mode=ElementPatchMode.APPEND),
            SSE.patch_signals({
                "tabs": {tab_id: {"temporary": temporary}},
                "active": tab_id,
            }),
        ])
        return DatastarResponse(events)

    @app.post("/api/tabs/{tab_id}/promote")
    async def promote_tab(
        session: Session,
        tab_id: TabId,
        _: None = Depends(verify_csrf_header),
    ) -> Response:
        rec = find_tab(session.data, tab_id)
        if rec is None or not rec.temporary:
            return Response(status_code=204)
        replace_tab(session.data, tab_id, TabRecord(
            id=rec.id, feature=rec.feature, intent=rec.intent,
            params=rec.params, title=rec.title, temporary=False,
        ))
        await session.persist_data()
        return DatastarResponse(SSE.patch_signals({
            "tabs": {tab_id: {"temporary": False}},
        }))

    @app.post("/api/tabs/{tab_id}/activate")
    async def activate_tab(
        session: Session,
        tab_id: TabId,
        _: None = Depends(verify_csrf_header),
    ) -> Response:
        """Persist the user's tab choice so a refresh lands on the same tab.

        Fired on every tab-strip click (in addition to the client-side
        ``$active = ...`` assignment that drives the immediate UI swap).
        Returns 204 — no SSE patch needed because the client already
        flipped its signal locally.
        """
        rec = find_tab(session.data, tab_id)
        if rec is None:
            return Response(status_code=204)
        session.data["active_tab_id"] = tab_id
        await session.persist_data()
        return Response(status_code=204)

    @app.delete("/api/tabs/{tab_id}")
    async def close_tab(
        request: Request,
        session: Session,
        tab_id: TabId,
        _: None = Depends(verify_csrf_header),
    ) -> Response:
        tabs = list_tabs(session.data)
        idx = next((i for i, t in enumerate(tabs) if t.id == tab_id), None)
        if idx is None:
            return Response(status_code=204)

        # Pick the tab that should become active if the user just closed
        # the active one: the left neighbor, or the right neighbor if the
        # closed tab was the leftmost, or "" when no tabs remain.
        if len(tabs) == 1:
            new_active = ""
        elif idx > 0:
            new_active = tabs[idx - 1].id
        else:
            new_active = tabs[idx + 1].id

        # Datastar sends current signals with @delete (URL query param).
        # We only re-target $active when the closed tab actually was the
        # active one — closing a background tab leaves $active alone.
        signals = await read_signals(request) or {}
        currently_active = signals.get("active")

        remove_tab(session.data, tab_id)
        if session.data.get("active_tab_id") == tab_id:
            session.data["active_tab_id"] = new_active
        await session.persist_data()

        events = [
            SSE.patch_elements(selector=f"#tab-button-{tab_id}", mode=ElementPatchMode.REMOVE),
            SSE.patch_elements(selector=f"#tab-content-{tab_id}", mode=ElementPatchMode.REMOVE),
        ]
        if currently_active == tab_id:
            events.append(SSE.patch_signals({"active": new_active}))
        return DatastarResponse(events)

    @app.patch("/api/tabs/{tab_id}")
    async def retarget_tab(
        request: Request,
        session: Session,
        tab_id: TabId,
        intent: Annotated[str, Query(max_length=64)],
        params: Annotated[str, Query(max_length=4096)] = "{}",
        _: None = Depends(verify_csrf_header),
    ) -> Response:
        existing = find_tab(session.data, tab_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="tab not found")
        dispatcher: IntentDispatcher = request.app.state.intent_dispatcher
        templates = request.app.state.templates
        try:
            params_dict = json.loads(params)
            if not isinstance(params_dict, dict):
                msg = "params must be a JSON object"
                raise ValueError(msg)
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"invalid params: {e}") from e

        try:
            spec = dispatcher.check(existing.feature, intent, session.capabilities)
        except IntentNotFound as e:
            raise HTTPException(status_code=400, detail="unknown intent") from e
        except IntentForbidden as e:
            raise HTTPException(status_code=403, detail="intent forbidden") from e

        new_rec = TabRecord(
            id=tab_id, feature=existing.feature, intent=intent,
            params=params_dict, title=spec.title(params_dict),
        )
        replace_tab(session.data, tab_id, new_rec)
        session.data["active_tab_id"] = tab_id
        await session.persist_data()

        return DatastarResponse([
            SSE.patch_signals({"active": tab_id}),
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

    # NOTE: /feature/{feature}/{tab_id}/render endpoints are owned by each
    # feature module's APIRouter (mounted at /feature/<feature>). FastAPI
    # naturally 404s when no feature has registered for the path.
