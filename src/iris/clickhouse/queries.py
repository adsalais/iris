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
from collections.abc import Mapping
from typing import Any

import httpx
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.query import QueryResult

from iris.clickhouse.identifiers import quote_identifier


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
            params[f"param_{k}"] = str(v)
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
