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
from typing import TYPE_CHECKING, Any

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
from iris.auth.authz.mapping import RoleMappingError
from iris.clickhouse.database_admins import DatabaseAdminStore
from iris.clickhouse.grants import revoke_select_from_database
from iris.clickhouse.identifiers import quote_identifier, validate_identifier
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX

if TYPE_CHECKING:
    from iris.auth.authz.store import RoleMappingStore
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


class ClickHouseDatabaseCreatorHandle:
    """Handle for users with the ``clickhouse_database_creator`` role.

    Exposes only ``create_database`` — creates a CH database and atomically
    records the calling iris user as an admin of the new database.
    """

    def __init__(
        self,
        *,
        client: Client,
        settings: ClickHouseSettings,
        db_admin_store: DatabaseAdminStore,
        username: str,
    ) -> None:
        self._client = client
        self._settings = settings
        self._db_admin_store = db_admin_store
        self._username = username

    async def create_database(self, name: str) -> None:
        """``CREATE DATABASE IF NOT EXISTS <name>``; record the calling user as
        an admin of the new database. The CH ``IF NOT EXISTS`` and the store's
        ``INSERT OR IGNORE`` together make this safe to retry after a partial
        failure."""
        validate_identifier(name, kind="database")
        quoted = quote_identifier(name, kind="database")
        await asyncio.to_thread(
            self._client.command, f"CREATE DATABASE IF NOT EXISTS {quoted}"
        )
        await self._db_admin_store.add_admin_user(
            database=name, username=self._username
        )


class ClickHouseDatabaseAdminHandle:
    """Per-database admin handle.

    Bound to a specific database. Methods translate iris-friendly identifiers
    (username, group) to CH role names (<username>_USER, <group>_GRP) using
    the existing suffix constants. Read grants, row policies, and admin
    delegation are scoped to ``self._database``.
    """

    def __init__(
        self,
        *,
        client: Client,
        http_client: httpx.AsyncClient,
        settings: ClickHouseSettings,
        db_admin_store: DatabaseAdminStore,
        authz_store: "RoleMappingStore",
        database: str,
        username: str,
    ) -> None:
        self._client = client
        self._http_client = http_client
        self._settings = settings
        self._db_admin_store = db_admin_store
        self._authz_store = authz_store
        self._database = database
        self._username = username

    # ---- grants ----

    async def grant_select_to_user(self, username: str) -> None:
        await asyncio.to_thread(
            grant_select_to_database,
            self._client,
            database=self._database,
            role=f"{username}{USER_ROLE_SUFFIX}",
        )

    async def grant_select_to_group(self, group: str) -> None:
        await asyncio.to_thread(
            grant_select_to_database,
            self._client,
            database=self._database,
            role=f"{group}{GROUP_ROLE_SUFFIX}",
        )

    async def revoke_select_from_user(self, username: str) -> None:
        await asyncio.to_thread(
            revoke_select_from_database,
            self._client,
            database=self._database,
            role=f"{username}{USER_ROLE_SUFFIX}",
        )

    async def revoke_select_from_group(self, group: str) -> None:
        await asyncio.to_thread(
            revoke_select_from_database,
            self._client,
            database=self._database,
            role=f"{group}{GROUP_ROLE_SUFFIX}",
        )

    # ---- row policies ----

    async def add_row_policy_for_user(
        self, *, table: str, column: str, username: str, value: str
    ) -> None:
        await asyncio.to_thread(
            add_row_policy,
            self._client,
            database=self._database,
            table=table,
            column=column,
            role=f"{username}{USER_ROLE_SUFFIX}",
            value=value,
            settings=self._settings,
        )

    async def add_row_policy_for_group(
        self, *, table: str, column: str, group: str, value: str
    ) -> None:
        await asyncio.to_thread(
            add_row_policy,
            self._client,
            database=self._database,
            table=table,
            column=column,
            role=f"{group}{GROUP_ROLE_SUFFIX}",
            value=value,
            settings=self._settings,
        )

    async def revoke_row_policy_for_user(
        self, *, table: str, column: str, username: str, value: str
    ) -> None:
        await asyncio.to_thread(
            revoke_row_policy,
            self._client,
            database=self._database,
            table=table,
            role=f"{username}{USER_ROLE_SUFFIX}",
            value=value,
        )

    async def revoke_row_policy_for_group(
        self, *, table: str, column: str, group: str, value: str
    ) -> None:
        await asyncio.to_thread(
            revoke_row_policy,
            self._client,
            database=self._database,
            table=table,
            role=f"{group}{GROUP_ROLE_SUFFIX}",
            value=value,
        )

    # ---- delegation ----

    async def add_admin_user(self, username: str) -> None:
        await self._db_admin_store.add_admin_user(
            database=self._database, username=username
        )

    async def remove_admin_user(self, username: str) -> None:
        await self._db_admin_store.remove_admin_user(
            database=self._database, username=username
        )

    async def add_admin_role(self, role: str) -> None:
        mapping = await self._authz_store.get_mapping()
        if role not in mapping.roles:
            raise RoleMappingError(f"role {role!r} is not defined in the role mapping")
        await self._db_admin_store.add_admin_role(
            database=self._database, role=role
        )

    async def remove_admin_role(self, role: str) -> None:
        # No validation: removing a role from per-DB admin can target a role
        # that has since been deleted from the authz mapping (cleanup case).
        await self._db_admin_store.remove_admin_role(
            database=self._database, role=role
        )

    # ---- listing ----

    async def list_admin_users(self) -> list[str]:
        return await self._db_admin_store.list_admin_users(database=self._database)

    async def list_admin_roles(self) -> list[str]:
        return await self._db_admin_store.list_admin_roles(database=self._database)

    # ---- audit ----

    async def list_grants(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_grants_sync)

    def _list_grants_sync(self) -> list[dict[str, Any]]:
        result = self._client.query(
            "SELECT * FROM system.grants WHERE database = {d:String}",
            parameters={"d": self._database},
        )
        return list(result.named_results())

    async def list_row_policies(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_row_policies_sync)

    def _list_row_policies_sync(self) -> list[dict[str, Any]]:
        result = self._client.query(
            "SELECT * FROM system.row_policies WHERE database = {d:String}",
            parameters={"d": self._database},
        )
        return list(result.named_results())
