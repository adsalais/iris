from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from iris.auth.config import MockSettings
from iris.auth.exceptions import AuthError
from iris.auth.providers.mock import MockProvider


def _provider() -> MockProvider:
    settings = MockSettings(
        username="alice",
        password="secret",
        groups=("admins", "users"),
        display_name="Alice",
    )
    return MockProvider(settings)


def test_begin_renders_login_form():
    provider = _provider()
    app = FastAPI()

    @app.get("/login")
    async def login(request: Request):
        return await provider.begin(request)

    r = TestClient(app).get("/login")
    assert r.status_code == 200
    assert "<form" in r.text
    assert 'name="username"' in r.text
    assert 'name="password"' in r.text


def test_complete_with_valid_creds_returns_user():
    import asyncio

    provider = _provider()
    user = asyncio.run(provider.authenticate("alice", "secret"))
    assert user.subject == "mock:alice"
    assert user.display_name == "Alice"
    assert user.groups == ("admins", "users")


def test_complete_with_wrong_password_raises_auth_error():
    import asyncio
    import pytest

    provider = _provider()
    with pytest.raises(AuthError) as exc:
        asyncio.run(provider.authenticate("alice", "nope"))
    assert exc.value.token == "invalid_credentials"


def test_complete_with_wrong_username_raises_auth_error():
    import asyncio
    import pytest

    provider = _provider()
    with pytest.raises(AuthError) as exc:
        asyncio.run(provider.authenticate("bob", "secret"))
    assert exc.value.token == "invalid_credentials"
