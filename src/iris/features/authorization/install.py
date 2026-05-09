"""Install the Authorization feature into a FastAPI app.

Registers nav contributions (Authorization + Org admin groups), intent
specs (my_access at this Phase 3 point; manage / create_database /
admin_console land in subsequent phases), the per-feature templates dir,
and mounts the feature's APIRouter at /feature/auth.

Depends on app.state.contributions and app.state.intent_dispatcher
existing — call AFTER iris.shell.install.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from iris.shell.contributions import (
    Contributions,
    NavEntry,
    NavGroup,
    TabIntent,
)
from iris.shell.intent_dispatch import IntentDispatcher, IntentSpec
from iris.templates import register_template_dir


def install(app: FastAPI) -> None:
    contribs: Contributions = app.state.contributions
    dispatcher: IntentDispatcher = app.state.intent_dispatcher

    register_template_dir(Path(__file__).parent / "templates")

    _register_intents(dispatcher)
    _register_nav(contribs)

    from iris.features.authorization.routes import router
    app.include_router(router)


def _register_intents(dispatcher: IntentDispatcher) -> None:
    dispatcher.register(IntentSpec(
        feature="auth",
        intent="my_access",
        title=lambda _params: "My access",
        required=lambda _c: True,
    ))


def _register_nav(contribs: Contributions) -> None:
    contribs.nav.add(NavGroup(
        label="Authorization",
        entries=(
            NavEntry("My access", on_click=TabIntent("auth", "my_access")),
            # Databases I admin / Create database land in Phase 4 / Phase 5
            # alongside the manage / create_database intents.
        ),
    ))
    contribs.nav.add(NavGroup(
        label="Org admin",
        visible=lambda c: c.is_admin,
        entries=(),
    ))
