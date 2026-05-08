from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, override

from iris.auth.session import EMPTY_RIGHTS, Rights


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
    """Request-scoped view of a logged-in session, with the ClickHouse
    operations available to the session's tier.

    Built once per request by the auth dep. Routes receive an ``AuthSession``
    (or one of its subclasses: :class:`DatabaseSession`,
    :class:`DatabaseAdminSession`, :class:`DatabaseCreatorSession`,
    :class:`AdminSession`) via the ``Annotated`` alias deps in
    ``iris.auth.deps``.

    Frozen except for ``data``: the dict is a per-request snapshot deserialized
    from the SQLite session store. Mutations to the dict do NOT auto-persist —
    call ``await request.app.state.auth_session_store.update_data(session.id,
    session.data)`` to write changes back.

    The ``client`` / ``http_client`` / ``settings`` fields are CH references
    injected by the dep resolver. They are not part of the persistent identity
    (``compare=False``, ``repr=False``) so two sessions with identical
    ``id``/``user``/``rights``/etc. compare equal regardless of which CH
    connections happen to be wired in.
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

    async def query_as_user(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import query_as_user_impl
        return await query_as_user_impl(
            self.http_client,
            username=self.user.username,
            sql=sql,
            parameters=parameters,
            database=database,
        )


@dataclass(frozen=True, slots=True)
class DatabaseSession(AuthSession):
    """Session bound to a specific database (the path/query parameter that
    drove the alias dep). ``query_as_user`` is auto-scoped to ``self.database``;
    no override is provided — to query a different database from a DB-scoped
    route, use a fully-qualified table name and let CH enforce privileges.
    """
    database: str

    @override
    async def query_as_user(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        # Intentional Liskov violation: the parent's `database=` kwarg is
        # dropped because the bound self.database is the source of truth.
        # To query a different database from a DB-scoped route, use a
        # fully-qualified table name (e.g. ``other_db.t``) and let CH
        # enforce privileges.
        from iris.clickhouse.handle import query_as_user_impl
        return await query_as_user_impl(
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
        from iris.clickhouse.handle import grant_reader_impl
        await grant_reader_impl(
            self.client, database=self.database, username=username
        )

    async def grant_writer(self, username: str) -> None:
        from iris.clickhouse.handle import grant_writer_impl
        await grant_writer_impl(
            self.client, database=self.database, username=username
        )

    async def add_admin_user(self, username: str) -> None:
        from iris.clickhouse.handle import add_admin_user_impl
        await add_admin_user_impl(
            self.client, database=self.database, username=username
        )

    async def revoke_reader(self, username: str) -> None:
        from iris.clickhouse.handle import revoke_reader_impl
        await revoke_reader_impl(
            self.client, database=self.database, username=username
        )

    async def revoke_writer(self, username: str) -> None:
        from iris.clickhouse.handle import revoke_writer_impl
        await revoke_writer_impl(
            self.client, database=self.database, username=username
        )

    async def remove_admin_user(self, username: str) -> None:
        from iris.clickhouse.handle import remove_admin_user_impl
        await remove_admin_user_impl(
            self.client, database=self.database, username=username
        )

    async def grant_reader_to_group(self, group: str) -> None:
        from iris.clickhouse.handle import grant_reader_to_group_impl
        await grant_reader_to_group_impl(
            self.client, database=self.database, group=group
        )

    async def grant_writer_to_group(self, group: str) -> None:
        from iris.clickhouse.handle import grant_writer_to_group_impl
        await grant_writer_to_group_impl(
            self.client, database=self.database, group=group
        )

    async def add_admin_group(self, group: str) -> None:
        from iris.clickhouse.handle import add_admin_group_impl
        await add_admin_group_impl(
            self.client, database=self.database, group=group
        )

    async def revoke_reader_from_group(self, group: str) -> None:
        from iris.clickhouse.handle import revoke_reader_from_group_impl
        await revoke_reader_from_group_impl(
            self.client, database=self.database, group=group
        )

    async def revoke_writer_from_group(self, group: str) -> None:
        from iris.clickhouse.handle import revoke_writer_from_group_impl
        await revoke_writer_from_group_impl(
            self.client, database=self.database, group=group
        )

    async def remove_admin_group(self, group: str) -> None:
        from iris.clickhouse.handle import remove_admin_group_impl
        await remove_admin_group_impl(
            self.client, database=self.database, group=group
        )

    async def delete_database(self) -> None:
        from iris.clickhouse.handle import delete_database_impl
        await delete_database_impl(self.client, database=self.database)

    async def list_admin_members(self) -> list[str]:
        from iris.clickhouse.handle import list_admin_members_impl
        return await list_admin_members_impl(
            self.client, database=self.database
        )

    async def list_grants(self) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import list_grants_impl
        return await list_grants_impl(self.client, database=self.database)

    async def list_row_policies(self) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import list_row_policies_impl
        return await list_row_policies_impl(self.client, database=self.database)


@dataclass(frozen=True, slots=True)
class DatabaseCreatorSession(AuthSession):
    """Session that can create new databases. Returned by the
    ``SessionDatabaseCreator`` alias when ``rights.is_admin`` or
    ``rights.can_create_database``."""

    async def create_database(self, name: str) -> None:
        from iris.clickhouse.handle import create_database_impl
        await create_database_impl(
            self.client,
            name=name,
            creator_username=self.user.username,
        )


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
    ) -> Any:  # QueryResult — typed Any to avoid clickhouse-connect import
        from iris.clickhouse.handle import query_as_service_impl
        return await query_as_service_impl(
            self.client, sql=sql, parameters=parameters, database=database
        )

    async def reprovision_user(self, *, username: str, groups: list[str]) -> None:
        from iris.clickhouse.handle import reprovision_user_impl
        await reprovision_user_impl(
            self.client, username=username, groups=groups, settings=self.settings
        )

    async def grant_select_to_database(self, *, database: str, role: str) -> None:
        from iris.clickhouse.handle import grant_select_to_database_impl
        await grant_select_to_database_impl(
            self.client, database=database, role=role
        )

    async def grant_insert_update_to_table(
        self, *, database: str, table: str, role: str
    ) -> None:
        from iris.clickhouse.handle import grant_insert_update_to_table_impl
        await grant_insert_update_to_table_impl(
            self.client, database=database, table=table, role=role
        )

    async def add_row_policy(
        self, *, database: str, table: str, column: str, role: str, value: str
    ) -> None:
        from iris.clickhouse.handle import add_row_policy_impl
        await add_row_policy_impl(
            self.client,
            database=database,
            table=table,
            column=column,
            role=role,
            value=value,
            settings=self.settings,
        )

    async def revoke_row_policy(
        self, *, database: str, table: str, role: str, value: str
    ) -> None:
        from iris.clickhouse.handle import revoke_row_policy_impl
        await revoke_row_policy_impl(
            self.client, database=database, table=table, role=role, value=value
        )

    async def user_grants(self, *, username: str) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import user_grants_impl
        return await user_grants_impl(self.client, username=username)

    async def role_grants(self, *, role: str) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import role_grants_impl
        return await role_grants_impl(self.client, role=role)

    async def user_role_memberships(
        self, *, username: str
    ) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import user_role_memberships_impl
        return await user_role_memberships_impl(self.client, username=username)

    async def user_row_policies(self, *, username: str) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import user_row_policies_impl
        return await user_row_policies_impl(self.client, username=username)

    async def role_row_policies(self, *, role: str) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import role_row_policies_impl
        return await role_row_policies_impl(self.client, role=role)

    async def table_row_policies(
        self, *, database: str, table: str
    ) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import table_row_policies_impl
        return await table_row_policies_impl(
            self.client, database=database, table=table
        )
