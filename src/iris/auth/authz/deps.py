from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from iris.auth.authz.core import CurrentMapping, resolve_roles
from iris.auth.deps import CurrentUser, Session
from iris.auth.exceptions import AuthForbidden, AuthorizationMisconfigured
from iris.auth.session import Session as _SessionT


async def _current_roles(mapping: CurrentMapping, user: CurrentUser) -> frozenset[str]:
    return resolve_roles(user, mapping)


CurrentRoles = Annotated[frozenset[str], Depends(_current_roles)]


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
