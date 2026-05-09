"""Request-scoped session views.

Each route receives an ``AuthSession`` (or a database-bound subclass) via the
``Annotated`` alias deps in ``iris.auth.deps``. These views carry the CH
client / httpx client / settings / SessionStore references that session
methods need to talk to ClickHouse; they are constructed once per request
and discarded at request end.

Frozen except for ``data``: the dict is a per-request snapshot deserialized
from the SQLite session store. Mutations to the dict do NOT auto-persist —
call ``await session.persist_data()`` to write the current ``data`` dict
back to the store before returning.

The ``client`` / ``http_client`` / ``settings`` / ``store`` fields are
``Optional`` because ``build_app(install_clickhouse=False)`` is a documented
test mode that wires up auth without ClickHouse. Subclass methods that
perform CH operations call ``self._ch()`` once at the top, which raises
if the refs are missing.

Note: ``AuthSession`` does not expose a ``query_as_user`` method. CH
impersonation requires a target database; the database-scoped subclasses
(``DatabaseSession`` and below) carry the per-database ``query_as_user``.
Admins query as the service identity via ``AdminSession.query_as_service``.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

import httpx
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.query import QueryResult

from iris.auth.identity import User
from iris.auth.rights import Capabilities
from iris.auth.store import SessionStore
from iris.clickhouse import audit, grants, policies
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.grants import (
    TIER_DBADMIN,
    TIER_DBREADER,
    TIER_DBWRITER,
    create_tier_roles,
    drop_tier_roles,
    grant_tier_to_group,
    grant_tier_to_user,
    revoke_tier_from_group,
    revoke_tier_from_user,
    tier_role_name,
)
from iris.clickhouse.identifiers import quote_identifier, validate_identifier
from iris.clickhouse.queries import query_as_service, query_as_user
from iris.clickhouse.users import provision_user


@dataclass(frozen=True, slots=True)
class AuthSession:
    """Request-scoped view of a logged-in session.

    Built once per request by the auth dep. Routes receive an ``AuthSession``
    (or one of its subclasses: :class:`DatabaseSession`,
    :class:`DatabaseAdminSession`, :class:`DatabaseCreatorSession`,
    :class:`AdminSession`) via the ``Annotated`` alias deps in
    ``iris.auth.deps``.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    data: dict[str, Any]
    capabilities: Capabilities
    client: Client | None = field(repr=False, compare=False)
    http_client: httpx.AsyncClient | None = field(repr=False, compare=False)
    settings: ClickHouseSettings | None = field(repr=False, compare=False)
    store: SessionStore | None = field(repr=False, compare=False)

    async def persist_data(self) -> None:
        """Write the current ``data`` dict back to the session store.

        Routes that mutate ``session.data`` and want the change to survive the
        request call this before returning. Values must be JSON-encodable;
        anything else raises ``TypeError`` at write time.
        """
        if self.store is None:
            raise RuntimeError(
                "persist_data requires a SessionStore; this session was "
                + "constructed without one (typically a CH-only test fixture)"
            )
        await self.store.update_data(self.id, self.data)

    def _ch(self) -> tuple[Client, httpx.AsyncClient, ClickHouseSettings]:
        """Return the CH refs as a non-None triple, or raise if CH isn't installed.

        Subclasses that perform CH operations call this once at the top of
        each method instead of reading ``self.client`` / ``http_client`` /
        ``settings`` directly. The Optional fields exist to support
        ``build_app(install_clickhouse=False)`` — by the time a CH-using
        method runs, the alias deps have already gated on CH-derived
        ``Capabilities``, so the refs are populated in practice.
        """
        if (
            self.client is None
            or self.http_client is None
            or self.settings is None
        ):
            raise RuntimeError(
                "ClickHouse not installed; this method requires "
                + "build_app(install_clickhouse=True)"
            )
        return self.client, self.http_client, self.settings


