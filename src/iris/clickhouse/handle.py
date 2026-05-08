"""Per-request ClickHouse handle classes used by FastAPI route handlers.

Four handle types share the same per-request shape but expose different surfaces:

- :class:`ClickHouseHandle` — for any logged-in user. Only ``query_as_user`` is
  exposed. The query runs as the user via ClickHouse's per-query
  ``EXECUTE AS`` prefix.
- :class:`ClickHouseAdminHandle` — for sessions whose ``rights.is_admin`` is
  True. Adds ``query_as_service`` (no impersonation) plus async wrappers around
  the module-level admin/audit functions.
- :class:`ClickHouseDatabaseCreatorHandle` — admits ``rights.is_admin`` or
  ``rights.can_create_database``. Exposes ``create_database`` which creates the
  database, the three tier roles, and grants DBADMIN to the creator.
- :class:`ClickHouseDatabaseAdminHandle` — for ``rights.has_admin(database)``.
  Tier-role grant/revoke per user/group, ``delete_database``, audit listing.

Why two HTTP transports? ClickHouse's ``EXECUTE AS user <SELECT>`` body grammar
rejects ``FORMAT`` clauses, but ``clickhouse-connect``'s ``query()`` always
appends ``FORMAT Native``. So impersonated queries go through a raw
``httpx.AsyncClient`` with ``?default_format=JSONEachRow`` as a URL parameter.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, cast

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
    TIER_DBADMIN,
    TIER_DBREADER,
    TIER_DBWRITER,
    create_tier_roles,
    drop_tier_roles,
    grant_insert_update_to_table,
    grant_select_to_database,
    grant_tier_to_group,
    grant_tier_to_user,
    revoke_tier_from_group,
    revoke_tier_from_user,
    tier_role_name,
)
from iris.clickhouse.identifiers import quote_identifier, validate_identifier
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
from iris.clickhouse.users import init_user_rights


class ClickHouseHandle:
    """Per-request handle for any logged-in user.

    Exposes only ``query_as_user``, which prepends ``EXECUTE AS <quoted_username>``
    to the SQL via raw HTTP so the query runs under the user's CH identity.

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
    """Admin-capable handle for routes gated on ``rights.is_admin``.

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
    """Handle for users with ``can_create_database`` (or ``is_admin``).

    ``create_database`` creates the database, the three tier roles, the
    privilege grants, and grants ``DBADMIN`` to the creator's per-user role.
    All steps are ``IF NOT EXISTS`` and idempotent.
    """

    def __init__(
        self,
        *,
        client: Client,
        settings: ClickHouseSettings,
        username: str,
    ) -> None:
        self._client = client
        self._settings = settings
        self._username = username

    async def create_database(self, name: str) -> None:
        validate_identifier(name, kind="database")
        quoted = quote_identifier(name, kind="database")
        await asyncio.to_thread(
            self._client.command, f"CREATE DATABASE IF NOT EXISTS {quoted}"
        )
        await asyncio.to_thread(create_tier_roles, self._client, database=name)
        await asyncio.to_thread(
            grant_tier_to_user,
            self._client,
            database=name,
            tier=TIER_DBADMIN,
            username=self._username,
        )


class ClickHouseDatabaseAdminHandle:
    """Per-database admin handle.

    All grant/revoke operations are tier-role grants on per-user/per-group
    roles in CH. Reading "who is admin of database X" is querying CH for
    members of ``X_DBADMIN``. Adding an admin is granting ``X_DBADMIN`` to
    the target's ``<username>_USER`` role (pre-creating it if absent).
    """

    def __init__(
        self,
        *,
        client: Client,
        http_client: httpx.AsyncClient,
        settings: ClickHouseSettings,
        database: str,
        username: str,
    ) -> None:
        self._client = client
        self._http_client = http_client
        self._settings = settings
        self._database = database
        self._username = username

    # ---- tier grants ----

    async def grant_reader(self, username: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_user,
            self._client,
            database=self._database,
            tier=TIER_DBREADER,
            username=username,
        )

    async def grant_writer(self, username: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_user,
            self._client,
            database=self._database,
            tier=TIER_DBWRITER,
            username=username,
        )

    async def add_admin_user(self, username: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_user,
            self._client,
            database=self._database,
            tier=TIER_DBADMIN,
            username=username,
        )

    async def revoke_reader(self, username: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_user,
            self._client,
            database=self._database,
            tier=TIER_DBREADER,
            username=username,
        )

    async def revoke_writer(self, username: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_user,
            self._client,
            database=self._database,
            tier=TIER_DBWRITER,
            username=username,
        )

    async def remove_admin_user(self, username: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_user,
            self._client,
            database=self._database,
            tier=TIER_DBADMIN,
            username=username,
        )

    # ---- group equivalents ----

    async def grant_reader_to_group(self, group: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_group,
            self._client,
            database=self._database,
            tier=TIER_DBREADER,
            group=group,
        )

    async def grant_writer_to_group(self, group: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_group,
            self._client,
            database=self._database,
            tier=TIER_DBWRITER,
            group=group,
        )

    async def add_admin_group(self, group: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_group,
            self._client,
            database=self._database,
            tier=TIER_DBADMIN,
            group=group,
        )

    async def revoke_reader_from_group(self, group: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_group,
            self._client,
            database=self._database,
            tier=TIER_DBREADER,
            group=group,
        )

    async def revoke_writer_from_group(self, group: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_group,
            self._client,
            database=self._database,
            tier=TIER_DBWRITER,
            group=group,
        )

    async def remove_admin_group(self, group: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_group,
            self._client,
            database=self._database,
            tier=TIER_DBADMIN,
            group=group,
        )

    # ---- database lifecycle ----

    async def delete_database(self) -> None:
        """``DROP DATABASE IF EXISTS`` then drop the three tier roles. Idempotent.

        Order matters: drop the database first so a partial failure leaves the
        data dropped (the goal) rather than orphan grants.
        """
        db_q = quote_identifier(self._database, kind="database")
        await asyncio.to_thread(
            self._client.command, f"DROP DATABASE IF EXISTS {db_q}"
        )
        await asyncio.to_thread(
            drop_tier_roles, self._client, database=self._database
        )

    # ---- listing ----

    async def list_admin_members(self) -> list[str]:
        """Members of ``<database>_DBADMIN`` — both user and group roles."""
        admin_role = tier_role_name(self._database, TIER_DBADMIN)
        rows = await asyncio.to_thread(
            self._client.query,
            "SELECT role_name FROM system.role_grants WHERE granted_role_name = {r:String}",
            {"r": admin_role},
        )
        return [cast(str, row["role_name"]) for row in rows.named_results()]

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
