"""Wire the shell into a FastAPI app.

Order matters: ``iris.shell.install`` must be called BEFORE any feature's
install (features assume ``app.state.contributions`` and
``app.state.intent_dispatcher`` exist). ``build_app()`` enforces:
auth → clickhouse → shell → features.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from iris.shell.contributions import Contributions
from iris.shell.intent_dispatch import IntentDispatcher
from iris.templates import register_template_dir


def install(app: FastAPI) -> None:
    app.state.contributions = Contributions()
    app.state.intent_dispatcher = IntentDispatcher()

    register_template_dir(Path(__file__).parent / "templates")

    app.mount(
        "/static/shell",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="shell-static",
    )

    from iris.shell.routes import install_routes
    install_routes(app)
