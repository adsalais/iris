from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from iris.auth.identity import User


@dataclass(frozen=True, slots=True)
class SessionView:
    """Request-scoped view of a logged-in session.

    Built once per request by the auth dep. Routes normally receive a
    ``SessionView`` via the ``Session`` (required) or ``OptionalSession``
    (optional) annotated aliases from ``iris.auth.deps`` — those aliases
    bake in ``Depends(...)`` metadata so a route can write
    ``session: Session`` and the dep system fills in a ``SessionView``
    automatically.

    Role-gated routes that combine the type with an explicit ``Depends``
    (e.g. ``= Depends(require_role("admin"))``) can't reuse the alias —
    FastAPI rejects ``Annotated[X, Depends(a)]`` plus ``= Depends(b)``
    on the same parameter — so they import the bare ``SessionView``
    type from ``iris.auth.session``.

    Frozen except for ``data``: the dict is a per-request snapshot
    deserialized from the SQLite session store. Mutations to the dict do
    NOT auto-persist — call
    ``await request.app.state.auth_session_store.update_data(session.id,
    session.data)`` to write changes back.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    data: dict[str, Any]
    roles: frozenset[str]
