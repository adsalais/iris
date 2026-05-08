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
from datetime import datetime
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


def _marshal_param(v: object) -> str:
    """Marshal a Python value for CH's HTTP ``param_<name>`` query string.

    CH's ``{name:Type}`` placeholders apply server-side type conversion, so
    we hand it a string. ``bool`` must be checked before ``int`` (Python
    ``bool`` subclasses ``int`` and would otherwise stringify to "True");
    ``datetime`` is rendered without the ``+00:00`` UTC suffix so CH parses
    it as ``DateTime``.
    """
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float, str)):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds").replace("+00:00", "")
    raise TypeError(f"unsupported CH param type: {type(v).__name__}")


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
    """
    body = f"EXECUTE AS {quote_identifier(username, kind='username')} {sql}"
    params: dict[str, str] = {"default_format": "JSONEachRow"}
    if database:
        params["database"] = database
    if parameters:
        for k, v in parameters.items():
            params[f"param_{k}"] = _marshal_param(v)
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
