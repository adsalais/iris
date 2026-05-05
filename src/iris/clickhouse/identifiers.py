"""Validation and quoting helpers for ClickHouse SQL identifiers and string literals."""

from __future__ import annotations

import re

_IDENT_RE = re.compile(r"^[a-zA-Z0-9_]+$")


class InvalidIdentifierError(ValueError):
    """Raised when an identifier from external input would have to be escaped to be safe."""


def validate_identifier(name: str, *, kind: str) -> str:
    """Reject anything outside ``[a-zA-Z0-9_]+``. Returns ``name`` unchanged on success.

    ``kind`` is woven into the error message ("username", "role", "database", ...) so
    operators tracing a bad input can see where it entered.
    """
    if not isinstance(name, str) or not _IDENT_RE.fullmatch(name):
        raise InvalidIdentifierError(f"invalid {kind}: {name!r}")
    return name
