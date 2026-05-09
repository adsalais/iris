"""Identity dataclasses for the auth subsystem.

- ``User``: frozen, slotted, externally-derived identity (subject, username,
  display name, groups). Returned by every provider's ``authenticate``.
- ``StoredSession``: mutable row-shape persisted in the SQLite session store.
  The sliding-TTL refresh logic in ``iris.auth.store.SessionStore`` operates
  on this type. Routes never see ``StoredSession`` directly; they receive
  the request-scoped ``AuthSession`` view (and its subclasses) from
  ``iris.auth.views`` via the alias deps in ``iris.auth.deps``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities


@dataclass(frozen=True, slots=True)
class User:
    subject: str
    username: str
    display_name: str
    groups: tuple[str, ...]


@dataclass(slots=True)
class StoredSession:
    """Internal mutable session row from the SQLite store.

    Routes consume the request-scoped immutable :class:`AuthSession` view via
    the alias deps in ``iris.auth.deps``. ``StoredSession`` is the row shape
    that sliding-TTL refresh operates on.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    absolute_expires_at: datetime
    data: dict[str, Any] = field(default_factory=dict)
    capabilities: Capabilities = EMPTY_CAPABILITIES
