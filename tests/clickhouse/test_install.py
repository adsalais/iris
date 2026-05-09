"""install(app) wires the ClickHouse client into the FastAPI app and registers
a provisioning hook on the auth post-login list.

The hook now does two things per login: provision_user (CH user/role
provisioning) and derive_capabilities (cache the Capabilities view on the session row).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from iris.auth.identity import User
from iris.auth.store import SessionStore
from iris.clickhouse.install import install
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX


def _make_app() -> FastAPI:
    """A fresh FastAPI with the bits install() expects from iris.auth.install:
    a post-login hook list, the auth bootstrap username (None disables seed),
    and a SessionStore so the post-login hook can persist capabilities."""
    app = FastAPI()
    app.state.post_login_hooks = []
    app.state.auth_db_path = ":memory:"
    app.state.auth_bootstrap_user = None
    app.state.auth_session_store = SessionStore(
        path=":memory:", ttl_seconds=60, absolute_ttl_seconds=3600
    )
    return app


def test_install_populates_app_state(ch_settings) -> None:
    app = _make_app()
    install(app)

    assert app.state.clickhouse_client is not None
    assert app.state.clickhouse_settings is not None
    assert app.state.clickhouse_http_client is not None
    # CH install registers exactly one shutdown hook (the http client closer).
    assert len(app.state.shutdown_hooks) == 1
    assert callable(app.state.shutdown_hooks[-1])
    assert len(app.state.post_login_hooks) == 1


def test_install_http_client_aclose_runs_clean(ch_settings) -> None:
    app = _make_app()
    install(app)

    asyncio.run(app.state.shutdown_hooks[-1]())
    assert app.state.clickhouse_http_client.is_closed


def test_install_appends_to_existing_hooks(ch_settings) -> None:
    app = _make_app()

    async def existing_hook(_user: User, _sid: str) -> None:
        pass

    app.state.post_login_hooks = [existing_hook]
    install(app)

    assert len(app.state.post_login_hooks) == 2
    assert app.state.post_login_hooks[0] is existing_hook


def test_install_creates_post_login_hooks_list_if_missing(ch_settings) -> None:
    """install() is robust to call order — it creates the hook list if absent."""
    app = FastAPI()
    app.state.auth_db_path = ":memory:"
    app.state.auth_bootstrap_user = None
    app.state.auth_session_store = SessionStore(
        path=":memory:", ttl_seconds=60, absolute_ttl_seconds=3600
    )
    install(app)
    assert isinstance(app.state.post_login_hooks, list)
    assert len(app.state.post_login_hooks) == 1


def test_install_hook_provisions_user_and_persists_capabilities(ch_settings, prefix) -> None:
    """The provisioning hook creates the user/role/grants in CH and writes a
    Capabilities row to the session store."""
    app = _make_app()
    install(app)

    user = User(
        subject=f"mock:{prefix}_alice",
        username=f"{prefix}_alice",
        display_name="Alice",
        groups=(f"{prefix}_admins",),
    )

    # Pre-create a session row so the hook can update it.
    sess = asyncio.run(app.state.auth_session_store.create(user))

    hook = app.state.post_login_hooks[0]
    asyncio.run(hook(user, sess.id))

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

    refreshed = asyncio.run(app.state.auth_session_store.get_and_refresh(sess.id))
    assert refreshed is not None
    # No tier-role grants made for this user → empty tiers, but the capabilities
    # field is now populated (not None).
    assert refreshed.capabilities.db_admin == frozenset()
    assert refreshed.capabilities.db_writer == frozenset()
    assert refreshed.capabilities.db_reader == frozenset()


def test_install_fails_loud_when_ensure_service_admin_fails(monkeypatch) -> None:
    """If CH is unreachable, install() raises and build_app refuses to boot."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKHOUSE_PORT", "1")
    monkeypatch.setenv("CLICKHOUSE_USER", "iris_svc")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "x")
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")
    monkeypatch.setenv("CLICKHOUSE_VERIFY", "false")
    monkeypatch.delenv("CLICKHOUSE_CA_CERT_PATH", raising=False)

    app = _make_app()
    with pytest.raises(Exception):
        install(app)
