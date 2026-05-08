"""Unit tests for the standalone CH operation functions in iris.clickhouse.handle.

The query_as_user_impl path goes through httpx.AsyncClient (mocked here via
httpx.MockTransport); the service-identity / DDL / audit functions go
through a mocked clickhouse-connect Client.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from clickhouse_connect.driver.query import QueryResult

from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.handle import (
    add_row_policy_impl,
    grant_select_to_database_impl,
    query_as_service_impl,
    query_as_user_impl,
    reprovision_user_impl,
    user_grants_impl,
)


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


def _http_client(handler: httpx.MockTransport | None = None) -> httpx.AsyncClient:
    transport = handler or httpx.MockTransport(
        lambda req: httpx.Response(200, content=b"{}\n")
    )
    return httpx.AsyncClient(base_url="http://h:1", transport=transport)


def test_query_as_user_prepends_execute_as_in_http_body() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b'{"v":1}\n')

    rows = asyncio.run(
        query_as_user_impl(
            _http_client(httpx.MockTransport(handler)),
            username="alice",
            sql="SELECT 1 AS v",
        )
    )

    assert rows == [{"v": 1}]
    assert len(captured) == 1
    body = captured[0].content.decode()
    assert body.startswith("EXECUTE AS `alice` "), body
    assert body.endswith("SELECT 1 AS v"), body


def test_query_as_user_sets_default_format_jsoneachrow() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"{}\n")

    asyncio.run(
        query_as_user_impl(
            _http_client(httpx.MockTransport(handler)),
            username="alice",
            sql="SELECT 1",
        )
    )

    assert captured[0].url.params["default_format"] == "JSONEachRow"


def test_query_as_user_passes_parameters_as_param_prefixed_url_params() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"{}\n")

    asyncio.run(
        query_as_user_impl(
            _http_client(httpx.MockTransport(handler)),
            username="alice",
            sql="SELECT {x:Int32}",
            parameters={"x": 7},
        )
    )

    assert captured[0].url.params["param_x"] == "7"


def test_query_as_user_database_kwarg_sets_url_param() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"{}\n")

    asyncio.run(
        query_as_user_impl(
            _http_client(httpx.MockTransport(handler)),
            username="alice",
            sql="SELECT count() FROM t",
            database="finance",
        )
    )

    assert captured[0].url.params["database"] == "finance"


def test_query_as_user_no_database_kwarg_omits_url_param() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"{}\n")

    asyncio.run(
        query_as_user_impl(
            _http_client(httpx.MockTransport(handler)),
            username="alice",
            sql="SELECT 1",
        )
    )

    assert "database" not in captured[0].url.params


def test_query_as_user_parses_multi_row_jsoneachrow() -> None:
    body_lines = [
        json.dumps({"n": 0}),
        json.dumps({"n": 1}),
        json.dumps({"n": 2}),
    ]
    handler = httpx.MockTransport(
        lambda _req: httpx.Response(
            200, content="\n".join(body_lines).encode() + b"\n"
        )
    )
    rows = asyncio.run(
        query_as_user_impl(
            _http_client(handler),
            username="alice",
            sql="SELECT number AS n FROM system.numbers LIMIT 3",
        )
    )
    assert rows == [{"n": 0}, {"n": 1}, {"n": 2}]


def test_query_as_user_returns_empty_list_for_empty_response() -> None:
    handler = httpx.MockTransport(lambda _req: httpx.Response(200, content=b""))
    rows = asyncio.run(
        query_as_user_impl(
            _http_client(handler),
            username="alice",
            sql="INSERT INTO t VALUES (1)",
        )
    )
    assert rows == []


def test_query_as_user_raises_on_http_error() -> None:
    handler = httpx.MockTransport(lambda _req: httpx.Response(500, content=b"boom"))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            query_as_user_impl(
                _http_client(handler),
                username="alice",
                sql="SELECT 1",
            )
        )


def test_query_as_user_rejects_invalid_username() -> None:
    with pytest.raises(ValueError):
        asyncio.run(
            query_as_user_impl(
                _http_client(),
                username="alice; DROP USER bob",
                sql="SELECT 1",
            )
        )


def test_query_as_service_does_not_prepend_execute_as() -> None:
    """query_as_service uses clickhouse-connect, not httpx."""
    client = MagicMock()
    client.query.return_value = MagicMock(spec=QueryResult)

    asyncio.run(query_as_service_impl(client, sql="SELECT 1"))
    args, kwargs = client.query.call_args
    sql = args[0] if args else kwargs["query"]
    assert "EXECUTE AS" not in sql
    assert sql == "SELECT 1"


def test_query_as_service_passes_database_kwarg() -> None:
    client = MagicMock()
    client.query.return_value = MagicMock(spec=QueryResult)

    asyncio.run(query_as_service_impl(client, sql="SELECT 1", database="finance"))
    _, kwargs = client.query.call_args
    assert kwargs["database"] == "finance"


def test_reprovision_user_delegates_to_init_user_rights() -> None:
    client = MagicMock()
    with patch("iris.clickhouse.handle.init_user_rights") as mock_init:
        asyncio.run(
            reprovision_user_impl(
                client, username="bob", groups=["sales"], settings=_settings()
            )
        )

    mock_init.assert_called_once()
    _, kwargs = mock_init.call_args
    assert kwargs["username"] == "bob"
    assert kwargs["groups"] == ["sales"]


def test_user_grants_impl_delegates() -> None:
    client = MagicMock()
    with patch("iris.clickhouse.handle.user_grants") as mock_ug:
        mock_ug.return_value = [{"x": 1}]
        result = asyncio.run(user_grants_impl(client, username="alice"))
    assert result == [{"x": 1}]
    mock_ug.assert_called_once()


def test_grant_select_to_database_impl_delegates() -> None:
    client = MagicMock()
    with patch("iris.clickhouse.handle.grant_select_to_database") as mock_grant:
        asyncio.run(
            grant_select_to_database_impl(client, database="orders", role="reader")
        )
    mock_grant.assert_called_once()
    _, kwargs = mock_grant.call_args
    assert kwargs["database"] == "orders"
    assert kwargs["role"] == "reader"


def test_add_row_policy_impl_delegates() -> None:
    client = MagicMock()
    with patch("iris.clickhouse.handle.add_row_policy") as mock_add:
        asyncio.run(
            add_row_policy_impl(
                client,
                database="orders",
                table="lines",
                column="region",
                role="reader",
                value="EU",
                settings=_settings(),
            )
        )
    mock_add.assert_called_once()
    _, kwargs = mock_add.call_args
    assert kwargs["database"] == "orders"
    assert kwargs["table"] == "lines"
    assert kwargs["column"] == "region"
    assert kwargs["value"] == "EU"
