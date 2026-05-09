"""Install the Authorization feature into a FastAPI app.

Registers nav contributions, intent specs, the per-feature templates dir,
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

    register_template_dir("authorization", Path(__file__).parent / "templates")

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
    dispatcher.register(IntentSpec(
        feature="auth",
        intent="manage",
        title=lambda params: f"Manage {params.get('database', '')}",
        required=lambda c: c.is_admin or bool(c.db_admin),
    ))
    dispatcher.register(IntentSpec(
        feature="auth",
        intent="create_database",
        title=lambda _params: "Create database",
        required=lambda c: c.is_admin or c.can_create_database,
    ))
    dispatcher.register(IntentSpec(
        feature="auth",
        intent="admin_console",
        title=lambda _params: "Org admin console",
        required=lambda c: c.is_admin,
    ))


def _register_nav(contribs: Contributions) -> None:
    contribs.nav.add(NavGroup(
        label="Authorization",
        entries=(
            NavEntry("My access", on_click=TabIntent("auth", "my_access")),
            NavEntry(
                "Databases I admin",
                visible=lambda c: bool(c.db_admin),
                badge=lambda c: str(len(c.db_admin)) if c.db_admin else None,
                children=lambda c: [
                    NavEntry(
                        db,
                        on_click=TabIntent("auth", "manage", {"database": db}),
                    )
                    for db in sorted(c.db_admin)
                ],
            ),
            NavEntry(
                "Create database",
                visible=lambda c: c.is_admin or c.can_create_database,
                on_click=TabIntent("auth", "create_database"),
            ),
        ),
    ))
    contribs.nav.add(NavGroup(
        label="Org admin",
        visible=lambda c: c.is_admin,
        entries=(
            NavEntry("All users",
                     on_click=TabIntent("auth", "admin_console", {"subtab": "users"})),
            NavEntry("All databases",
                     on_click=TabIntent("auth", "admin_console", {"subtab": "databases"})),
            NavEntry("Row policies",
                     on_click=TabIntent("auth", "admin_console", {"subtab": "policies"})),
            NavEntry("Audit",
                     on_click=TabIntent("auth", "admin_console", {"subtab": "audit"})),
        ),
    ))
