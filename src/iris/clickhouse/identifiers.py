"""Validation and quoting helpers for ClickHouse SQL identifiers and string literals."""

from __future__ import annotations

import hashlib
import re

_IDENT_RE = re.compile(r"^[a-zA-Z0-9_]+$")
_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")


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


def policy_name(database: str, table: str, role: str, value: str) -> str:
    """Build a row-policy name: ``<database>_<table>_<role>_<slug>_<16charhash>``.

    ``database``, ``table``, ``role`` are validated as identifiers. ``value`` is
    treated as opaque — non-[a-zA-Z0-9_] characters collapse to '_' for the
    slug, and a 16-character SHA-256 hex digest of the raw value is appended
    so distinct values that happen to share a slug (``'EU/UK'`` vs ``'EU UK'``)
    get distinct names.

    The 16-char (64-bit) digest matters because ``add_row_policy`` issues
    ``CREATE ROW POLICY IF NOT EXISTS`` — a hash collision on the same
    ``(database, table, role)`` triple would silently drop the second
    policy. 64 bits puts the birthday bound around 4 billion entries.
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(role, kind="role")
    slug = _SLUG_RE.sub("_", value).strip("_") or "v"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{database}_{table}_{role}_{slug}_{digest}"
