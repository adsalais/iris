"""Per-request ClickHouse handle classes used by FastAPI route handlers.

Two handle types share the same per-request shape but expose different surfaces:

- :class:`ClickHouseHandle` — for any logged-in user. Only ``query_as_user`` is
  exposed. The query runs as the user via ClickHouse's per-query
  ``EXECUTE AS`` prefix.
- :class:`ClickHouseAdminHandle` — gated on the ``clickhouse_admin`` role.
  Adds ``query_as_service`` (no impersonation) plus async wrappers around the
  module-level admin/audit functions.

Why two HTTP transports? ClickHouse's ``EXECUTE AS user <SELECT>`` body grammar
rejects ``FORMAT`` clauses, but ``clickhouse-connect``'s ``query()`` always
appends ``FORMAT Native``. So impersonated queries go through a raw
``httpx.AsyncClient`` with ``?default_format=JSONEachRow`` as a URL parameter
(which the server *does* honor). Non-impersonated queries keep using
``clickhouse-connect`` for the polished ``QueryResult``.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.query import QueryResult

from iris.clickhouse.audit import (
    role_grants,
    role_row_policies,
    table_row_policies,
    user_grants,
    user_role_memberships,
    user_row_policies,
)
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.grants import (
    grant_insert_update_to_table,
    grant_select_to_database,
)
from iris.clickhouse.identifiers import quote_identifier
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
from iris.clickhouse.users import init_user_rights


class ClickHouseHandle:
    """Per-request handle for any logged-in user.

    Exposes only ``query_as_user``, which prepends ``EXECUTE AS <quoted_username>``
    to the SQL via raw HTTP so the query runs under the user's CH identity.
    Service-identity queries and admin functions are not exposed here — see
    :class:`ClickHouseAdminHandle`.

    Returns ``list[dict[str, Any]]`` (parsed JSONEachRow). Numeric types are
    preserved by ClickHouse's JSON encoder, but column-type metadata is lost
    relative to ``QueryResult``; if you need types or column ordering, query
    via the admin handle's ``query_as_service`` (which uses clickhouse-connect).
    """

    def __init__(
        self,
        *,
        client: Client,
        http_client: httpx.AsyncClient,
        username: str,
    ) -> None:
        self._client = client
        self._http_client = http_client
        self._username_quoted = quote_identifier(username, kind="username")
        self._username = username

    async def query_as_user(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        body = f"EXECUTE AS {self._username_quoted} {sql}"
        params: dict[str, str] = {"default_format": "JSONEachRow"}
        if parameters:
            for k, v in parameters.items():
                params[f"param_{k}"] = str(v)

        response = await self._http_client.post("/", params=params, content=body)
        response.raise_for_status()
        text = response.text.strip()
        if not text:
            return []
        return [json.loads(line) for line in text.splitlines() if line]


class ClickHouseAdminHandle(ClickHouseHandle):
    """Admin-capable handle for routes gated on the ``clickhouse_admin`` role.

    Adds service-identity queries (no impersonation) and async wrappers around
    the existing module-level admin/audit functions. ``query_as_user`` is
    inherited from the parent class.
    """

    def __init__(
        self,
        *,
        client: Client,
        http_client: httpx.AsyncClient,
        username: str,
        settings: ClickHouseSettings,
    ) -> None:
        super().__init__(client=client, http_client=http_client, username=username)
        self._settings = settings

    async def query_as_service(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> QueryResult:
        return await asyncio.to_thread(
            self._client.query,
            sql,
            parameters=dict(parameters) if parameters else None,
        )

    async def reprovision_user(self, *, username: str, groups: list[str]) -> None:
        await asyncio.to_thread(
            init_user_rights,
            self._client,
            username=username,
            groups=groups,
            settings=self._settings,
        )

    async def grant_select_to_database(self, *, database: str, role: str) -> None:
        await asyncio.to_thread(
            grant_select_to_database,
            self._client,
            database=database,
            role=role,
        )

    async def grant_insert_update_to_table(
        self, *, database: str, table: str, role: str
    ) -> None:
        await asyncio.to_thread(
            grant_insert_update_to_table,
            self._client,
            database=database,
            table=table,
            role=role,
        )

    async def add_row_policy(
        self,
        *,
        database: str,
        table: str,
        column: str,
        role: str,
        value: str,
    ) -> None:
        await asyncio.to_thread(
            add_row_policy,
            self._client,
            database=database,
            table=table,
            column=column,
            role=role,
            value=value,
            settings=self._settings,
        )

    async def revoke_row_policy(
        self,
        *,
        database: str,
        table: str,
        role: str,
        value: str,
    ) -> None:
        await asyncio.to_thread(
            revoke_row_policy,
            self._client,
            database=database,
            table=table,
            role=role,
            value=value,
        )

    async def user_grants(self, *, username: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(user_grants, self._client, username=username)

    async def role_grants(self, *, role: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(role_grants, self._client, role=role)

    async def user_role_memberships(self, *, username: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            user_role_memberships, self._client, username=username
        )

    async def user_row_policies(self, *, username: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            user_row_policies, self._client, username=username
        )

    async def role_row_policies(self, *, role: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            role_row_policies, self._client, role=role
        )

    async def table_row_policies(
        self, *, database: str, table: str
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            table_row_policies, self._client, database=database, table=table
        )
