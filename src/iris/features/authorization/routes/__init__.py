"""APIRouter for the Authorization feature.

Mounted at ``/feature/authorization`` by ``install.py``. Each per-area
sub-module owns its own thin ``APIRouter()`` (no prefix); this package
combines them into the single feature-level router exported as ``router``.

Sub-modules by concern:

- ``render``        — per-intent GET handlers (my_access / manage /
                      create_database / admin_console).
- ``members``       — tier-member grant/revoke (POST + DELETE).
- ``policies``      — row-policy add/revoke (POST + DELETE).
- ``admin_console`` — admin sub-tab GETs (users / databases / policies /
                      audit) + the reprovision POST.
- ``lifecycle``     — create_database submit + delete_database.

``_common`` holds the shared render helpers.
"""
from __future__ import annotations

from fastapi import APIRouter

from iris.features.authorization.routes import (
    admin_console,
    lifecycle,
    members,
    policies,
    render,
)

router = APIRouter(prefix="/feature/authorization")
router.include_router(render.router)
router.include_router(members.router)
router.include_router(policies.router)
router.include_router(admin_console.router)
router.include_router(lifecycle.router)

__all__ = ["router"]
