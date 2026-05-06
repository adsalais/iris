"""Unit tests for the ClickHouse FastAPI deps."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from iris.auth.identity import User
from iris.auth.session import Session
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.deps import (
    CLICKHOUSE_ADMIN_ROLE,
    get_clickhouse_handle,
)
from iris.clickhouse.handle import ClickHouseHandle


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


def _session(*, roles: frozenset[str] = frozenset()) -> Session:
    user = User(
        subject="mock:alice",
        username="alice",
        display_name="Alice",
        groups=("admins",),
    )
    now = datetime.now(UTC)
    return Session(
        id="sid",
        user=user,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        data={},
        roles=roles,
    )


def _make_app() -> tuple[FastAPI, MagicMock]:
    app = FastAPI()
    client = MagicMock()
    app.state.clickhouse_client = client
    app.state.clickhouse_settings = _settings()
    app.state.clickhouse_http_client = httpx.AsyncClient(
        base_url="http://h:1",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"{}\n")),
    )
    return app, client


def test_get_clickhouse_handle_returns_handle_for_session() -> None:
    """The dep injects a ClickHouseHandle bound to the session's username.

    We override the auth dep on the app — the focus of this test is the CH
    dep, not the auth chain.
    """
    from iris.auth.deps import _build_required

    app, _client = _make_app()

    async def fake_session() -> Session:
        return _session()

    app.dependency_overrides[_build_required] = fake_session

    @app.get("/use")
    async def use(handle: ClickHouseHandle = Depends(get_clickhouse_handle)) -> dict[str, Any]:
        return {"username": handle._username}

    response = TestClient(app).get("/use")
    assert response.status_code == 200
    assert response.json() == {"username": "alice"}


def test_clickhouse_admin_role_constant() -> None:
    assert CLICKHOUSE_ADMIN_ROLE == "clickhouse_admin"


def _mapping_with_admin_role():
    from iris.auth.authz.mapping import RoleDef, RoleMapping

    role = RoleDef(
        name=CLICKHOUSE_ADMIN_ROLE,
        groups=frozenset({"admins"}),
        users_lower=frozenset(),
        includes=(),
    )
    return RoleMapping(
        roles={CLICKHOUSE_ADMIN_ROLE: role},
        closure={CLICKHOUSE_ADMIN_ROLE: frozenset({CLICKHOUSE_ADMIN_ROLE})},
    )


def _mapping_without_admin_role():
    from iris.auth.authz.mapping import RoleMapping

    return RoleMapping(roles={}, closure={})


def _admin_app(mapping, session_roles: frozenset[str]):
    from iris.auth.authz.core import current_mapping
    from iris.auth.deps import _build_required
    from iris.auth.exceptions import install_exception_handlers
    from iris.clickhouse.deps import require_clickhouse_admin
    from iris.clickhouse.handle import ClickHouseAdminHandle

    app, _client = _make_app()

    async def fake_session() -> Session:
        return _session(roles=session_roles)

    async def fake_mapping():
        return mapping

    app.dependency_overrides[_build_required] = fake_session
    app.dependency_overrides[current_mapping] = fake_mapping

    @app.get("/admin")
    async def admin_route(
        handle: ClickHouseAdminHandle = Depends(require_clickhouse_admin),
    ) -> dict[str, Any]:
        return {"ok": True, "username": handle._username}

    app.state.templates = MagicMock()
    install_exception_handlers(app, cookie_name="iris_session")
    return app


def test_require_clickhouse_admin_500s_when_role_missing_from_yaml() -> None:
    app = _admin_app(_mapping_without_admin_role(), frozenset())
    response = TestClient(app, raise_server_exceptions=False).get("/admin")
    assert response.status_code == 500


def test_require_clickhouse_admin_403s_when_user_lacks_role() -> None:
    app = _admin_app(_mapping_with_admin_role(), frozenset({"reader"}))
    response = TestClient(app).get(
        "/admin", headers={"accept": "application/json"}
    )
    assert response.status_code == 403


def test_require_clickhouse_admin_returns_admin_handle_on_success() -> None:
    app = _admin_app(
        _mapping_with_admin_role(), frozenset({CLICKHOUSE_ADMIN_ROLE})
    )
    response = TestClient(app).get("/admin")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "username": "alice"}
