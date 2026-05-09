"""URL builders shared between the shell template and feature integrations.

The shell template (and its SSE helpers) emit URLs to render tab panels;
each feature's render routes follow a uniform convention:

    /feature/<feature>/<tab_id>/<intent>?<params encoded as query>

This module exposes ``tab_render_url`` as a Jinja global (registered by
``iris.app.build_app`` after ``init_templates()``) so templates can call
it without each feature needing its own URL builder.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote


def tab_render_url(tab: dict[str, Any]) -> str:
    """Build the GET URL the panel hits to render its content.

    Tab params (the per-tab dict stored in ``session.data``) become the
    URL's query string, supporting auto-injection into typed FastAPI deps
    (e.g. ``database`` → ``SessionDatabaseAdmin``).
    """
    base = f"/feature/{tab['feature']}/{tab['id']}/{tab['intent']}"
    params = tab.get("params") or {}
    if not params:
        return base
    qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    return f"{base}?{qs}"
