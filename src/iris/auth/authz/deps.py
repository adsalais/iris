from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from iris.auth.authz.core import CurrentMapping, resolve_roles
from iris.auth.deps import CurrentUser
from iris.auth.exceptions import AuthForbidden, AuthorizationMisconfigured
from iris.auth.identity import User


async def _current_roles(mapping: CurrentMapping, user: CurrentUser) -> frozenset[str]:
    return resolve_roles(user, mapping)


CurrentRoles = Annotated[frozenset[str], Depends(_current_roles)]


def require_role(role: str):
    async def _check(
        mapping: CurrentMapping,
        roles: CurrentRoles,
        user: CurrentUser,
    ) -> User:
        if role not in mapping.roles:
            raise AuthorizationMisconfigured(role)
        if role not in roles:
            raise AuthForbidden(needed=(role,), have=tuple(sorted(roles)))
        return user

    return _check
