import json

import httpx
import pytest

from iris.auth.config import OIDCSettings
from iris.auth.exceptions import AuthError
from iris.auth.providers.oauth import OAuthProvider


ISSUER = "https://kc.example/realms/iris"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
AUTHZ = f"{ISSUER}/protocol/openid-connect/auth"
TOKEN = f"{ISSUER}/protocol/openid-connect/token"
USERINFO = f"{ISSUER}/protocol/openid-connect/userinfo"


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
    return OAuthProvider(settings, _http_transport=_signing_mock_transport())


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


# JWT/JWKS test fixtures
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt as pyjwt
import json as _json

JWKS_URI = f"{ISSUER}/protocol/openid-connect/certs"


def _generate_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_private = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_numbers = private_key.public_key().public_numbers()
    # Build JWKS jwk entry (n, e in unsigned base64url)
    import base64
    def b64url_uint(n: int) -> str:
        b = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": "test-key-1",
        "n": b64url_uint(public_numbers.n),
        "e": b64url_uint(public_numbers.e),
    }
    return pem_private, jwk


_PRIVATE_PEM, _PUBLIC_JWK = _generate_keypair()


def _make_id_token(*, sub="abc-123", iss=ISSUER, aud="iris", exp_offset_seconds=300, kid="test-key-1"):
    import time
    now = int(time.time())
    payload = {
        "sub": sub,
        "iss": iss,
        "aud": aud,
        "iat": now,
        "exp": now + exp_offset_seconds,
        "name": "Alice",
        "preferred_username": "alice",
        "groups": ["admins", "users"],
    }
    return pyjwt.encode(payload, _PRIVATE_PEM, algorithm="RS256", headers={"kid": kid})


def _signing_mock_transport(id_token: str | None = None):
    """Like _mock_transport but signs id_token via the JWKS keypair."""
    if id_token is None:
        id_token = _make_id_token()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": AUTHZ,
                    "token_endpoint": TOKEN,
                    "userinfo_endpoint": USERINFO,
                    "jwks_uri": JWKS_URI,
                    "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
                },
            )
        if str(request.url) == JWKS_URI:
            return httpx.Response(200, json={"keys": [_PUBLIC_JWK]})
        if str(request.url) == TOKEN:
            return httpx.Response(
                200,
                json={
                    "access_token": "fake-access",
                    "id_token": id_token,
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
def signing_provider(settings):
    return OAuthProvider(settings, _http_transport=_signing_mock_transport())


def test_exchange_code_accepts_valid_id_token(signing_provider):
    """A properly-signed id_token from the IdP's JWKS is accepted."""
    import asyncio
    user = asyncio.run(
        signing_provider.exchange_code(
            code="dummy",
            code_verifier="v",
            redirect_uri="http://localhost/login/callback",
        )
    )
    assert user.subject == "abc-123"


def test_exchange_code_rejects_missing_id_token(settings):
    """If the token-endpoint response has no id_token, exchange fails."""
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": AUTHZ, "token_endpoint": TOKEN,
                "userinfo_endpoint": USERINFO, "jwks_uri": JWKS_URI,
            })
        if str(request.url) == JWKS_URI:
            return httpx.Response(200, json={"keys": [_PUBLIC_JWK]})
        if str(request.url) == TOKEN:
            return httpx.Response(200, json={"access_token": "fake", "token_type": "Bearer"})
        return httpx.Response(404)

    provider = OAuthProvider(settings, _http_transport=httpx.MockTransport(handler))
    with pytest.raises(AuthError) as exc:
        asyncio.run(
            provider.exchange_code(code="x", code_verifier="v", redirect_uri="http://x/cb")
        )
    assert exc.value.token == "oauth_exchange"


def test_exchange_code_rejects_id_token_with_wrong_audience(settings):
    """An id_token whose aud doesn't match client_id is rejected."""
    import asyncio
    bad_token = _make_id_token(aud="some-other-client")
    provider = OAuthProvider(settings, _http_transport=_signing_mock_transport(id_token=bad_token))
    with pytest.raises(AuthError) as exc:
        asyncio.run(
            provider.exchange_code(code="x", code_verifier="v", redirect_uri="http://x/cb")
        )
    assert exc.value.token == "oauth_exchange"


def test_exchange_code_rejects_expired_id_token(settings):
    """An id_token whose exp is past is rejected."""
    import asyncio
    expired = _make_id_token(exp_offset_seconds=-60)  # expired 60s ago
    provider = OAuthProvider(settings, _http_transport=_signing_mock_transport(id_token=expired))
    with pytest.raises(AuthError) as exc:
        asyncio.run(
            provider.exchange_code(code="x", code_verifier="v", redirect_uri="http://x/cb")
        )
    assert exc.value.token == "oauth_exchange"


def test_exchange_code_rejects_id_token_signed_with_wrong_key(settings):
    """An id_token signed with a different RSA key (not in the JWKS) is rejected."""
    import asyncio
    other_pem, _other_jwk = _generate_keypair()
    forged = pyjwt.encode(
        {"sub": "abc-123", "iss": ISSUER, "aud": "iris",
         "iat": 0, "exp": 9999999999},
        other_pem,
        algorithm="RS256",
        headers={"kid": "test-key-1"},  # claim same kid, but signed with a different key
    )
    provider = OAuthProvider(settings, _http_transport=_signing_mock_transport(id_token=forged))
    with pytest.raises(AuthError) as exc:
        asyncio.run(
            provider.exchange_code(code="x", code_verifier="v", redirect_uri="http://x/cb")
        )
    assert exc.value.token == "oauth_exchange"
