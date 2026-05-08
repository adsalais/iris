from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from iris.auth.session import EMPTY_RIGHTS, Rights


@dataclass(frozen=True, slots=True)
class User:
    subject: str
    username: str
    display_name: str
    groups: tuple[str, ...]


@dataclass(slots=True)
class UserSession:
    """Internal mutable session row from the SQLite store.

    Routes consume the request-scoped immutable :class:`AuthSession` view via
    the alias deps in ``iris.auth.deps``. ``UserSession`` is the row shape that
    sliding-TTL refresh operates on.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    absolute_expires_at: datetime
    data: dict[str, Any] = field(default_factory=dict)
    rights: Rights = EMPTY_RIGHTS


@dataclass(frozen=True, slots=True)
class AuthSession:
    """Request-scoped view of a logged-in session, with derived ``Rights``.

    Built once per request by the auth dep. Routes receive an ``AuthSession``
    via one of the ``Annotated`` alias deps in ``iris.auth.deps``: ``Session``
    (require auth), ``SessionOptional`` (admit None), ``SessionRead`` /
    ``SessionWrite`` / ``SessionDatabaseAdmin`` (database-scoped tier checks
    via ``rights``), ``SessionDatabaseCreator`` / ``SessionAdmin`` (global).

    Frozen except for ``data``: the dict is a per-request snapshot deserialized
    from the SQLite session store. Mutations to the dict do NOT auto-persist —
    call ``await request.app.state.auth_session_store.update_data(session.id,
    session.data)`` to write changes back.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    data: dict[str, Any]
    rights: Rights
