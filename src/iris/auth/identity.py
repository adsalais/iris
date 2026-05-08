from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from clickhouse_connect.driver.query import QueryResult

from iris.auth.session import EMPTY_RIGHTS, Rights
from iris.clickhouse import audit, grants, policies
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
from iris.clickhouse.users import init_user_rights


@dataclass(frozen=True, slots=True)
class User:
    subject: str
    username: str
    display_name: str
    groups: tuple[str, ...]


@dataclass(slots=True)
class UserSession:
    """Internal mutable session row from the SQLite store.

    Routes consume the request-scoped immutable :class:`AuthSession` view via
    the alias deps in ``iris.auth.deps``. ``UserSession`` is the row shape that
    sliding-TTL refresh operates on.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    absolute_expires_at: datetime
    data: dict[str, Any] = field(default_factory=dict)
    rights: Rights = EMPTY_RIGHTS


@dataclass(frozen=True, slots=True)
class AuthSession:
    """Request-scoped view of a logged-in session.

    Built once per request by the auth dep. Routes receive an ``AuthSession``
    (or one of its subclasses: :class:`DatabaseSession`,
    :class:`DatabaseAdminSession`, :class:`DatabaseCreatorSession`,
    :class:`AdminSession`) via the ``Annotated`` alias deps in
    ``iris.auth.deps``.

    Frozen except for ``data``: the dict is a per-request snapshot deserialized
    from the SQLite session store. Mutations to the dict do NOT auto-persist —
    call ``await session.persist_data()`` to write the current ``data`` dict
    back to the store before returning.

    The ``client`` / ``http_client`` / ``settings`` / ``store`` fields are
    references injected by the dep resolver. They are not part of the
    persistent identity (``compare=False``, ``repr=False``) so two sessions
    with identical ``id``/``user``/``rights``/etc. compare equal regardless
    of which connections happen to be wired in.

    Note: ``AuthSession`` does not expose a ``query_as_user`` method. CH
    impersonation requires a target database; the database-scoped
    subclasses (``DatabaseSession`` and below) carry the per-database
    ``query_as_user``. Admins query as the service identity via
    ``AdminSession.query_as_service``.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    data: dict[str, Any]
    rights: Rights
    client: Any = field(repr=False, compare=False)
    http_client: Any = field(repr=False, compare=False)
    settings: Any = field(repr=False, compare=False)
    store: Any = field(repr=False, compare=False)

    async def persist_data(self) -> None:
        """Write the current ``data`` dict back to the session store.

        Routes that mutate ``session.data`` and want the change to survive the
        request call this before returning. Values must be JSON-encodable;
        anything else raises ``TypeError`` at write time.
        """
        await self.store.update_data(self.id, self.data)


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
        return await query_as_user(
            self.http_client,
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
        await asyncio.to_thread(
            grant_tier_to_user, self.client,
            database=self.database, tier=TIER_DBREADER, username=username,
        )

    async def grant_writer(self, username: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_user, self.client,
            database=self.database, tier=TIER_DBWRITER, username=username,
        )

    async def add_admin_user(self, username: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_user, self.client,
            database=self.database, tier=TIER_DBADMIN, username=username,
        )

    async def revoke_reader(self, username: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_user, self.client,
            database=self.database, tier=TIER_DBREADER, username=username,
        )

    async def revoke_writer(self, username: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_user, self.client,
            database=self.database, tier=TIER_DBWRITER, username=username,
        )

    async def remove_admin_user(self, username: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_user, self.client,
            database=self.database, tier=TIER_DBADMIN, username=username,
        )

    async def grant_reader_to_group(self, group: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_group, self.client,
            database=self.database, tier=TIER_DBREADER, group=group,
        )

    async def grant_writer_to_group(self, group: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_group, self.client,
            database=self.database, tier=TIER_DBWRITER, group=group,
        )

    async def add_admin_group(self, group: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_group, self.client,
            database=self.database, tier=TIER_DBADMIN, group=group,
        )

    async def revoke_reader_from_group(self, group: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_group, self.client,
            database=self.database, tier=TIER_DBREADER, group=group,
        )

    async def revoke_writer_from_group(self, group: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_group, self.client,
            database=self.database, tier=TIER_DBWRITER, group=group,
        )

    async def remove_admin_group(self, group: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_group, self.client,
            database=self.database, tier=TIER_DBADMIN, group=group,
        )

    async def delete_database(self) -> None:
        db_q = quote_identifier(self.database, kind="database")
        database = self.database
        client = self.client

        def _sync() -> None:
            client.command(f"DROP DATABASE IF EXISTS {db_q}")
            drop_tier_roles(client, database=database)

        await asyncio.to_thread(_sync)

    async def list_admin_members(self) -> list[dict[str, str]]:
        """Return everything granted the per-database admin role.

        Each entry is ``{"kind": "user" | "role", "name": <str>}``. Includes
        direct user grantees AND role grantees (e.g. group-roles or
        per-user roles holding the admin tier). Previously this only
        returned ``role_name`` and emitted ``None`` for direct user grants.
        """
        admin_role = tier_role_name(self.database, TIER_DBADMIN)
        client = self.client

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
        client = self.client
        database = self.database

        def _sync() -> list[dict[str, Any]]:
            result = client.query(
                "SELECT * FROM system.grants WHERE database = {d:String}",
                parameters={"d": database},
            )
            return list(result.named_results())

        return await asyncio.to_thread(_sync)

    async def list_row_policies(self) -> list[dict[str, Any]]:
        client = self.client
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
    ``SessionDatabaseCreator`` alias when ``rights.is_admin`` or
    ``rights.can_create_database``."""

    async def create_database(self, name: str) -> None:
        validate_identifier(name, kind="database")
        quoted = quote_identifier(name, kind="database")
        creator_username = self.user.username
        client = self.client

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
    row-policy operations. For per-database operations, the route should use
    ``SessionDatabaseAdmin`` (which admits admins via the ``is_admin``
    superset and returns a :class:`DatabaseAdminSession` bound to the path's
    database)."""

    async def query_as_service(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> QueryResult:
        return await query_as_service(
            self.client, sql=sql, parameters=parameters, database=database,
        )

    async def reprovision_user(self, *, username: str, groups: list[str]) -> None:
        await asyncio.to_thread(
            init_user_rights, self.client,
            username=username, groups=groups, settings=self.settings,
        )

    async def grant_select_to_database(self, *, database: str, role: str) -> None:
        await asyncio.to_thread(
            grants.grant_select_to_database, self.client,
            database=database, role=role,
        )

    async def grant_insert_update_to_table(
        self, *, database: str, table: str, role: str
    ) -> None:
        await asyncio.to_thread(
            grants.grant_insert_update_to_table, self.client,
            database=database, table=table, role=role,
        )

    async def add_row_policy(
        self, *, database: str, table: str, column: str, role: str, value: str
    ) -> None:
        await asyncio.to_thread(
            policies.add_row_policy, self.client,
            database=database, table=table, column=column, role=role, value=value,
        )

    async def revoke_row_policy(
        self, *, database: str, table: str, role: str, value: str
    ) -> None:
        await asyncio.to_thread(
            policies.revoke_row_policy, self.client,
            database=database, table=table, role=role, value=value,
        )

    async def user_grants(self, *, username: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(audit.user_grants, self.client, username=username)

    async def role_grants(self, *, role: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(audit.role_grants, self.client, role=role)

    async def user_role_memberships(
        self, *, username: str
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(audit.user_role_memberships, self.client, username=username)

    async def user_row_policies(self, *, username: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(audit.user_row_policies, self.client, username=username)

    async def role_row_policies(self, *, role: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(audit.role_row_policies, self.client, role=role)

    async def table_row_policies(
        self, *, database: str, table: str
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            audit.table_row_policies, self.client,
            database=database, table=table,
        )
