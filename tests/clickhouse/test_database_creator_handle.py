"""Unit tests for ClickHouseDatabaseCreatorHandle.

The handle wraps a clickhouse-connect Client and a DatabaseAdminStore.
Both are mocked here.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.handle import ClickHouseDatabaseCreatorHandle


def _settings() -> ClickHouseSettings:
    return ClickHouseSettings(
        host="h",
        port=1,
        user="u",
        password="p",
        secure=False,
        verify=False,
        ca_cert_path=None,
        service_admin_user="iris_svc",
        service_admin_role="service_admin_role",
    )


def _make_handle(*, client: Any = None, store: Any = None, username: str = "alice") -> ClickHouseDatabaseCreatorHandle:
    return ClickHouseDatabaseCreatorHandle(
        client=client or MagicMock(),
        settings=_settings(),
        db_admin_store=store or MagicMock(),
        username=username,
    )


def test_create_database_issues_create_with_if_not_exists() -> None:
    client = MagicMock()
    store = MagicMock()
    store.add_admin_user = AsyncMock()
    handle = _make_handle(client=client, store=store, username="alice")

    asyncio.run(handle.create_database("orders"))

    args, _kwargs = client.command.call_args
    sql = args[0]
    assert sql == "CREATE DATABASE IF NOT EXISTS `orders`"


def test_create_database_records_user_as_admin() -> None:
    client = MagicMock()
    store = MagicMock()
    store.add_admin_user = AsyncMock()
    handle = _make_handle(client=client, store=store, username="alice")

    asyncio.run(handle.create_database("orders"))

    store.add_admin_user.assert_awaited_once_with(database="orders", username="alice")


def test_create_database_validates_name() -> None:
    client = MagicMock()
    store = MagicMock()
    store.add_admin_user = AsyncMock()
    handle = _make_handle(client=client, store=store)

    with pytest.raises(ValueError):
        asyncio.run(handle.create_database("bad name with spaces"))
    client.command.assert_not_called()
    store.add_admin_user.assert_not_called()


def test_create_database_idempotent_via_if_not_exists() -> None:
    """Two calls don't add duplicate admin rows (store's INSERT OR IGNORE handles it)."""
    client = MagicMock()
    store = MagicMock()
    store.add_admin_user = AsyncMock()
    handle = _make_handle(client=client, store=store, username="alice")

    asyncio.run(handle.create_database("orders"))
    asyncio.run(handle.create_database("orders"))

    assert client.command.call_count == 2
    assert store.add_admin_user.await_count == 2
