"""Shell routes: home page, tab lifecycle, feature-render proxy."""
from __future__ import annotations

import json
import logging
from typing import Annotated

from datastar_py.consts import ElementPatchMode
from datastar_py.fastapi import DatastarResponse
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
    TabRecord,
    append_tab,
    find_tab,
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
        active_tab_id = tabs[0].id if tabs else ""
        tabs_signal = {t.id: {} for t in tabs}

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

        tab_id = new_tab_id()
        rec = TabRecord(
            id=tab_id, feature=feature, intent=intent,
            params=params_dict, title=spec.title(params_dict),
        )
        try:
            append_tab(session.data, rec)
        except TabCapExceeded as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        await session.persist_data()

        button_html = templates.get_template("shell/_tab_strip.html").render(
            tab=rec.to_json()
        )
        panel_html = templates.get_template("shell/_tab_panel.html").render(
            tab=rec.to_json()
        )
        return DatastarResponse([
            SSE.patch_elements(button_html, selector="#tab-strip", mode=ElementPatchMode.APPEND),
            SSE.patch_elements(panel_html, selector="#tab-content", mode=ElementPatchMode.APPEND),
            SSE.patch_signals({
                "tabs": {tab_id: {}},
                "active": tab_id,
            }),
        ])

    @app.delete("/api/tabs/{tab_id}")
    async def close_tab(
        session: Session,
        tab_id: str,
        _: None = Depends(verify_csrf_header),
    ) -> Response:
        if find_tab(session.data, tab_id) is None:
            return Response(status_code=204)
        remove_tab(session.data, tab_id)
        await session.persist_data()
        return DatastarResponse([
            SSE.patch_elements(selector=f"#tab-button-{tab_id}", mode=ElementPatchMode.REMOVE),
            SSE.patch_elements(selector=f"#tab-content-{tab_id}", mode=ElementPatchMode.REMOVE),
        ])

    @app.patch("/api/tabs/{tab_id}")
    async def retarget_tab(
        request: Request,
        session: Session,
        tab_id: str,
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
