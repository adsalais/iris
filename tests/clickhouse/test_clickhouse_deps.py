"""Unit tests for the ClickHouse FastAPI deps."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

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
