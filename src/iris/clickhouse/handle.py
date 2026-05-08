"""Standalone async ClickHouse operations.

Each ``*_impl`` function takes primitive arguments (``client``, ``http_client``,
``username``, etc.) and runs one CH operation. The Session classes in
``iris.auth.identity`` are the only callers; they import the ``*_impl``
functions at module top level. The cycle is broken by ``iris.clickhouse``
only importing from ``iris.auth.session`` (the ``Rights`` value type),
never from ``iris.auth.identity``.

Why two transport stories? ``query_as_user_impl`` posts to ClickHouse's HTTP
endpoint via ``httpx`` so we can prepend ``EXECUTE AS <user>`` without
clickhouse-connect rewriting the body with ``FORMAT Native``. Everything
else uses ``clickhouse-connect`` via ``asyncio.to_thread``.
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


# ---- standalone async functions ----
# Module-level implementations called by Session methods (iris.auth.identity)
# and by the handle classes below (which delegate). The classes are scheduled
# for deletion; the standalone functions are the canonical surface.


async def query_as_user_impl(
    http_client: httpx.AsyncClient,
    *,
    username: str,
    sql: str,
    parameters: Mapping[str, Any] | None = None,
    database: str | None = None,
) -> list[dict[str, Any]]:
    """Run ``sql`` on ClickHouse impersonated as ``username``.

    Sends ``EXECUTE AS <username> <sql>`` to the CH HTTP endpoint with
    ``default_format=JSONEachRow`` (and ``database=<database>`` when supplied,
    so unqualified table names resolve against that schema).
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


async def query_as_service_impl(
    client: Client,
    *,
    sql: str,
    parameters: Mapping[str, Any] | None = None,
    database: str | None = None,
) -> QueryResult:
    """Run ``sql`` as the service identity (no impersonation). When
    ``database`` is supplied, clickhouse-connect's ``database=`` kwarg sets
    the default schema for unqualified names."""
    kwargs: dict[str, Any] = {}
    if parameters:
        kwargs["parameters"] = dict(parameters)
    if database:
        kwargs["database"] = database
    return await asyncio.to_thread(client.query, sql, **kwargs)


async def reprovision_user_impl(
    client: Client,
    *,
    username: str,
    groups: list[str],
    settings: ClickHouseSettings,
) -> None:
    await asyncio.to_thread(
        init_user_rights,
        client,
        username=username,
        groups=groups,
        settings=settings,
    )


async def grant_select_to_database_impl(
    client: Client, *, database: str, role: str
) -> None:
    await asyncio.to_thread(
        grant_select_to_database, client, database=database, role=role
    )


async def grant_insert_update_to_table_impl(
    client: Client, *, database: str, table: str, role: str
) -> None:
    await asyncio.to_thread(
        grant_insert_update_to_table,
        client,
        database=database,
        table=table,
        role=role,
    )


async def add_row_policy_impl(
    client: Client,
    *,
    database: str,
    table: str,
    column: str,
    role: str,
    value: str,
) -> None:
    await asyncio.to_thread(
        add_row_policy,
        client,
        database=database,
        table=table,
        column=column,
        role=role,
        value=value,
    )


async def revoke_row_policy_impl(
    client: Client,
    *,
    database: str,
    table: str,
    role: str,
    value: str,
) -> None:
    await asyncio.to_thread(
        revoke_row_policy,
        client,
        database=database,
        table=table,
        role=role,
        value=value,
    )


async def user_grants_impl(client: Client, *, username: str) -> list[dict[str, Any]]:
    return await asyncio.to_thread(user_grants, client, username=username)


async def role_grants_impl(client: Client, *, role: str) -> list[dict[str, Any]]:
    return await asyncio.to_thread(role_grants, client, role=role)


