import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from html import escape
from typing import Annotated, Any

from datastar_py.fastapi import DatastarResponse, read_signals
from datastar_py.fastapi import ServerSentEventGenerator as SSE
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse

from iris.auth import SessionView, require_session
from iris.auth.csrf import attach_csrf_cookie, mint_csrf_token
from iris.middleware import SecurityHeadersMiddleware
from iris.templates import TEMPLATES


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup is no-op; install() runs eagerly during build_app(). On shutdown,
    # close any teardown hooks registered by the auth or clickhouse layers
    # (OAuthProvider's httpx client, the impersonation httpx client, the SQLite
    # session store).
    yield
    closer = getattr(app.state, "auth_close_provider", None)
    if closer is not None:
        await closer()
    ch_closer = getattr(app.state, "clickhouse_close_http", None)
    if ch_closer is not None:
        await ch_closer()
    sess_closer = getattr(app.state, "auth_close_session_store", None)
    if sess_closer is not None:
        await sess_closer()
    authz_closer = getattr(app.state, "auth_close_authz_store", None)
    if authz_closer is not None:
        await authz_closer()
    db_admin_closer = getattr(app.state, "clickhouse_close_database_admins", None)
    if db_admin_closer is not None:
        await db_admin_closer()


async def _signals(request: Request) -> dict[str, Any]:
    return await read_signals(request) or {}


Signals = Annotated[dict[str, Any], Depends(_signals)]


async def _clock_stream():
    while True:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        yield SSE.patch_signals({"now": now})
        await asyncio.sleep(1)


def build_app(*, install_clickhouse: bool = True) -> FastAPI:
    app = FastAPI(title="Iris", lifespan=_lifespan)

    from iris.auth.routes import install as install_auth

    install_auth(app)

    if install_clickhouse:
        from iris.clickhouse.install import install as install_clickhouse_fn

        install_clickhouse_fn(app)

    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, session: SessionView = Depends(require_session)):
        # Mint (or reuse) the CSRF token, then attach the cookie to the
        # TemplateResponse explicitly. Routes that return their own Response
        # bypass FastAPI's dep-injected-Response cookie merge, so we can't
        # rely on Depends(issue_csrf_token) here.
        csrf = mint_csrf_token(request)
        response = TEMPLATES.TemplateResponse(
            request, "index.html", {"user": session.user, "csrf_token": csrf}
        )
        attach_csrf_cookie(request, response, csrf)
        return response

    @app.get("/api/greet")
    async def greet(signals: Signals, session: SessionView = Depends(require_session)) -> DatastarResponse:
        raw = str(signals.get("name") or session.user.display_name).strip()
        name = escape(raw) if raw else "stranger"
        return DatastarResponse(
            SSE.patch_elements(f'<div id="greeting">Hello, <strong>{name}</strong>!</div>')
        )

    @app.get("/api/clock")
    async def clock(_session: SessionView = Depends(require_session)) -> DatastarResponse:
        return DatastarResponse(_clock_stream())

    return app
