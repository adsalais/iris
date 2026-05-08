"""Shared `Jinja2Templates` instance for both root-level (`index.html`)
and auth-flow (`auth/*.html`) templates. Imported by `iris.app:build_app`
and re-exposed on `app.state.templates` so exception handlers and providers
can render without re-creating the loader.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
