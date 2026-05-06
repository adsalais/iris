from __future__ import annotations

from fastapi import Depends

from iris.auth.authz.core import CurrentMapping
from iris.auth.deps import require_session
from iris.auth.exceptions import AuthForbidden, AuthorizationMisconfigured
from iris.auth.session import Session


def require_role(role: str):
    async def _check(
        mapping: CurrentMapping,
        session: Session = Depends(require_session),
    ) -> Session:
        if role not in mapping.roles:
            raise AuthorizationMisconfigured(role)
        if role not in session.roles:
            raise AuthForbidden(
                needed=(role,), have=tuple(sorted(session.roles))
            )
        return session

    return _check
