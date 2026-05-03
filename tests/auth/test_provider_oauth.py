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
