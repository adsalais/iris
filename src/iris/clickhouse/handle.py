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

from iris.clickhouse.identifiers import quote_identifier


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
