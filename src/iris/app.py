from html import escape
from pathlib import Path

from datastar_py.fastapi import DatastarResponse, datastar_response, read_signals
from datastar_py.fastapi import ServerSentEventGenerator as SSE
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")

app = FastAPI(title="Iris")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return TEMPLATES.TemplateResponse(request, "index.html")


@app.get("/api/greet")
@datastar_response
async def greet(request: Request) -> DatastarResponse:
    signals = await read_signals(request) or {}
    raw = str(signals.get("name") or "").strip()
    name = escape(raw) if raw else "stranger"
    return DatastarResponse(
        SSE.patch_elements(f'<div id="greeting">Hello, <strong>{name}</strong>!</div>')
    )
