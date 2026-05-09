"""Validation and quoting helpers for ClickHouse SQL identifiers and string literals."""

from __future__ import annotations

import hashlib
import re
from typing import Final

_IDENT_RE = re.compile(r"^[a-zA-Z0-9_]+$")
_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")

# CH's FixedString(N) type marker. Module-private; consumers go through
# `is_fixed_string_type` below — that's the only public surface.
_FIXED_STRING_RE: Final = re.compile(r"^FixedString\(\d+\)$")


def is_fixed_string_type(ch_type: str) -> bool:
    """Return True iff ``ch_type`` is a CH ``FixedString(N)`` literal type
    string (e.g. ``"FixedString(16)"``).

    Used by row-policy filter construction (``iris.clickhouse.policies``)
    and the typed param marshaller (``iris.clickhouse.queries``) to detect
    FixedString variants of (Array of) string-like types. The regex itself
    is module-private; callers consume this predicate so the implementation
    can change (e.g. adding ``LowCardinality(FixedString(N))``) without
    touching every call site.
    """
    return _FIXED_STRING_RE.match(ch_type) is not None

# Suffixes iris synthesizes for role names: `<username>_USER`, `<group>_GRP`,
# `<database>_DBADMIN/_DBWRITER/_DBREADER`. External-input identifiers must
# not end in these — otherwise the post-login role-graph walk in
# `iris.clickhouse.capabilities.derive_capabilities` cannot disambiguate
# whether a role is a tier role or an external name that happens to look
# like one. See `_SUFFIX_CHECKED_KINDS` below for the kinds where this
# rule applies.
_RESERVED_SUFFIXES: Final = ("_USER", "_GRP", "_DBADMIN", "_DBWRITER", "_DBREADER")

# Identifier `kind` values that come from external input (auth provider
# claims, route path / query parameters, operator config). Synthesized
# names like `<db>_DBADMIN` legitimately end in reserved suffixes, so
# `kind in {"role", "policy", "table", "column"}` is exempt.
_SUFFIX_CHECKED_KINDS: Final = frozenset({"database", "username", "group"})


class InvalidIdentifierError(ValueError):
    """Raised when an identifier from external input would have to be escaped to be safe."""


def validate_identifier(name: str, *, kind: str) -> str:
    """Reject anything outside ``[a-zA-Z0-9_]+``. Returns ``name`` unchanged on success.

    For ``kind`` in ``{"database", "username", "group"}``, additionally rejects
    names ending in iris's reserved role suffixes (``_USER``, ``_GRP``,
    ``_DBADMIN``, ``_DBWRITER``, ``_DBREADER``). These suffixes are reserved
    for synthesized role names (e.g. ``<username>_USER``,
    ``<database>_DBADMIN``); allowing external input to also end with them
    creates ambiguity in the post-login role-graph walk in
    ``iris.clickhouse.capabilities.derive_capabilities``.

    Other ``kind`` values (``role``, ``policy``, ``table``, ``column``) skip
    the suffix check, since synthesized role names like ``<db>_DBADMIN``
    legitimately end in those suffixes.

    ``kind`` is woven into the error message ("username", "role", "database",
    ...) so operators tracing a bad input can see where it entered.
    """
    if not _IDENT_RE.fullmatch(name):
        raise InvalidIdentifierError(f"invalid {kind}: {name!r}")
    if kind in _SUFFIX_CHECKED_KINDS:
        for suffix in _RESERVED_SUFFIXES:
            if name.endswith(suffix):
                raise InvalidIdentifierError(
                    f"invalid {kind}: {name!r} ends with reserved iris role suffix {suffix!r}"
                )
    return name


def quote_identifier(name: str, *, kind: str) -> str:
    """Validate then backtick-quote. The validating regex blocks backticks, so the
    quoted form is always safe to inline into DDL."""
    return f"`{validate_identifier(name, kind=kind)}`"


def quote_sql_literal(value: str) -> str:
    """Quote a SQL string literal for inline use in DDL or query text.

    Backslashes are doubled, then single quotes are doubled (CH's standard
    string-literal escape grammar). Use for values that appear directly in
    query text, e.g. row-policy USING clauses: ``USING col = 'value'``.

    Renamed from ``quote_string`` (atomic rename, no alias).
    """
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"


def quote_sql_array_element(value: str) -> str:
    """Quote a SQL string for use as an element in a CH array literal.

    CH array literal syntax requires single-quoted string elements with
    backslash escaping (NOT doubled-quote escaping — that grammar is
    rejected inside ``[...]``). Backslashes are doubled, then single
    quotes are backslash-escaped. Use ONLY for values placed inside
    ``[...]`` array literals; for inline String literals use
    ``quote_sql_literal``.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
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


def validate_dict_name(name: str) -> str:
    """Validate ``<dict>`` or ``<db>.<dict>``. Return ``name`` unchanged on success.

    Both halves of a dotted name go through ``validate_identifier`` (using
    ``kind="database"`` for the left half and ``kind="table"`` for the right).
    More than one dot, or any segment failing identifier validation, raises
    ``InvalidIdentifierError``.

    Used by ``add_row_dict_policy`` to gate ``dictionary`` parameters before
    they're emitted as a SQL string literal in the policy USING clause.
    """
    parts = name.split(".")
    if len(parts) == 1:
        validate_identifier(parts[0], kind="table")
    elif len(parts) == 2:
        validate_identifier(parts[0], kind="database")
        validate_identifier(parts[1], kind="table")
    else:
        msg = f"dictionary name must be '<dict>' or '<db>.<dict>'; got {name!r}"
        raise InvalidIdentifierError(msg)
    return name


def dict_policy_name(
    database: str,
    table: str,
    role: str,
    value: str,
    dictionary: str,
    authorisations: str,
    auth_id: str,
) -> str:
    """Build a row-policy name for a dict-keyed policy.

    Same shape as ``policy_name``: ``<db>_<table>_<role>_<slug>_<16charhash>``.
    The 16-char SHA-256 prefix incorporates ``value | dictionary |
    authorisations | auth_id`` (NUL-separated) so two dict policies on the
    same ``(database, table, role, value)`` tuple but using different
    dictionaries / attributes / auth_id columns get distinct names.

    The slug is derived from ``value`` only (matching the scalar
    ``policy_name`` behavior) so the human-readable portion of the name
    stays recognisable.
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(role, kind="role")
    slug = _SLUG_RE.sub("_", value).strip("_") or "v"
    digest_input = f"{value}\0{dictionary}\0{authorisations}\0{auth_id}"
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]
    return f"{database}_{table}_{role}_{slug}_{digest}"
