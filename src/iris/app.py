from html import escape
from pathlib import Path
from typing import Annotated, Any

from datastar_py.fastapi import DatastarResponse, read_signals
from datastar_py.fastapi import ServerSentEventGenerator as SSE
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")

app = FastAPI(title="Iris")


async def _signals(request: Request) -> dict[str, Any]:
    return await read_signals(request) or {}


Signals = Annotated[dict[str, Any], Depends(_signals)]


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
