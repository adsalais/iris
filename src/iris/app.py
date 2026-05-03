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

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return TEMPLATES.TemplateResponse(request, "index.html")

    @app.get("/api/greet")
    async def greet(signals: Signals) -> DatastarResponse:
        raw = str(signals.get("name") or "").strip()
        name = escape(raw) if raw else "stranger"
        return DatastarResponse(
            SSE.patch_elements(f'<div id="greeting">Hello, <strong>{name}</strong>!</div>')
        )

    @app.get("/api/clock")
    async def clock() -> DatastarResponse:
        return DatastarResponse(_clock_stream())

    return app


app = build_app()
