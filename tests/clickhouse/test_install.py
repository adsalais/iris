"""install(app) wires the ClickHouse client into the FastAPI app and registers
a provisioning hook on the auth post-login list."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from iris.auth.identity import User
from iris.clickhouse.install import install
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX


def test_install_populates_app_state(ch_settings) -> None:
    app = FastAPI()
    app.state.post_login_hooks = []
    app.state.auth_db_path = ":memory:"

    install(app)

    assert app.state.clickhouse_client is not None
    assert app.state.clickhouse_settings is not None
    assert app.state.clickhouse_http_client is not None
    assert callable(app.state.clickhouse_close_http)
    assert app.state.clickhouse_database_admins is not None
    assert callable(app.state.clickhouse_close_database_admins)
    assert len(app.state.post_login_hooks) == 1


def test_install_http_client_aclose_runs_clean(ch_settings) -> None:
    """The clickhouse_close_http hook should close the httpx.AsyncClient cleanly."""
    import asyncio

    app = FastAPI()
    app.state.post_login_hooks = []
    app.state.auth_db_path = ":memory:"
    install(app)

    asyncio.run(app.state.clickhouse_close_http())
    assert app.state.clickhouse_http_client.is_closed


def test_install_appends_to_existing_hooks(ch_settings) -> None:
    app = FastAPI()
    app.state.auth_db_path = ":memory:"

    async def existing_hook(_user: User) -> None:
        pass

    app.state.post_login_hooks = [existing_hook]
    install(app)

    assert len(app.state.post_login_hooks) == 2
    assert app.state.post_login_hooks[0] is existing_hook


def test_install_creates_post_login_hooks_list_if_missing(ch_settings) -> None:
    """install() doesn't require iris.auth.install to have run first; it creates
    the hook list if absent. (Production wiring still calls auth first, but
    install() is robust to call order.)"""
    app = FastAPI()
    app.state.auth_db_path = ":memory:"
    install(app)
    assert isinstance(app.state.post_login_hooks, list)
    assert len(app.state.post_login_hooks) == 1


def test_install_hook_calls_init_user_rights(ch_settings, prefix) -> None:
    """The provisioning hook actually creates the user/role/grants in CH."""
    app = FastAPI()
    app.state.post_login_hooks = []
    app.state.auth_db_path = ":memory:"
    install(app)

    user = User(
        subject=f"mock:{prefix}_alice",
        username=f"{prefix}_alice",
        display_name="Alice",
        groups=(f"{prefix}_admins",),
    )

    hook = app.state.post_login_hooks[0]
    asyncio.run(hook(user))

    client = app.state.clickhouse_client
    rows = list(
        client.query(
            "SELECT name FROM system.users WHERE name = {u:String}",
            parameters={"u": user.username},
        ).named_results()
    )
    assert len(rows) == 1, rows

    role_rows = list(
        client.query(
            "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String}",
            parameters={"u": user.username},
        ).named_results()
    )
    role_names = {r["granted_role_name"] for r in role_rows}
    assert f"{user.username}{USER_ROLE_SUFFIX}" in role_names
    assert f"{prefix}_admins{GROUP_ROLE_SUFFIX}" in role_names


def test_install_fails_loud_when_ensure_service_admin_fails(monkeypatch) -> None:
    """If CH is unreachable, install() raises and build_app refuses to boot."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKHOUSE_PORT", "1")  # closed port
    monkeypatch.setenv("CLICKHOUSE_USER", "iris_svc")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "x")
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")
    monkeypatch.setenv("CLICKHOUSE_VERIFY", "false")
    monkeypatch.setenv("CLICKHOUSE_SERVICE_ADMIN_USER", "iris_svc")
    monkeypatch.setenv("CLICKHOUSE_SERVICE_ADMIN_ROLE", "service_admin_role")
    monkeypatch.delenv("CLICKHOUSE_CA_CERT_PATH", raising=False)

    app = FastAPI()
    app.state.post_login_hooks = []
    with pytest.raises(Exception):
        install(app)
