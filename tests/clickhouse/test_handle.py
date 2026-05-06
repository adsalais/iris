"""Unit tests for ClickHouseHandle against a mocked Client."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from clickhouse_connect.driver.query import QueryResult

from iris.clickhouse.handle import ClickHouseHandle


def test_query_as_user_prepends_execute_as() -> None:
    client = MagicMock()
    client.query.return_value = MagicMock(spec=QueryResult)

    handle = ClickHouseHandle(client=client, username="alice")
    asyncio.run(handle.query_as_user("SELECT 1"))

    args, kwargs = client.query.call_args
    sql = args[0] if args else kwargs["query"]
    assert sql.startswith("EXECUTE AS `alice` "), sql
    assert sql.endswith("SELECT 1"), sql


def test_query_as_user_passes_parameters() -> None:
    client = MagicMock()
    client.query.return_value = MagicMock(spec=QueryResult)

    handle = ClickHouseHandle(client=client, username="alice")
    asyncio.run(handle.query_as_user("SELECT {x:Int32}", parameters={"x": 7}))

    _args, kwargs = client.query.call_args
    assert kwargs["parameters"] == {"x": 7}


def test_handle_rejects_invalid_username() -> None:
    client = MagicMock()
    with pytest.raises(ValueError):
        ClickHouseHandle(client=client, username="alice; DROP USER bob")
