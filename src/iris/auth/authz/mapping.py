from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

_ROLE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_ALLOWED_ROLE_KEYS = frozenset({"groups", "users", "includes"})


class RoleMappingError(ValueError):
    """Raised when a role mapping YAML file fails to load or validate."""


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


class _NoDuplicatesSafeLoader(yaml.SafeLoader):
    """SafeLoader subclass that rejects duplicate mapping keys.

    PyYAML's default behavior silently overwrites earlier occurrences,
    which would mask operator typos like two `reader:` blocks.
    """


def _construct_mapping_no_dupes(loader: yaml.Loader, node: yaml.MappingNode) -> dict:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise RoleMappingError(
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_NoDuplicatesSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_no_dupes,
)


def parse(text: str) -> RoleMapping:
    try:
        doc = yaml.load(text, Loader=_NoDuplicatesSafeLoader)
    except yaml.YAMLError as exc:
        raise RoleMappingError(f"YAML syntax error: {exc}") from exc
    except RoleMappingError:
        raise

    if not isinstance(doc, dict):
        raise RoleMappingError("file must contain a top-level mapping")
    if "roles" not in doc:
        raise RoleMappingError("missing required key 'roles'")
    extra = set(doc) - {"roles"}
    if extra:
        raise RoleMappingError(f"unknown top-level key(s): {sorted(extra)}")

    roles_doc = doc["roles"]
    if roles_doc is None:
        roles_doc = {}
    if not isinstance(roles_doc, dict):
        raise RoleMappingError("'roles' must be a mapping")

    roles: dict[str, RoleDef] = {}
    for name, body in roles_doc.items():
        if not isinstance(name, str) or not _ROLE_NAME_RE.fullmatch(name):
            raise RoleMappingError(f"invalid role name {name!r}")
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise RoleMappingError(f"role {name!r}: body must be a mapping")
        unknown = set(body) - _ALLOWED_ROLE_KEYS
        if unknown:
            keys = ", ".join(f"'{k}'" for k in sorted(unknown))
            raise RoleMappingError(
                f"role {name!r}: unknown key {keys}"
            )

        groups = _coerce_string_list(body.get("groups", []), where=f"role {name!r}: groups")
        users = _coerce_string_list(body.get("users", []), where=f"role {name!r}: users")
        includes = _coerce_string_list(
            body.get("includes", []), where=f"role {name!r}: includes"
        )

        roles[name] = RoleDef(
            name=name,
            groups=frozenset(groups),
            users_lower=frozenset(u.lower() for u in users),
            includes=tuple(includes),
        )

    for role in roles.values():
        for inc in role.includes:
            if inc not in roles:
                raise RoleMappingError(
                    f"role {role.name!r}: includes undefined role {inc!r}"
                )

    closure = _compute_closure(roles)
    return RoleMapping(roles=roles, closure=closure)


def _coerce_string_list(value: Any, *, where: str) -> list[str]:
    if not isinstance(value, list):
        raise RoleMappingError(f"{where}: must be a list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RoleMappingError(f"{where}: each entry must be a string, got {type(item).__name__}")
        out.append(item)
    return out


def _compute_closure(roles: dict[str, RoleDef]) -> dict[str, frozenset[str]]:
    closure: dict[str, frozenset[str]] = {}
    visiting: set[str] = set()

    def visit(name: str) -> frozenset[str]:
        if name in closure:
            return closure[name]
        if name in visiting:
            raise RoleMappingError(f"cycle detected involving role {name!r}")
        visiting.add(name)
        result = {name}
        for inc in roles[name].includes:
            result |= visit(inc)
        visiting.remove(name)
        frozen = frozenset(result)
        closure[name] = frozen
        return frozen

    for name in roles:
        visit(name)
    return closure
