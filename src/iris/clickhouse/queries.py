"""Async ClickHouse query helpers.

Two transport stories:

- ``query_as_user`` POSTs to CH's HTTP endpoint via ``httpx`` so we can
  prepend ``EXECUTE AS <user>`` to the body. clickhouse-connect would
  rewrite the body with ``FORMAT Native`` and break the impersonation.
- ``query_as_service`` runs over clickhouse-connect's ``Client`` (no
  impersonation), wrapped in ``asyncio.to_thread`` to stay off the
  event loop.

Session methods (in ``iris.auth.identity``) are the only callers.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import httpx
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.query import QueryResult

from iris.clickhouse.identifiers import quote_identifier

_PLACEHOLDER_RE = re.compile(r"\{(\w+):([^}]+)\}")


def _parse_placeholder_types(sql: str) -> dict[str, str]:
    """Extract every ``{name:Type}`` placeholder from ``sql``.

    Returns a ``name -> type-string`` map. Type strings are stripped of
    leading/trailing whitespace; interior whitespace (e.g. inside
    ``Decimal(10, 2)``) is preserved.

    Raises ``ValueError`` if the same name appears with conflicting types
    in two different placeholders. The same name with the same type is
    allowed (collapses to one entry).

    The regex relies on CH type strings never containing ``}``; nested
    parametric types like ``Array(Nullable(Int32))`` stay inside parens.
    """
    found: dict[str, str] = {}
    for m in _PLACEHOLDER_RE.finditer(sql):
        name, ch_type = m.group(1), m.group(2).strip()
        prev = found.get(name)
        if prev is not None and prev != ch_type:
            raise ValueError(
                f"conflicting CH types for placeholder {name!r}: {prev!r} vs {ch_type!r}"
            )
        found[name] = ch_type
    return found


_DATETIME64_RE = re.compile(r"^DateTime64\((\d+)\)$")
_DATETIME_TZ_RE = re.compile(r"^DateTime(?:\([^)]*\))?$")
_FIXED_STRING_RE = re.compile(r"^FixedString\(\d+\)$")
_INT_TYPES = frozenset(
    {"Int8", "Int16", "Int32", "Int64", "UInt8", "UInt16", "UInt32", "UInt64"}
)
_FLOAT_TYPES = frozenset({"Float32", "Float64"})


def _format_datetime_seconds(v: datetime) -> str:
    """Format ``v`` as ``YYYY-MM-DD HH:MM:SS`` in UTC.

    Naive datetimes are treated as UTC (matches iris's repo-wide
    convention of ``datetime.now(UTC)``); aware datetimes are converted.
    """
    if v.tzinfo is None:
        v = v.replace(tzinfo=UTC)
    else:
        v = v.astimezone(UTC)
    return v.strftime("%Y-%m-%d %H:%M:%S")


def _format_datetime64(v: datetime, precision: int) -> str:
    """Format ``v`` as ``YYYY-MM-DD HH:MM:SS.fff…`` with ``precision``
    fractional digits, in UTC. Python ``datetime`` carries microsecond
    precision (6 digits); higher precisions right-pad with zeros, lower
    precisions truncate."""
    seconds = _format_datetime_seconds(v)
    if precision == 0:
        return seconds
    micros = v.microsecond if v.tzinfo is None else v.astimezone(UTC).microsecond
    # 6-digit microsecond field; pad on the right to up to 9 digits, then
    # take the first ``precision`` digits.
    fractional = f"{micros:06d}".ljust(9, "0")[:precision]
    return f"{seconds}.{fractional}"


def _format_array(v: object, inner_type: str) -> str:
    if not isinstance(v, (list, tuple)):
        raise TypeError(
            f"Array({inner_type}) expects list or tuple, got {type(v).__name__}"
        )
    # Reject array-of-date/datetime types: array literals require these to
    # be quoted, but _marshal_array_element only quotes String/FixedString.
    # Adding quoting for date/datetime is a future enhancement; until then,
    # fail loudly so callers don't get an opaque CH-side rejection.
    bare = inner_type.strip()
    if bare.startswith("Nullable(") and bare.endswith(")"):
        bare = bare[len("Nullable("):-1].strip()
    if (
        bare in ("Date", "Date32")
        or _DATETIME64_RE.match(bare)
        or _DATETIME_TZ_RE.match(bare)
    ):
        raise TypeError(
            f"Array({inner_type}) is not supported: array-element quoting"
            + " for date/datetime types is not implemented"
        )
    parts = [_marshal_array_element(e, inner_type) for e in v]
    return "[" + ",".join(parts) + "]"


def _marshal_array_element(v: object, ch_type: str) -> str:
    """Like ``_marshal_param`` but quotes Strings (CH's array literal
    syntax requires single-quoted string elements). Nullable inside an
    array still emits the bare ``NULL`` token."""
    ch_type = ch_type.strip()
    if ch_type.startswith("Nullable(") and ch_type.endswith(")"):
        if v is None:
            return "NULL"
        return _marshal_array_element(v, ch_type[len("Nullable(") : -1])
    if ch_type == "String" or _FIXED_STRING_RE.match(ch_type):
        if not isinstance(v, str):
            raise TypeError(f"{ch_type} expects str, got {type(v).__name__}")
        # Backslash first, then single quote — order matters.
        escaped = v.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    return _marshal_param(v, ch_type)


def _marshal_param(v: object, ch_type: str) -> str:
    """Marshal a Python value to the wire format CH expects for the
    declared placeholder type ``ch_type``.

    The full grammar is in
    ``docs/superpowers/specs/2026-05-09-typed-param-marshaller-design.md``.

    Briefly: dispatches on the declared CH type, peeling ``Nullable(...)``
    and ``Array(...)`` wrappers recursively. Raises ``TypeError`` for
    unsupported types and value/type mismatches.
    """
    ch_type = ch_type.strip()

    # Nullable(T): None → "NULL", else recurse on inner T.
    if ch_type.startswith("Nullable(") and ch_type.endswith(")"):
        inner = ch_type[len("Nullable(") : -1]
        if v is None:
            return "NULL"
        return _marshal_param(v, inner)

    # Array(T): bracket-comma-join, with strings quoted inside.
    if ch_type.startswith("Array(") and ch_type.endswith(")"):
        inner = ch_type[len("Array(") : -1]
        return _format_array(v, inner)

    # bool — checked before any int-like branch because bool subclasses int.
    if ch_type == "Bool":
        if not isinstance(v, bool):
            raise TypeError(f"Bool expects bool, got {type(v).__name__}")
        return "true" if v else "false"

    # Numbers — must reject bool first.
    if ch_type in _INT_TYPES:
        if isinstance(v, bool):
            raise TypeError(f"{ch_type} rejects bool (use Bool type)")
        if not isinstance(v, int):
            raise TypeError(f"{ch_type} expects int, got {type(v).__name__}")
        return str(v)
    if ch_type in _FLOAT_TYPES:
        if isinstance(v, bool):
            raise TypeError(f"{ch_type} rejects bool")
        if not isinstance(v, (int, float)):
            raise TypeError(
                f"{ch_type} expects int or float, got {type(v).__name__}"
            )
        return str(v)

    # String / FixedString(N): bare passthrough; quoting only happens inside arrays.
    if ch_type == "String" or _FIXED_STRING_RE.match(ch_type):
        if not isinstance(v, str):
            raise TypeError(f"{ch_type} expects str, got {type(v).__name__}")
        return v

    # Date / Date32 — accepts both date and datetime, formats YYYY-MM-DD.
    if ch_type in ("Date", "Date32"):
        if isinstance(v, datetime):
            return v.date().isoformat()
        if isinstance(v, date):
            return v.isoformat()
        raise TypeError(f"{ch_type} expects date or datetime, got {type(v).__name__}")

    # DateTime64(p) — must come BEFORE DateTime since the latter regex
    # matches "DateTime" with optional "(...)".
    m64 = _DATETIME64_RE.match(ch_type)
    if m64:
        if not isinstance(v, datetime):
            raise TypeError(f"{ch_type} expects datetime, got {type(v).__name__}")
        return _format_datetime64(v, int(m64.group(1)))

    # DateTime / DateTime('TZ') — second precision in UTC.
    if _DATETIME_TZ_RE.match(ch_type):
        if not isinstance(v, datetime):
            raise TypeError(f"{ch_type} expects datetime, got {type(v).__name__}")
        return _format_datetime_seconds(v)

    raise TypeError(f"unsupported CH param type: {ch_type!r}")


async def query_as_user(
    http_client: httpx.AsyncClient,
    *,
    username: str,
    sql: str,
    parameters: Mapping[str, Any] | None = None,
    database: str | None = None,
) -> list[dict[str, Any]]:
    """Run ``sql`` on ClickHouse impersonated as ``username``.

    Sends ``EXECUTE AS <username> <sql>`` to the CH HTTP endpoint with
    ``default_format=JSONEachRow`` (and ``database=<database>`` when
    supplied, so unqualified table names resolve against that schema).

    Each parameter is marshaled according to the CH type declared in the
    SQL via ``{name:Type}``. A parameter passed without a matching
    placeholder raises ``ValueError`` — likely a caller-side typo.
    """
    body = f"EXECUTE AS {quote_identifier(username, kind='username')} {sql}"
    params: dict[str, str] = {"default_format": "JSONEachRow"}
    if database:
        params["database"] = database
    if parameters:
        type_map = _parse_placeholder_types(sql)
        for k, v in parameters.items():
            if k not in type_map:
                raise ValueError(
                    f"parameter {k!r} has no {{{k}:Type}} placeholder in the SQL"
                )
            params[f"param_{k}"] = _marshal_param(v, type_map[k])
    response = await http_client.post("/", params=params, content=body)
    response.raise_for_status()
    text = response.text.strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines() if line]


async def query_as_service(
    client: Client,
    *,
    sql: str,
    parameters: Mapping[str, Any] | None = None,
    database: str | None = None,
) -> QueryResult:
    """Run ``sql`` as the service identity (no impersonation). When
    ``database`` is supplied, clickhouse-connect's ``database=`` kwarg
    sets the default schema for unqualified names."""
    kwargs: dict[str, Any] = {}
    if parameters:
        kwargs["parameters"] = dict(parameters)
    if database:
        kwargs["database"] = database
    return await asyncio.to_thread(client.query, sql, **kwargs)
