from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from iris.auth.authz.mapping import RoleMapping
from iris.auth.identity import User


def resolve_roles(user: User, mapping: RoleMapping) -> frozenset[str]:
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


async def current_mapping(request: Request) -> RoleMapping:
    return await request.app.state.authz_store.get_mapping()


CurrentMapping = Annotated[RoleMapping, Depends(current_mapping)]
