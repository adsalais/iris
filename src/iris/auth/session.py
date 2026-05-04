from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from iris.auth.identity import User


@dataclass(frozen=True, slots=True)
class Session:
    """Request-scoped view of a logged-in session.

    Built once per request by the auth dep. Routes receive it via the
    ``Session`` or ``OptionalSession`` annotated aliases from
    ``iris.auth.deps``.

    Frozen except for ``data``, which is the SAME ``dict`` object as
    ``UserSession.data`` in the session store. This means
    ``session.data[key] = value`` writes through to the store with no
    commit step. All other fields are immutable from the route's view.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    data: dict[str, Any]
    roles: frozenset[str]
