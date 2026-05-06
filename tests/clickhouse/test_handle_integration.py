"""Integration tests: EXECUTE AS prefix actually impersonates against a real CH server.

These exercise the handle's real httpx.AsyncClient against the testcontainer.
Verification uses ``currentUser()`` (post-impersonation identity) — ``user()`` is
an alias for ``authenticatedUser()`` (the underlying login) and would always
report ``iris_svc`` regardless of impersonation.
"""
from __future__ import annotations

import asyncio

import httpx

from iris.clickhouse.handle import ClickHouseAdminHandle, ClickHouseHandle
from iris.clickhouse.users import init_user_rights


def _http_client(ch_settings) -> httpx.AsyncClient:
    scheme = "https" if ch_settings.secure else "http"
    return httpx.AsyncClient(
        base_url=f"{scheme}://{ch_settings.host}:{ch_settings.port}",
        auth=(ch_settings.user, ch_settings.password),
        verify=ch_settings.verify,
        timeout=httpx.Timeout(30.0),
    )


def _seed_user(ch_client, ch_settings, username: str) -> None:
    init_user_rights(ch_client, username=username, groups=[], settings=ch_settings)
    ch_client.command(f"GRANT SELECT ON *.* TO `{username}_USER`")


def test_query_as_user_impersonates(ch_client, ch_settings, prefix) -> None:
    username = f"{prefix}_alice"
    _seed_user(ch_client, ch_settings, username)

    async def run():
        async with _http_client(ch_settings) as http_client:
            handle = ClickHouseHandle(
                client=ch_client, http_client=http_client, username=username
            )
            return await handle.query_as_user(
                "SELECT currentUser() AS cu, authenticatedUser() AS au FROM system.one"
            )

    rows = asyncio.run(run())
    assert rows == [{"cu": username, "au": ch_settings.user}], rows


def test_query_as_service_does_not_impersonate(ch_client, ch_settings, prefix) -> None:
    async def run():
        async with _http_client(ch_settings) as http_client:
            handle = ClickHouseAdminHandle(
                client=ch_client,
                http_client=http_client,
                username=f"{prefix}_unused",
                settings=ch_settings,
            )
            result = await handle.query_as_service(
                "SELECT currentUser() AS cu FROM system.one"
            )
            return list(result.named_results())

    rows = asyncio.run(run())
    assert rows == [{"cu": ch_settings.user}], rows


def test_admin_handle_query_as_user_still_impersonates(
    ch_client, ch_settings, prefix
) -> None:
    username = f"{prefix}_admin_imp"
    _seed_user(ch_client, ch_settings, username)

    async def run():
        async with _http_client(ch_settings) as http_client:
            handle = ClickHouseAdminHandle(
                client=ch_client,
                http_client=http_client,
                username=username,
                settings=ch_settings,
            )
            return await handle.query_as_user(
                "SELECT currentUser() AS cu FROM system.one"
            )

    rows = asyncio.run(run())
    assert rows == [{"cu": username}], rows


def test_query_as_user_passes_parameters(ch_client, ch_settings, prefix) -> None:
    username = f"{prefix}_paramuser"
    _seed_user(ch_client, ch_settings, username)

    async def run():
        async with _http_client(ch_settings) as http_client:
            handle = ClickHouseHandle(
                client=ch_client, http_client=http_client, username=username
            )
            return await handle.query_as_user(
                "SELECT {x:Int32} AS v FROM system.one", parameters={"x": 42}
            )

    rows = asyncio.run(run())
    assert rows == [{"v": 42}], rows


def test_query_as_user_multi_row(ch_client, ch_settings, prefix) -> None:
    """Multi-row impersonated query — JSONEachRow returns one dict per row."""
    username = f"{prefix}_multi"
    _seed_user(ch_client, ch_settings, username)

    async def run():
        async with _http_client(ch_settings) as http_client:
            handle = ClickHouseHandle(
                client=ch_client, http_client=http_client, username=username
            )
            return await handle.query_as_user(
                "SELECT number AS n, number * 2 AS doubled FROM system.numbers LIMIT 3"
            )

    rows = asyncio.run(run())
    assert rows == [
        {"n": 0, "doubled": 0},
        {"n": 1, "doubled": 2},
        {"n": 2, "doubled": 4},
    ], rows
