"""Per-request ClickHouse handle classes used by FastAPI route handlers.

The handle wraps the app-scoped Client and a username; it doesn't open or close
connections. Each method wraps the sync clickhouse-connect call in
``asyncio.to_thread`` so a slow query doesn't block the FastAPI event loop.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

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
    to the SQL so the query runs under the user's CH identity. Service-identity
    queries and admin functions are not exposed here — see ``ClickHouseAdminHandle``.
    """

    def __init__(self, *, client: Client, username: str) -> None:
        self._client = client
        self._username_quoted = quote_identifier(username, kind="username")
        self._username = username

    async def query_as_user(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> QueryResult:
        impersonated = f"EXECUTE AS {self._username_quoted} {sql}"
        return await asyncio.to_thread(
            self._client.query,
            impersonated,
            parameters=dict(parameters) if parameters else None,
        )


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
        username: str,
        settings: ClickHouseSettings,
    ) -> None:
        super().__init__(client=client, username=username)
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
