"""Read-side helpers for the Authorization feature.

Pure functions that take a Capabilities (or other inputs) and return data
suitable for templates. No FastAPI imports here — keeps testing easy and
makes the layering explicit (routes → service → CH).
"""
from __future__ import annotations

from typing import Any

from iris.auth.rights import Capabilities


def my_access_view(caps: Capabilities) -> dict[str, Any]:
    """Build the template context for the my_access render."""
    return {
        "reader_dbs": sorted(caps.db_reader),
        "writer_dbs": sorted(caps.db_writer),
        "admin_dbs": sorted(caps.db_admin),
        "can_create_database": caps.can_create_database,
        "is_admin": caps.is_admin,
    }
