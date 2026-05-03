import json

import httpx
import pytest

from iris.auth.config import OIDCSettings
from iris.auth.providers.oauth import OAuthProvider


ISSUER = "https://kc.example/realms/iris"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
AUTHZ = f"{ISSUER}/protocol/openid-connect/auth"
TOKEN = f"{ISSUER}/protocol/openid-connect/token"
USERINFO = f"{ISSUER}/protocol/openid-connect/userinfo"


def _mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": AUTHZ,
                    "token_endpoint": TOKEN,
                    "userinfo_endpoint": USERINFO,
                    "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
                },
            )
        if str(request.url) == TOKEN:
            return httpx.Response(
                200,
                json={
                    "access_token": "fake-access",
                    "id_token": "fake-id",
                    "token_type": "Bearer",
                    "expires_in": 300,
                },
            )
        if str(request.url) == USERINFO:
            return httpx.Response(
                200,
                json={
                    "sub": "abc-123",
                    "name": "Alice",
                    "preferred_username": "alice",
                    "groups": ["admins", "users"],
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def settings():
    return OIDCSettings(
        issuer_url=ISSUER,
        client_id="iris",
        client_secret="shh",
        scopes=("openid", "profile", "email", "groups"),
    )


@pytest.fixture
def provider(settings):
    return OAuthProvider(settings, _http_transport=_mock_transport())


def test_provider_construction_fetches_discovery(provider):
    assert provider.authorize_endpoint == AUTHZ
    assert provider.token_endpoint == TOKEN
    assert provider.userinfo_endpoint == USERINFO


def test_build_authorize_url_includes_state_and_pkce(provider):
    url, state, verifier = provider.build_authorize_url(redirect_uri="http://localhost/login/callback")
    assert url.startswith(AUTHZ)
    assert "client_id=iris" in url
    assert f"state={state}" in url
    assert "code_challenge=" in url
    assert verifier  # non-empty


def test_complete_callback_returns_user(provider):
    import asyncio

    user = asyncio.run(
        provider.exchange_code(
            code="dummy",
            code_verifier="dummy-verifier",
            redirect_uri="http://localhost/login/callback",
        )
    )
    assert user.subject == "abc-123"
    assert user.display_name == "Alice"
    assert set(user.groups) == {"admins", "users"}


def test_oauth_state_cookie_follows_cookie_secure(provider):
    """The oauth_state cookie's Secure flag should follow app.state.auth_cookie_secure."""
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    for cookie_secure in (True, False):
        app = FastAPI()
        app.state.auth_cookie_secure = cookie_secure

        @app.get("/login", name="login_callback")  # name needed for url_for inside begin()
        async def login(request: Request):
            return await provider.begin(request)

        r = TestClient(app).get("/login", follow_redirects=False)
        assert r.status_code == 302
        # The set-cookie header for oauth_state should reflect cookie_secure
        set_cookies = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else [r.headers.get("set-cookie", "")]
        oauth_state_cookie = next(
            (c for c in set_cookies if c.lower().startswith("oauth_state=")),
            "",
        )
        assert oauth_state_cookie, "oauth_state cookie should be set"
        has_secure = "secure" in oauth_state_cookie.lower()
        assert has_secure == cookie_secure, (
            f"cookie_secure={cookie_secure} but Set-Cookie was {oauth_state_cookie!r}"
        )


def test_oauth_state_cookie_defaults_secure_when_app_state_missing(provider):
    """If app.state.auth_cookie_secure is unset, default to True (paranoid)."""
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    app = FastAPI()
    # Do NOT set app.state.auth_cookie_secure

    @app.get("/login", name="login_callback")
    async def login(request: Request):
        return await provider.begin(request)

    r = TestClient(app).get("/login", follow_redirects=False)
    set_cookie = r.headers.get("set-cookie", "").lower()
    assert "oauth_state=" in set_cookie
    assert "secure" in set_cookie  # paranoid default
