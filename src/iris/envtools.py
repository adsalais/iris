"""Environment-variable parsing helpers shared across subsystem configs.

Each subsystem used to define its own ``_get_bool`` / ``_required`` /
``_get_int`` privately. Hoisting them here unifies the accepted token
set (true/false/yes/no/on/off/1/0 for booleans) and removes the
divergence where, for example, ``CLICKHOUSE_SECURE=yes`` was rejected
but ``COOKIE_SECURE=yes`` accepted.

Boolean semantics:
- ``get_bool(name)`` raises if the var is missing or empty.
- ``get_bool(name, default=...)`` returns the default when missing or
  empty, and raises only when the value is non-empty but unrecognized.

Integer / string semantics follow the same shape: pass ``default=...``
to make the var optional.
"""
from __future__ import annotations

import os
from typing import overload

_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})


def required(name: str) -> str:
    """Return ``os.environ[name].strip()``. Raise if missing or empty."""
    val = os.environ.get(name, "").strip()
    if not val:
        raise ValueError(f"{name} is required")
    return val


@overload
def get_bool(name: str) -> bool: ...
@overload
def get_bool(name: str, *, default: bool) -> bool: ...
def get_bool(name: str, *, default: bool | None = None) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        if default is None:
            raise ValueError(f"{name} is required")
        return default
    v = raw.strip().lower()
    if v in _TRUE_TOKENS:
        return True
    if v in _FALSE_TOKENS:
        return False
    raise ValueError(f"{name} must be a boolean (true/false), got {raw!r}")


@overload
def get_int(name: str) -> int: ...
@overload
def get_int(name: str, *, default: int) -> int: ...
def get_int(name: str, *, default: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        if default is None:
            raise ValueError(f"{name} is required")
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from e


def split_csv(raw: str) -> tuple[str, ...]:
    """Split a comma-separated string into a tuple, dropping empty pieces."""
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def split_ws(raw: str) -> tuple[str, ...]:
    """Split a whitespace-separated string into a tuple, dropping empties."""
    return tuple(p for p in raw.split() if p)
