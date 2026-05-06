"""Unit tests for require_clickhouse_database_creator and
require_clickhouse_database_admin. Mirrors the structure of the existing
tests/clickhouse/test_clickhouse_deps.py — fast, no testcontainer, uses
FastAPI dependency overrides.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from iris.auth.authz.mapping import RoleDef, RoleMapping
from iris.auth.identity import User
from iris.auth.session import Session
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.deps import (
    CLICKHOUSE_DATABASE_CREATOR_ROLE,
    require_clickhouse_database_admin,
    require_clickhouse_database_creator,
)
from iris.clickhouse.handle import (
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
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


def _session(*, username: str = "alice", roles: frozenset[str] = frozenset()) -> Session:
    user = User(
        subject="mock:" + username,
        username=username,
        display_name=username.title(),
        groups=(),
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


def _mapping(roles: list[str]) -> RoleMapping:
    role_defs = {
        r: RoleDef(name=r, groups=frozenset(), users_lower=frozenset(), includes=())
        for r in roles
    }
    closure = {r: frozenset({r}) for r in roles}
    return RoleMapping(roles=role_defs, closure=closure)


def _make_app(*, db_admin_store=None, authz_store=None) -> FastAPI:
    app = FastAPI()
    app.state.clickhouse_client = MagicMock()
    app.state.clickhouse_http_client = httpx.AsyncClient(
        base_url="http://h:1",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"")),
    )
    app.state.clickhouse_settings = _settings()
    app.state.clickhouse_database_admins = db_admin_store or MagicMock()
    app.state.authz_store = authz_store or MagicMock()
    return app


# ---- require_clickhouse_database_creator ----


def test_require_creator_500s_when_role_missing_from_yaml() -> None:
    from iris.auth.authz.core import current_mapping
    from iris.auth.deps import _build_required
    from iris.auth.exceptions import install_exception_handlers

    app = _make_app()
    app.state.templates = MagicMock()
    install_exception_handlers(app, cookie_name="iris_session")

    async def fake_session() -> Session:
        return _session(roles=frozenset())

    async def fake_mapping():
        return _mapping([])

    app.dependency_overrides[_build_required] = fake_session
    app.dependency_overrides[current_mapping] = fake_mapping

    @app.get("/create")
    async def create(
        handle: ClickHouseDatabaseCreatorHandle = Depends(
            require_clickhouse_database_creator
        ),
    ) -> dict[str, Any]:
        return {"ok": True}

    response = TestClient(app, raise_server_exceptions=False).get("/create")
    assert response.status_code == 500


def test_require_creator_403s_when_user_lacks_role() -> None:
    from iris.auth.authz.core import current_mapping
    from iris.auth.deps import _build_required
    from iris.auth.exceptions import install_exception_handlers

    app = _make_app()
    app.state.templates = MagicMock()
    install_exception_handlers(app, cookie_name="iris_session")

    async def fake_session() -> Session:
        return _session(roles=frozenset({"reader"}))

    async def fake_mapping():
        return _mapping([CLICKHOUSE_DATABASE_CREATOR_ROLE])

    app.dependency_overrides[_build_required] = fake_session
    app.dependency_overrides[current_mapping] = fake_mapping

    @app.get("/create")
    async def create(
        handle: ClickHouseDatabaseCreatorHandle = Depends(
            require_clickhouse_database_creator
        ),
    ) -> dict[str, Any]:
        return {"ok": True}

    response = TestClient(app).get("/create", headers={"accept": "application/json"})
    assert response.status_code == 403


def test_require_creator_returns_handle_on_success() -> None:
    from iris.auth.authz.core import current_mapping
    from iris.auth.deps import _build_required

    db_store = MagicMock()
    db_store.add_admin_user = AsyncMock()
    app = _make_app(db_admin_store=db_store)

    async def fake_session() -> Session:
        return _session(roles=frozenset({CLICKHOUSE_DATABASE_CREATOR_ROLE}))

    async def fake_mapping():
        return _mapping([CLICKHOUSE_DATABASE_CREATOR_ROLE])

    app.dependency_overrides[_build_required] = fake_session
    app.dependency_overrides[current_mapping] = fake_mapping

    @app.get("/create")
    async def create(
        handle: ClickHouseDatabaseCreatorHandle = Depends(
            require_clickhouse_database_creator
        ),
    ) -> dict[str, Any]:
        return {"username": handle._username}

    response = TestClient(app).get("/create")
    assert response.status_code == 200
    assert response.json() == {"username": "alice"}


# ---- require_clickhouse_database_admin ----


def test_require_db_admin_403s_for_non_admin() -> None:
    from iris.auth.deps import _build_required
    from iris.auth.exceptions import install_exception_handlers

    db_store = MagicMock()
    db_store.is_admin = AsyncMock(return_value=False)
    app = _make_app(db_admin_store=db_store)
    app.state.templates = MagicMock()
    install_exception_handlers(app, cookie_name="iris_session")

    async def fake_session() -> Session:
        return _session(username="dave", roles=frozenset())

    app.dependency_overrides[_build_required] = fake_session

    @app.get("/db/{database}")
    async def admin_route(
        database: str,
        handle: ClickHouseDatabaseAdminHandle = Depends(
            require_clickhouse_database_admin
        ),
    ) -> dict[str, Any]:
        return {"ok": True}

    response = TestClient(app).get(
        "/db/orders", headers={"accept": "application/json"}
    )
    assert response.status_code == 403


def test_require_db_admin_admits_listed_user() -> None:
    from iris.auth.deps import _build_required

    db_store = MagicMock()
    db_store.is_admin = AsyncMock(return_value=True)
    app = _make_app(db_admin_store=db_store)

    async def fake_session() -> Session:
        return _session(username="alice", roles=frozenset())

    app.dependency_overrides[_build_required] = fake_session

    @app.get("/db/{database}")
    async def admin_route(
        database: str,
        handle: ClickHouseDatabaseAdminHandle = Depends(
            require_clickhouse_database_admin
        ),
    ) -> dict[str, Any]:
        return {"db": handle._database, "user": handle._username}

    response = TestClient(app).get("/db/orders")
    assert response.status_code == 200
    assert response.json() == {"db": "orders", "user": "alice"}
    db_store.is_admin.assert_awaited_once_with(
        database="orders", username_lower="alice", roles=frozenset()
    )


def test_require_db_admin_short_circuits_for_clickhouse_admin() -> None:
    """Global admin: is_admin sees clickhouse_admin in roles and returns True
    without consulting the per-DB tables."""
    from iris.auth.deps import _build_required

    db_store = MagicMock()
    async def fake_is_admin(*, database, username_lower, roles):
        return "clickhouse_admin" in roles

    db_store.is_admin = AsyncMock(side_effect=fake_is_admin)
    app = _make_app(db_admin_store=db_store)

    async def fake_session() -> Session:
        return _session(username="globaladmin", roles=frozenset({"clickhouse_admin"}))

    app.dependency_overrides[_build_required] = fake_session

    @app.get("/db/{database}")
    async def admin_route(
        database: str,
        handle: ClickHouseDatabaseAdminHandle = Depends(
            require_clickhouse_database_admin
        ),
    ) -> dict[str, Any]:
        return {"db": handle._database}

    response = TestClient(app).get("/db/secret_db")
    assert response.status_code == 200
    assert response.json() == {"db": "secret_db"}


def test_require_db_admin_rejects_invalid_database_name() -> None:
    """Invalid CH identifier as database name -> 500 (programming error)."""
    from iris.auth.deps import _build_required

    app = _make_app()

    async def fake_session() -> Session:
        return _session(roles=frozenset({"clickhouse_admin"}))

    app.dependency_overrides[_build_required] = fake_session

    @app.get("/db/{database}")
    async def admin_route(
        database: str,
        handle: ClickHouseDatabaseAdminHandle = Depends(
            require_clickhouse_database_admin
        ),
    ) -> dict[str, Any]:
        return {"ok": True}

    response = TestClient(app, raise_server_exceptions=False).get(
        "/db/bad name with spaces"
    )
    assert response.status_code == 500
