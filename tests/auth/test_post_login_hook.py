"""The auth layer exposes a generic post-login hook list. iris.clickhouse and any
future bridge can append to it without auth depending on them."""
from __future__ import annotations

from fastapi.testclient import TestClient

from iris.app import build_app
from iris.auth.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD
from iris.auth.identity import User


def test_post_login_hooks_default_to_empty_list_after_install() -> None:
    app = build_app(install_clickhouse=False)
    assert isinstance(app.state.post_login_hooks, list)
    assert app.state.post_login_hooks == []


def test_post_login_hook_fires_on_form_login() -> None:
    app = build_app(install_clickhouse=False)
    seen: list[User] = []

    async def hook(user: User) -> None:
        seen.append(user)

    app.state.post_login_hooks.append(hook)

    client = TestClient(app)
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    response = client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "alice",
            "password": "secret",
            "next": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text

    assert len(seen) == 1
    assert seen[0].username == "alice"
    assert "admins" in seen[0].groups


def test_post_login_hook_exception_is_fail_loud() -> None:
    app = build_app(install_clickhouse=False)

    async def hook(_user: User) -> None:
        raise RuntimeError("boom")

    app.state.post_login_hooks.append(hook)

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    response = client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "alice",
            "password": "secret",
            "next": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 500
