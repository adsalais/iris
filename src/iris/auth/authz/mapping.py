"""Value types and graph helpers for the authz role mapping.

The YAML parser used to live here. After the SQLite cutover, the store
in iris.auth.authz.store builds RoleDef / RoleMapping by querying the DB,
then computes the closure via compute_closure below. The regex and
RoleMappingError are reused by store.py and bootstrap.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ROLE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class RoleMappingError(ValueError):
    """Raised when the role mapping fails to load or validate."""


@dataclass(frozen=True, slots=True)
class RoleDef:
    name: str
    groups: frozenset[str]
    users_lower: frozenset[str]
    includes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoleMapping:
    roles: dict[str, RoleDef]
    closure: dict[str, frozenset[str]]


def compute_closure(roles: dict[str, RoleDef]) -> dict[str, frozenset[str]]:
    closure: dict[str, frozenset[str]] = {}
    visiting: set[str] = set()

    def visit(name: str) -> frozenset[str]:
        if name in closure:
            return closure[name]
        if name in visiting:
            raise RoleMappingError(f"cycle detected involving role {name!r}")
        visiting.add(name)
        try:
            result = {name}
            for inc in roles[name].includes:
                result |= visit(inc)
        finally:
            visiting.discard(name)
        frozen = frozenset(result)
        closure[name] = frozen
        return frozen

    for name in roles:
        visit(name)
    return closure