async def user_role_memberships_impl(
    client: Client, *, username: str
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(user_role_memberships, client, username=username)


async def user_row_policies_impl(
    client: Client, *, username: str
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(user_row_policies, client, username=username)


async def role_row_policies_impl(
    client: Client, *, role: str
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(role_row_policies, client, role=role)


async def table_row_policies_impl(
    client: Client, *, database: str, table: str
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        table_row_policies, client, database=database, table=table
    )


async def create_database_impl(
    client: Client,
    *,
    name: str,
    creator_username: str,
) -> None:
    """``CREATE DATABASE IF NOT EXISTS`` + tier role lifecycle + grant
    ``DBADMIN`` to the creator's per-user role. Idempotent."""
    validate_identifier(name, kind="database")
    quoted = quote_identifier(name, kind="database")
    await asyncio.to_thread(client.command, f"CREATE DATABASE IF NOT EXISTS {quoted}")
    await asyncio.to_thread(create_tier_roles, client, database=name)
    await asyncio.to_thread(
        grant_tier_to_user,
        client,
        database=name,
        tier=TIER_DBADMIN,
        username=creator_username,
    )


async def grant_reader_impl(
    client: Client, *, database: str, username: str
) -> None:
    await asyncio.to_thread(
        grant_tier_to_user,
        client,
        database=database,
        tier=TIER_DBREADER,
        username=username,
    )


async def grant_writer_impl(
    client: Client, *, database: str, username: str
) -> None:
    await asyncio.to_thread(
        grant_tier_to_user,
        client,
        database=database,
        tier=TIER_DBWRITER,
        username=username,
    )


async def add_admin_user_impl(
    client: Client, *, database: str, username: str
) -> None:
    await asyncio.to_thread(
        grant_tier_to_user,
        client,
        database=database,
        tier=TIER_DBADMIN,
        username=username,
    )


async def revoke_reader_impl(
    client: Client, *, database: str, username: str
) -> None:
    await asyncio.to_thread(
        revoke_tier_from_user,
        client,
        database=database,
        tier=TIER_DBREADER,
        username=username,
    )


async def revoke_writer_impl(
    client: Client, *, database: str, username: str
) -> None:
    await asyncio.to_thread(
        revoke_tier_from_user,
        client,
        database=database,
        tier=TIER_DBWRITER,
        username=username,
    )


async def remove_admin_user_impl(
    client: Client, *, database: str, username: str
) -> None:
    await asyncio.to_thread(
        revoke_tier_from_user,
        client,
        database=database,
        tier=TIER_DBADMIN,
        username=username,
    )


async def grant_reader_to_group_impl(
    client: Client, *, database: str, group: str
) -> None:
    await asyncio.to_thread(
        grant_tier_to_group,
        client,
        database=database,
        tier=TIER_DBREADER,
        group=group,
    )


async def grant_writer_to_group_impl(
    client: Client, *, database: str, group: str
) -> None:
    await asyncio.to_thread(
        grant_tier_to_group,
        client,
        database=database,
        tier=TIER_DBWRITER,
        group=group,
    )


async def add_admin_group_impl(
    client: Client, *, database: str, group: str
) -> None:
    await asyncio.to_thread(
        grant_tier_to_group,
        client,
        database=database,
        tier=TIER_DBADMIN,
        group=group,
    )


async def revoke_reader_from_group_impl(
    client: Client, *, database: str, group: str
) -> None:
    await asyncio.to_thread(
        revoke_tier_from_group,
        client,
        database=database,
        tier=TIER_DBREADER,
        group=group,
    )


async def revoke_writer_from_group_impl(
    client: Client, *, database: str, group: str
) -> None:
    await asyncio.to_thread(
        revoke_tier_from_group,
        client,
        database=database,
        tier=TIER_DBWRITER,
        group=group,
    )


async def remove_admin_group_impl(
    client: Client, *, database: str, group: str
) -> None:
    await asyncio.to_thread(
        revoke_tier_from_group,
        client,
        database=database,
        tier=TIER_DBADMIN,
        group=group,
    )


async def delete_database_impl(client: Client, *, database: str) -> None:
    """``DROP DATABASE IF EXISTS`` then drop the three tier roles."""
    db_q = quote_identifier(database, kind="database")
    await asyncio.to_thread(client.command, f"DROP DATABASE IF EXISTS {db_q}")
    await asyncio.to_thread(drop_tier_roles, client, database=database)


async def list_admin_members_impl(client: Client, *, database: str) -> list[str]:
    """Members of ``<database>_DBADMIN`` — both user and group roles."""
    admin_role = tier_role_name(database, TIER_DBADMIN)
    rows = await asyncio.to_thread(
        client.query,
        "SELECT role_name FROM system.role_grants WHERE granted_role_name = {r:String}",
        {"r": admin_role},
    )
    return [cast(str, row["role_name"]) for row in rows.named_results()]


async def list_grants_impl(client: Client, *, database: str) -> list[dict[str, Any]]:
    def _sync() -> list[dict[str, Any]]:
        result = client.query(
            "SELECT * FROM system.grants WHERE database = {d:String}",
            parameters={"d": database},
        )
        return list(result.named_results())

    return await asyncio.to_thread(_sync)


async def list_row_policies_impl(
    client: Client, *, database: str
) -> list[dict[str, Any]]:
    def _sync() -> list[dict[str, Any]]:
        result = client.query(
            "SELECT * FROM system.row_policies WHERE database = {d:String}",
            parameters={"d": database},
        )
        return list(result.named_results())

    return await asyncio.to_thread(_sync)
