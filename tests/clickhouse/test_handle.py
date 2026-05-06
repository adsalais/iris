"""Unit tests for ClickHouseHandle against a mocked Client."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from clickhouse_connect.driver.query import QueryResult

from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.handle import ClickHouseAdminHandle, ClickHouseHandle


def _settings() -> ClickHouseSettings:
    return ClickHouseSettings(
        host="h",
        port=1,
        user="u",
        password="p",
        secure=True,
        verify=True,
        ca_cert_path=None,
        service_admin_user="iris_svc",
        service_admin_role="service_admin_role",
    )


def _admin_handle(client: Any) -> ClickHouseAdminHandle:
    return ClickHouseAdminHandle(client=client, username="alice", settings=_settings())


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


def test_admin_handle_subclasses_user_handle() -> None:
    client = MagicMock()
    client.query.return_value = MagicMock(spec=QueryResult)
    handle = _admin_handle(client)

    assert isinstance(handle, ClickHouseHandle)
    asyncio.run(handle.query_as_user("SELECT 1"))
    args, _kwargs = client.query.call_args
    assert args[0].startswith("EXECUTE AS `alice` ")


def test_query_as_service_does_not_prepend_execute_as() -> None:
    client = MagicMock()
    client.query.return_value = MagicMock(spec=QueryResult)
    handle = _admin_handle(client)

    asyncio.run(handle.query_as_service("SELECT 1"))
    args, kwargs = client.query.call_args
    sql = args[0] if args else kwargs["query"]
    assert "EXECUTE AS" not in sql
    assert sql == "SELECT 1"


def test_reprovision_user_delegates_to_init_user_rights() -> None:
    client = MagicMock()
    handle = _admin_handle(client)

    with patch("iris.clickhouse.handle.init_user_rights") as mock_init:
        asyncio.run(handle.reprovision_user(username="bob", groups=["sales"]))

    mock_init.assert_called_once()
    _, kwargs = mock_init.call_args
    assert kwargs["username"] == "bob"
    assert kwargs["groups"] == ["sales"]


def test_admin_audit_methods_delegate() -> None:
    client = MagicMock()
    handle = _admin_handle(client)

    with patch("iris.clickhouse.handle.user_grants") as mock_ug:
        mock_ug.return_value = [{"x": 1}]
        result = asyncio.run(handle.user_grants(username="alice"))
    assert result == [{"x": 1}]
    mock_ug.assert_called_once()


def test_admin_grant_methods_delegate() -> None:
    client = MagicMock()
    handle = _admin_handle(client)

    with patch("iris.clickhouse.handle.grant_select_to_database") as mock_grant:
        asyncio.run(handle.grant_select_to_database(database="orders", role="reader"))
    mock_grant.assert_called_once()
    _, kwargs = mock_grant.call_args
    assert kwargs["database"] == "orders"
    assert kwargs["role"] == "reader"


def test_admin_row_policy_methods_delegate() -> None:
    client = MagicMock()
    handle = _admin_handle(client)

    with patch("iris.clickhouse.handle.add_row_policy") as mock_add:
        asyncio.run(
            handle.add_row_policy(
                database="orders",
                table="lines",
                column="region",
                role="reader",
                value="EU",
            )
        )
    mock_add.assert_called_once()
    _, kwargs = mock_add.call_args
    assert kwargs["database"] == "orders"
    assert kwargs["table"] == "lines"
    assert kwargs["column"] == "region"
    assert kwargs["value"] == "EU"
