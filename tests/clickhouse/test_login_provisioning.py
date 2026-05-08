"""Bridge tests: form-login through TestClient triggers init_user_rights.

These build a real iris app with install_clickhouse=True and drive a form-login
through TestClient against the testcontainer. They verify the post-login hook
actually creates the CH user/role/group memberships.

ch_settings ensures the CLICKHOUSE_* env vars point at the testcontainer
before each test calls build_app(install_clickhouse=True).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from iris.app import build_app
from iris.auth.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX


def _login(client: TestClient, *, username: str, password: str) -> None:
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    response = client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": username,
            "password": password,
            "next": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text


def test_form_login_provisions_user_in_clickhouse(
    ch_settings, monkeypatch, prefix
) -> None:
    username = f"{prefix}_alice"
    monkeypatch.setenv("MOCK_USERNAME", username)
    monkeypatch.setenv("MOCK_PASSWORD", "secret")
    monkeypatch.setenv("MOCK_GROUPS", f"{prefix}_admins")
    monkeypatch.setenv("MOCK_DISPLAY_NAME", "Alice")

    app = build_app(install_clickhouse=True)
    try:
        client = TestClient(app)
        _login(client, username=username, password="secret")

        ch = app.state.clickhouse_client
        user_rows = list(
            ch.query(
                "SELECT name FROM system.users WHERE name = {u:String}",
                parameters={"u": username},
            ).named_results()
        )
        assert len(user_rows) == 1, user_rows

        role_rows = list(
            ch.query(
                "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String}",
                parameters={"u": username},
            ).named_results()
        )
        names = {r["granted_role_name"] for r in role_rows}
        assert f"{username}{USER_ROLE_SUFFIX}" in names
        assert f"{prefix}_admins{GROUP_ROLE_SUFFIX}" in names
    finally:
        # Best-effort: run the registered shutdown hooks to avoid leak warnings.
        import asyncio
        for hook in reversed(app.state.shutdown_hooks):
            asyncio.run(hook())


def test_second_login_reconciles_group_change(
    ch_settings, monkeypatch, prefix
) -> None:
    username = f"{prefix}_bob"
    monkeypatch.setenv("MOCK_USERNAME", username)
    monkeypatch.setenv("MOCK_PASSWORD", "secret")
    monkeypatch.setenv("MOCK_GROUPS", f"{prefix}_a")
    monkeypatch.setenv("MOCK_DISPLAY_NAME", "Bob")

    app1 = build_app(install_clickhouse=True)
    try:
        _login(TestClient(app1), username=username, password="secret")
    finally:
        import asyncio
        for hook in reversed(app1.state.shutdown_hooks):
            asyncio.run(hook())

    monkeypatch.setenv("MOCK_GROUPS", f"{prefix}_b")
    app2 = build_app(install_clickhouse=True)
    try:
        _login(TestClient(app2), username=username, password="secret")

        ch = app2.state.clickhouse_client
        role_rows = list(
            ch.query(
                "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String}",
                parameters={"u": username},
            ).named_results()
        )
        names = {r["granted_role_name"] for r in role_rows}
        assert f"{prefix}_b{GROUP_ROLE_SUFFIX}" in names
        assert f"{prefix}_a{GROUP_ROLE_SUFFIX}" not in names
    finally:
        import asyncio
        for hook in reversed(app2.state.shutdown_hooks):
            asyncio.run(hook())


def test_build_app_fails_loud_when_clickhouse_unreachable(monkeypatch) -> None:
    """ensure_service_admin runs at install time; CH unreachable => app refuses to boot."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKHOUSE_PORT", "1")  # closed port
    monkeypatch.setenv("CLICKHOUSE_USER", "iris_svc")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "x")
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")
    monkeypatch.setenv("CLICKHOUSE_VERIFY", "false")
    monkeypatch.setenv("CLICKHOUSE_SERVICE_ADMIN_USER", "iris_svc")
    monkeypatch.setenv("CLICKHOUSE_SERVICE_ADMIN_ROLE", "service_admin_role")
    monkeypatch.delenv("CLICKHOUSE_CA_CERT_PATH", raising=False)

    with pytest.raises(Exception):
        build_app(install_clickhouse=True)
