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
    if not _IDENT_RE.fullmatch(name):
        raise InvalidIdentifierError(f"invalid {kind}: {name!r}")
    return name


def quote_identifier(name: str, *, kind: str) -> str:
    """Validate then backtick-quote. The validating regex blocks backticks, so the
    quoted form is always safe to inline into DDL."""
    return f"`{validate_identifier(name, kind=kind)}`"


def quote_string(value: str) -> str:
    """Quote a SQL string literal: backslashes are doubled, then single quotes are doubled."""
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"
