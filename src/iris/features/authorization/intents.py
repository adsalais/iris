"""Intent render functions for the Authorization feature.

The shell's per-feature route /feature/auth/{tab_id}/render dispatches
on tab.intent into RENDER_BY_INTENT. Phase 3 adds my_access; Phase 4 adds
manage; Phase 5 adds create_database; Phase 6 adds admin_console (and
its four sub-tab handlers).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import Request, Response

if TYPE_CHECKING:
    from iris.auth.views import AuthSession
    from iris.shell.tabs import TabRecord

IntentHandler = Callable[[Request, "AuthSession", "TabRecord"], Awaitable[Response]]

RENDER_BY_INTENT: dict[str, IntentHandler] = {}
