import asyncio
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Annotated, Any

from datastar_py.fastapi import DatastarResponse, read_signals
from datastar_py.fastapi import ServerSentEventGenerator as SSE
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from iris.auth.csrf import attach_csrf_cookie, mint_csrf_token
from iris.auth.deps import CurrentUser
from iris.middleware import SecurityHeadersMiddleware

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


async def _signals(request: Request) -> dict[str, Any]:
    return await read_signals(request) or {}


Signals = Annotated[dict[str, Any], Depends(_signals)]


async def _clock_stream():
    while True:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        yield SSE.patch_signals({"now": now})
        await asyncio.sleep(1)


def build_app() -> FastAPI:
    app = FastAPI(title="Iris")

    from iris.auth.routes import install as install_auth

    install_auth(app)

    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, user: CurrentUser):
        # Mint (or reuse) the CSRF token, then attach the cookie to the
        # TemplateResponse explicitly. Routes that return their own Response
        # bypass FastAPI's dep-injected-Response cookie merge, so we can't
        # rely on Depends(issue_csrf_token) here.
        csrf = mint_csrf_token(request)
        response = TEMPLATES.TemplateResponse(
            request, "index.html", {"user": user, "csrf_token": csrf}
        )
        attach_csrf_cookie(request, response, csrf)
        return response

    @app.get("/api/greet")
    async def greet(signals: Signals, user: CurrentUser) -> DatastarResponse:
        raw = str(signals.get("name") or user.display_name).strip()
        name = escape(raw) if raw else "stranger"
        return DatastarResponse(
            SSE.patch_elements(f'<div id="greeting">Hello, <strong>{name}</strong>!</div>')
        )

    @app.get("/api/clock")
    async def clock(user: CurrentUser) -> DatastarResponse:
        return DatastarResponse(_clock_stream())

    return app


app = build_app()
