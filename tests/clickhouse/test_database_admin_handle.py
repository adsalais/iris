"""Unit tests for ClickHouseDatabaseAdminHandle."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from iris.auth.authz.mapping import RoleDef, RoleMapping, RoleMappingError
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.handle import ClickHouseDatabaseAdminHandle


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


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://h:1",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"")),
    )


def _empty_mapping() -> RoleMapping:
    return RoleMapping(roles={}, closure={})


def _mapping_with(role_name: str) -> RoleMapping:
    role = RoleDef(
        name=role_name,
        groups=frozenset(),
        users_lower=frozenset(),
        includes=(),
    )
    return RoleMapping(
        roles={role_name: role},
        closure={role_name: frozenset({role_name})},
    )


def _make_handle(
    *,
    client: Any = None,
    db_admin_store: Any = None,
    authz_store: Any = None,
    database: str = "orders",
    username: str = "alice",
) -> ClickHouseDatabaseAdminHandle:
    return ClickHouseDatabaseAdminHandle(
        client=client or MagicMock(),
        http_client=_http_client(),
        settings=_settings(),
        db_admin_store=db_admin_store or MagicMock(),
        authz_store=authz_store or MagicMock(),
        database=database,
        username=username,
    )


# ---- grants ----

def test_grant_select_to_user_translates_to_user_role() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    asyncio.run(handle.grant_select_to_user("bob"))
    args, _ = client.command.call_args
    assert args[0] == "GRANT SELECT ON `orders`.* TO `bob_USER`"


def test_grant_select_to_group_translates_to_group_role() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    asyncio.run(handle.grant_select_to_group("editors"))
    args, _ = client.command.call_args
    assert args[0] == "GRANT SELECT ON `orders`.* TO `editors_GRP`"


def test_revoke_select_from_user_translates_to_user_role() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    asyncio.run(handle.revoke_select_from_user("bob"))
    args, _ = client.command.call_args
    assert args[0] == "REVOKE SELECT ON `orders`.* FROM `bob_USER`"


def test_revoke_select_from_group_translates_to_group_role() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    asyncio.run(handle.revoke_select_from_group("editors"))
    args, _ = client.command.call_args
    assert args[0] == "REVOKE SELECT ON `orders`.* FROM `editors_GRP`"


# ---- row policies ----

def test_add_row_policy_for_user_calls_underlying_helper() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    with patch("iris.clickhouse.handle.add_row_policy") as mock_add:
        asyncio.run(
            handle.add_row_policy_for_user(
                table="lines", column="region", username="bob", value="EU"
            )
        )
    mock_add.assert_called_once()
    _, kwargs = mock_add.call_args
    assert kwargs["database"] == "orders"
    assert kwargs["table"] == "lines"
    assert kwargs["column"] == "region"
    assert kwargs["role"] == "bob_USER"
    assert kwargs["value"] == "EU"


def test_add_row_policy_for_group_calls_underlying_helper() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    with patch("iris.clickhouse.handle.add_row_policy") as mock_add:
        asyncio.run(
            handle.add_row_policy_for_group(
                table="lines", column="region", group="editors", value="EU"
            )
        )
    _, kwargs = mock_add.call_args
    assert kwargs["role"] == "editors_GRP"


def test_revoke_row_policy_for_user_calls_underlying_helper() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    with patch("iris.clickhouse.handle.revoke_row_policy") as mock_revoke:
        asyncio.run(
            handle.revoke_row_policy_for_user(
                table="lines", username="bob", value="EU"
            )
        )
    _, kwargs = mock_revoke.call_args
    assert kwargs["role"] == "bob_USER"


def test_revoke_row_policy_for_group_calls_underlying_helper() -> None:
    client = MagicMock()
    handle = _make_handle(client=client, database="orders")
    with patch("iris.clickhouse.handle.revoke_row_policy") as mock_revoke:
        asyncio.run(
            handle.revoke_row_policy_for_group(
                table="lines", group="editors", value="EU"
            )
        )
    _, kwargs = mock_revoke.call_args
    assert kwargs["role"] == "editors_GRP"


# ---- delegation ----

def test_add_admin_user_delegates_to_store() -> None:
    store = MagicMock()
    store.add_admin_user = AsyncMock()
    handle = _make_handle(db_admin_store=store, database="orders")
    asyncio.run(handle.add_admin_user("bob"))
    store.add_admin_user.assert_awaited_once_with(database="orders", username="bob")


def test_remove_admin_user_delegates_to_store() -> None:
    store = MagicMock()
    store.remove_admin_user = AsyncMock()
    handle = _make_handle(db_admin_store=store, database="orders")
    asyncio.run(handle.remove_admin_user("bob"))
    store.remove_admin_user.assert_awaited_once_with(database="orders", username="bob")


def test_add_admin_role_validates_role_exists_in_authz() -> None:
    db_store = MagicMock()
    db_store.add_admin_role = AsyncMock()
    authz = MagicMock()
    authz.get_mapping = AsyncMock(return_value=_mapping_with("ops"))
    handle = _make_handle(db_admin_store=db_store, authz_store=authz, database="orders")

    asyncio.run(handle.add_admin_role("ops"))

    authz.get_mapping.assert_awaited_once_with()
    db_store.add_admin_role.assert_awaited_once_with(database="orders", role="ops")


def test_add_admin_role_rejects_undefined_role() -> None:
    db_store = MagicMock()
    db_store.add_admin_role = AsyncMock()
    authz = MagicMock()
    authz.get_mapping = AsyncMock(return_value=_empty_mapping())
    handle = _make_handle(db_admin_store=db_store, authz_store=authz, database="orders")

    with pytest.raises(RoleMappingError):
        asyncio.run(handle.add_admin_role("nope"))

    db_store.add_admin_role.assert_not_awaited()


def test_remove_admin_role_does_not_validate() -> None:
    """remove_admin_role can target a role that no longer exists in authz —
    e.g., to clean up a stale mapping after the role was deleted."""
    db_store = MagicMock()
    db_store.remove_admin_role = AsyncMock()
    handle = _make_handle(db_admin_store=db_store, database="orders")
    asyncio.run(handle.remove_admin_role("ops"))
    db_store.remove_admin_role.assert_awaited_once_with(database="orders", role="ops")


# ---- listing ----

def test_list_admin_users_delegates_to_store() -> None:
    store = MagicMock()
    store.list_admin_users = AsyncMock(return_value=["alice", "bob"])
    handle = _make_handle(db_admin_store=store, database="orders")
    rows = asyncio.run(handle.list_admin_users())
    store.list_admin_users.assert_awaited_once_with(database="orders")
    assert rows == ["alice", "bob"]


def test_list_admin_roles_delegates_to_store() -> None:
    store = MagicMock()
    store.list_admin_roles = AsyncMock(return_value=["ops"])
    handle = _make_handle(db_admin_store=store, database="orders")
    rows = asyncio.run(handle.list_admin_roles())
    store.list_admin_roles.assert_awaited_once_with(database="orders")
    assert rows == ["ops"]


def test_list_grants_queries_system_grants_for_database() -> None:
    client = MagicMock()
    result = MagicMock()
    result.named_results.return_value = [{"role_name": "bob_USER", "access_type": "SELECT"}]
    client.query.return_value = result
    handle = _make_handle(client=client, database="orders")

    rows = asyncio.run(handle.list_grants())

    args, kwargs = client.query.call_args
    sql = args[0] if args else kwargs["query"]
    assert "system.grants" in sql
    assert kwargs["parameters"]["d"] == "orders"
    assert rows == [{"role_name": "bob_USER", "access_type": "SELECT"}]


def test_list_row_policies_queries_system_row_policies_for_database() -> None:
    client = MagicMock()
    result = MagicMock()
    result.named_results.return_value = [{"name": "orders_lines_bob_USER_EU_abc12345"}]
    client.query.return_value = result
    handle = _make_handle(client=client, database="orders")

    rows = asyncio.run(handle.list_row_policies())

    args, kwargs = client.query.call_args
    sql = args[0] if args else kwargs["query"]
    assert "system.row_policies" in sql
    assert kwargs["parameters"]["d"] == "orders"
    assert rows == [{"name": "orders_lines_bob_USER_EU_abc12345"}]
