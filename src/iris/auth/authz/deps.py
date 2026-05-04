from __future__ import annotations

from iris.auth.authz.core import CurrentMapping
from iris.auth.deps import Session
from iris.auth.exceptions import AuthForbidden, AuthorizationMisconfigured
from iris.auth.session import Session as _SessionT


def require_role(role: str):
    async def _check(session: Session, mapping: CurrentMapping) -> _SessionT:
        if role not in mapping.roles:
            raise AuthorizationMisconfigured(role)
        if role not in session.roles:
            raise AuthForbidden(
                needed=(role,), have=tuple(sorted(session.roles))
            )
        return session

    return _check
