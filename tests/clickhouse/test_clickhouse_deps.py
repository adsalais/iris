"""Unit tests for the ClickHouse FastAPI handle providers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from iris.auth.deps import _require_session
from iris.auth.exceptions import install_exception_handlers
from iris.auth.identity import AuthSession, User
from iris.auth.session import EMPTY_RIGHTS, Rights
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.deps import (
    get_clickhouse_handle,
    require_clickhouse_admin,
    require_clickhouse_database_admin,
    require_clickhouse_database_creator,
)
from iris.clickhouse.handle import (
    ClickHouseAdminHandle,
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
    ClickHouseHandle,
)


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


def _session(*, rights: Rights = EMPTY_RIGHTS) -> AuthSession:
    user = User(
        subject="mock:alice",
        username="alice",
        display_name="Alice",
        groups=("admins",),
    )
    now = datetime.now(UTC)
    return AuthSession(
        id="sid",
        user=user,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        data={},
        rights=rights,
    )


def _make_app(*, rights: Rights = EMPTY_RIGHTS) -> FastAPI:
    app = FastAPI()
    app.state.clickhouse_client = MagicMock()
    app.state.clickhouse_settings = _settings()
    app.state.clickhouse_http_client = httpx.AsyncClient(
        base_url="http://h:1",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"{}\n")),
    )
    app.state.templates = MagicMock()
    install_exception_handlers(app, cookie_name="iris_session")

    async def fake_session() -> AuthSession:
        return _session(rights=rights)

    app.dependency_overrides[_require_session] = fake_session
    return app


def test_get_clickhouse_handle_returns_handle_for_session() -> None:
    app = _make_app()

    @app.get("/use")
    async def use(handle: ClickHouseHandle = Depends(get_clickhouse_handle)) -> dict[str, Any]:
        return {"username": handle._username}

    response = TestClient(app).get("/use")
    assert response.status_code == 200
    assert response.json() == {"username": "alice"}


def test_require_clickhouse_admin_403s_when_not_admin() -> None:
    app = _make_app(rights=EMPTY_RIGHTS)

    @app.get("/admin")
    async def admin_route(
        handle: ClickHouseAdminHandle = Depends(require_clickhouse_admin),
    ) -> dict[str, Any]:
        return {"ok": True}

    response = TestClient(app).get(
        "/admin", headers={"accept": "application/json"}
    )
    assert response.status_code == 403


def test_require_clickhouse_admin_admits_when_is_admin() -> None:
    rights = Rights(
        is_admin=True,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    app = _make_app(rights=rights)

    @app.get("/admin")
    async def admin_route(
        handle: ClickHouseAdminHandle = Depends(require_clickhouse_admin),
    ) -> dict[str, Any]:
        return {"ok": True, "username": handle._username}

    response = TestClient(app).get("/admin")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "username": "alice"}


def test_require_clickhouse_database_creator_admits_creator() -> None:
    rights = Rights(
        is_admin=False,
        can_create_database=True,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    app = _make_app(rights=rights)

    @app.post("/db/{database}")
    async def create(
        database: str,
        handle: ClickHouseDatabaseCreatorHandle = Depends(
            require_clickhouse_database_creator
        ),
    ) -> dict[str, Any]:
        return {"ok": True}

    response = TestClient(app).post("/db/finance")
    assert response.status_code == 200


def test_require_clickhouse_database_creator_403s_when_neither_admin_nor_creator() -> None:
    app = _make_app(rights=EMPTY_RIGHTS)

    @app.post("/db/{database}")
    async def create(
        database: str,
        handle: ClickHouseDatabaseCreatorHandle = Depends(
            require_clickhouse_database_creator
        ),
    ) -> dict[str, Any]:
        return {"ok": True}

    response = TestClient(app).post(
        "/db/finance", headers={"accept": "application/json"}
    )
    assert response.status_code == 403


def test_require_clickhouse_database_admin_admits_for_db_admin() -> None:
    rights = Rights(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset({"finance"}),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    app = _make_app(rights=rights)

    @app.post("/db/{database}/admin")
    async def admin(
        database: str,
        handle: ClickHouseDatabaseAdminHandle = Depends(
            require_clickhouse_database_admin
        ),
    ) -> dict[str, Any]:
        return {"db": handle._database}

    response = TestClient(app).post("/db/finance/admin")
    assert response.status_code == 200
    assert response.json() == {"db": "finance"}


def test_require_clickhouse_database_admin_403s_for_other_db() -> None:
    rights = Rights(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset({"finance"}),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    app = _make_app(rights=rights)

    @app.post("/db/{database}/admin")
    async def admin(
        database: str,
        handle: ClickHouseDatabaseAdminHandle = Depends(
            require_clickhouse_database_admin
        ),
    ) -> dict[str, Any]:
        return {"ok": True}

    response = TestClient(app).post(
        "/db/hr/admin", headers={"accept": "application/json"}
    )
    assert response.status_code == 403