@dataclass(frozen=True, slots=True)
class DatabaseSession(AuthSession):
    """Session bound to a specific database (the path/query parameter that
    drove the alias dep). ``query_as_user`` is auto-scoped to ``self.database``.
    To query a different database, use a fully-qualified table name and let
    CH enforce privileges.
    """
    database: str

    async def query_as_user(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        _client, http_client, _settings = self._ch()
        return await query_as_user(
            http_client,
            username=self.user.username,
            sql=sql,
            parameters=parameters,
            database=self.database,
        )


@dataclass(frozen=True, slots=True)
class DatabaseAdminSession(DatabaseSession):
    """Per-database admin session. Adds tier-grant/revoke/lifecycle/audit
    methods scoped to ``self.database``."""

    async def grant_reader(self, username: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grant_tier_to_user, client,
            database=self.database, tier=TIER_DBREADER, username=username,
        )

    async def grant_writer(self, username: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grant_tier_to_user, client,
            database=self.database, tier=TIER_DBWRITER, username=username,
        )

    async def add_admin_user(self, username: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grant_tier_to_user, client,
            database=self.database, tier=TIER_DBADMIN, username=username,
        )

    async def revoke_reader(self, username: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            revoke_tier_from_user, client,
            database=self.database, tier=TIER_DBREADER, username=username,
        )

    async def revoke_writer(self, username: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            revoke_tier_from_user, client,
            database=self.database, tier=TIER_DBWRITER, username=username,
        )

    async def remove_admin_user(self, username: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            revoke_tier_from_user, client,
            database=self.database, tier=TIER_DBADMIN, username=username,
        )

    async def grant_reader_to_group(self, group: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grant_tier_to_group, client,
            database=self.database, tier=TIER_DBREADER, group=group,
        )

    async def grant_writer_to_group(self, group: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grant_tier_to_group, client,
            database=self.database, tier=TIER_DBWRITER, group=group,
        )

    async def add_admin_group(self, group: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grant_tier_to_group, client,
            database=self.database, tier=TIER_DBADMIN, group=group,
        )

    async def revoke_reader_from_group(self, group: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            revoke_tier_from_group, client,
            database=self.database, tier=TIER_DBREADER, group=group,
        )

    async def revoke_writer_from_group(self, group: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            revoke_tier_from_group, client,
            database=self.database, tier=TIER_DBWRITER, group=group,
        )

    async def remove_admin_group(self, group: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            revoke_tier_from_group, client,
            database=self.database, tier=TIER_DBADMIN, group=group,
        )

    async def delete_database(self) -> None:
        db_q = quote_identifier(self.database, kind="database")
        database = self.database
        client, _, _ = self._ch()

        def _sync() -> None:
            client.command(f"DROP DATABASE IF EXISTS {db_q}")
            drop_tier_roles(client, database=database)

        await asyncio.to_thread(_sync)

    async def list_admin_members(self) -> list[dict[str, str]]:
        """Return everything granted the per-database admin role.

        Each entry is ``{"kind": "user" | "role", "name": <str>}``. Includes
        direct user grantees AND role grantees (e.g. group-roles or
        per-user roles holding the admin tier).
        """
        admin_role = tier_role_name(self.database, TIER_DBADMIN)
        client, _, _ = self._ch()

        def _sync() -> list[dict[str, str]]:
            rows = client.query(
                """
                SELECT user_name, role_name FROM system.role_grants
                WHERE granted_role_name = {r:String}
                """,
                {"r": admin_role},
            )
            out: list[dict[str, str]] = []
            for row in rows.named_results():
                u = row.get("user_name")
                r = row.get("role_name")
                if u:
                    out.append({"kind": "user", "name": cast(str, u)})
                elif r:
                    out.append({"kind": "role", "name": cast(str, r)})
            return out

        return await asyncio.to_thread(_sync)

    async def list_grants(self) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        database = self.database

        def _sync() -> list[dict[str, Any]]:
            result = client.query(
                "SELECT * FROM system.grants WHERE database = {d:String}",
                parameters={"d": database},
            )
            return list(result.named_results())

        return await asyncio.to_thread(_sync)

    async def list_row_policies(self) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        database = self.database

        def _sync() -> list[dict[str, Any]]:
            result = client.query(
                "SELECT * FROM system.row_policies WHERE database = {d:String}",
                parameters={"d": database},
            )
            return list(result.named_results())

        return await asyncio.to_thread(_sync)


@dataclass(frozen=True, slots=True)
class DatabaseCreatorSession(AuthSession):
    """Session that can create new databases. Returned by the
    ``SessionDatabaseCreator`` alias when ``capabilities.is_admin`` or
    ``capabilities.can_create_database``."""

    async def create_database(self, name: str) -> None:
        validate_identifier(name, kind="database")
        quoted = quote_identifier(name, kind="database")
        creator_username = self.user.username
        client, _, _ = self._ch()

        def _sync() -> None:
            client.command(f"CREATE DATABASE IF NOT EXISTS {quoted}")
            create_tier_roles(client, database=name)
            grant_tier_to_user(
                client, database=name, tier=TIER_DBADMIN, username=creator_username,
            )

        await asyncio.to_thread(_sync)


@dataclass(frozen=True, slots=True)
class AdminSession(AuthSession):
    """Global-admin session. Adds service-identity queries plus audit and
    row-policy operations."""

    async def query_as_service(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> QueryResult:
        client, _, _ = self._ch()
        return await query_as_service(
            client, sql=sql, parameters=parameters, database=database,
        )

    async def reprovision_user(self, *, username: str, groups: list[str]) -> None:
        client, _, settings = self._ch()
        await asyncio.to_thread(
            provision_user, client,
            username=username, groups=groups, settings=settings,
        )

    async def grant_select_to_database(self, *, database: str, role: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grants.grant_select_to_database, client,
            database=database, role=role,
        )

    async def grant_insert_update_to_table(
        self, *, database: str, table: str, role: str
    ) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grants.grant_insert_update_to_table, client,
            database=database, table=table, role=role,
        )

    async def add_row_policy(
        self, *, database: str, table: str, column: str, role: str, value: str
    ) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            policies.add_row_policy, client,
            database=database, table=table, column=column, role=role, value=value,
        )

    async def revoke_row_policy(
        self, *, database: str, table: str, role: str, value: str
    ) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            policies.revoke_row_policy, client,
            database=database, table=table, role=role, value=value,
        )

    async def user_grants(self, *, username: str) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        return await asyncio.to_thread(audit.user_grants, client, username=username)

    async def role_grants(self, *, role: str) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        return await asyncio.to_thread(audit.role_grants, client, role=role)

    async def user_role_memberships(
        self, *, username: str
    ) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        return await asyncio.to_thread(audit.user_role_memberships, client, username=username)

    async def user_row_policies(self, *, username: str) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        return await asyncio.to_thread(audit.user_row_policies, client, username=username)

    async def role_row_policies(self, *, role: str) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        return await asyncio.to_thread(audit.role_row_policies, client, role=role)

    async def table_row_policies(
        self, *, database: str, table: str
    ) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        return await asyncio.to_thread(
            audit.table_row_policies, client,
            database=database, table=table,
        )
