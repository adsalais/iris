from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from iris.auth.authz.mapping import RoleMapping
from iris.auth.deps import CurrentUser
from iris.auth.exceptions import AuthForbidden, AuthorizationMisconfigured
from iris.auth.identity import User


def _resolve_roles(user: User, mapping: RoleMapping) -> frozenset[str]:
    base: set[str] = set()
    username_lower = user.username.lower()
    user_groups = set(user.groups)
    for role_name, role_def in mapping.roles.items():
        if username_lower in role_def.users_lower:
            base.add(role_name)
        elif role_def.groups & user_groups:
            base.add(role_name)
    effective: set[str] = set()
    for r in base:
        effective |= mapping.closure[r]
    return frozenset(effective)


async def _current_mapping(request: Request) -> RoleMapping:
    return request.app.state.authz_loader.get()


_CurrentMapping = Annotated[RoleMapping, Depends(_current_mapping)]


async def _current_roles(mapping: _CurrentMapping, user: CurrentUser) -> frozenset[str]:
    return _resolve_roles(user, mapping)


CurrentRoles = Annotated[frozenset[str], Depends(_current_roles)]


def require_role(role: str):
    async def _check(
        mapping: _CurrentMapping,
        roles: CurrentRoles,
        user: CurrentUser,
    ) -> User:
        if role not in mapping.roles:
            raise AuthorizationMisconfigured(role)
        if role not in roles:
            raise AuthForbidden(needed=(role,), have=tuple(sorted(roles)))
        return user

    return _check
