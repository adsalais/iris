from typing import cast

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

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


def test_oidc_settings_ca_cert_path_defaults_to_none():
    """OIDCSettings.ca_cert_path is optional; existing callers that don't
    pass it should get None (production code paths and offline tests)."""
    s = OIDCSettings(
        issuer_url="https://example",
        client_id="x",
        client_secret="y",
        scopes=("openid",),
    )
    assert s.ca_cert_path is None


def test_provider_construction_does_not_fetch_discovery(settings):
    """Construction is lazy; the discovery URL is fetched only on first use."""
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(404)

    OAuthProvider(settings, _http_transport=httpx.MockTransport(handler))
    assert fetched == [], (
        f"construction should not have fetched anything, but fetched: {fetched}"
    )


def test_ensure_discovered_returns_endpoints(provider):
    """The async _ensure_discovered populates the discovery doc."""
    import asyncio

    doc = asyncio.run(provider._ensure_discovered())
    assert doc["authorization_endpoint"] == AUTHZ
    assert doc["token_endpoint"] == TOKEN
    assert doc["userinfo_endpoint"] == USERINFO


def test_discovery_failure_surfaces_oauth_discovery_token(settings):
    """If discovery fails, the failure surfaces as AuthError('oauth_discovery')."""
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    provider = OAuthProvider(settings, _http_transport=httpx.MockTransport(handler))
    with pytest.raises(AuthError) as exc:
        asyncio.run(provider._ensure_discovered())
    assert exc.value.token == "oauth_discovery"


def test_build_authorize_url_includes_state_and_pkce(provider):
    url, state, verifier, nonce = provider.build_authorize_url(
        redirect_uri="http://localhost/login/callback",
        authorize_endpoint=AUTHZ,
    )
    assert url.startswith(AUTHZ)
    assert "client_id=iris" in url
    assert f"state={state}" in url
    assert "code_challenge=" in url
    assert f"nonce={nonce}" in url
    assert verifier  # non-empty
    assert nonce  # non-empty


def test_concurrent_ensure_discovered_runs_once(settings):
    """Two coroutines awaiting _ensure_discovered concurrently must trigger
    exactly one discovery network round-trip; subsequent awaits read the cache."""
    import asyncio

    fetched: list[str] = []
    real_handler = _signing_mock_transport()  # an httpx.MockTransport

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        # Delegate to the signing-mock body so JWKS still resolves.
        # MockTransport.handler accepts a sync handler; cast through
        # object because pyright sees the sync/async union return type.
        result = real_handler.handler(request)  # type: ignore[attr-defined]
        return cast(httpx.Response, result)

    provider = OAuthProvider(
        settings, _http_transport=httpx.MockTransport(handler)
    )

    async def _two_callers():
        return await asyncio.gather(
            provider._ensure_discovered(),
            provider._ensure_discovered(),
        )

    asyncio.run(_two_callers())
    discovery_calls = [u for u in fetched if "openid-configuration" in u]
    assert len(discovery_calls) == 1, (
        f"discovery should fire once even with concurrent callers; saw {discovery_calls}"
    )


def test_complete_callback_returns_user(provider):
    import asyncio

    user = asyncio.run(
        provider.exchange_code(
            code="dummy",
            code_verifier="dummy-verifier",
            redirect_uri="http://localhost/login/callback",
            expected_nonce=_DEFAULT_NONCE,
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

        @app.get(
            "/login", name="login_callback"
        )  # name needed for url_for inside begin()
        async def login(request: Request):
            return await provider.begin(request)

        r = TestClient(app).get("/login", follow_redirects=False)
        assert r.status_code == 302
        # The set-cookie header for oauth_state should reflect cookie_secure
        set_cookies = (
            r.headers.get_list("set-cookie")
            if hasattr(r.headers, "get_list")
            else [r.headers.get("set-cookie", "")]
        )
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


_DEFAULT_NONCE = "test-nonce"


def _make_id_token(
    *,
    sub="abc-123",
    iss=ISSUER,
    aud="iris",
    exp_offset_seconds=300,
    kid="test-key-1",
    nonce=_DEFAULT_NONCE,
):
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
        "nonce": nonce,
    }
    return pyjwt.encode(payload, _PRIVATE_PEM, algorithm="RS256", headers={"kid": kid})


def _signing_mock_transport(
    id_token: str | None = None,
    *,
    userinfo_sub: str = "abc-123",
    userinfo_groups: object = None,  # default to ["admins", "users"]
):
    """Like _mock_transport but signs id_token via the JWKS keypair.

    ``userinfo_sub`` lets tests force a sub-mismatch between id_token and
    userinfo. ``userinfo_groups`` lets tests inject a non-list value to
    exercise the defensive validation path.
    """
    if id_token is None:
        id_token = _make_id_token()
    if userinfo_groups is None:
        userinfo_groups = ["admins", "users"]

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
                    "sub": userinfo_sub,
                    "name": "Alice",
                    "preferred_username": "alice",
                    "groups": userinfo_groups,
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
            expected_nonce=_DEFAULT_NONCE,
        )
    )
    assert user.subject == "abc-123"


def test_exchange_code_rejects_missing_id_token(settings):
    """If the token-endpoint response has no id_token, exchange fails."""
    import asyncio

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
                },
            )
        if str(request.url) == JWKS_URI:
            return httpx.Response(200, json={"keys": [_PUBLIC_JWK]})
        if str(request.url) == TOKEN:
            return httpx.Response(
                200, json={"access_token": "fake", "token_type": "Bearer"}
            )
        return httpx.Response(404)

    provider = OAuthProvider(settings, _http_transport=httpx.MockTransport(handler))
    with pytest.raises(AuthError) as exc:
        asyncio.run(
            provider.exchange_code(
                code="x",
                code_verifier="v",
                redirect_uri="http://x/cb",
                expected_nonce=_DEFAULT_NONCE,
            )
        )
    assert exc.value.token == "oauth_exchange"


def test_exchange_code_rejects_id_token_with_wrong_audience(settings):
    """An id_token whose aud doesn't match client_id is rejected."""
    import asyncio

    bad_token = _make_id_token(aud="some-other-client")
    provider = OAuthProvider(
        settings, _http_transport=_signing_mock_transport(id_token=bad_token)
    )
    with pytest.raises(AuthError) as exc:
        asyncio.run(
            provider.exchange_code(
                code="x",
                code_verifier="v",
                redirect_uri="http://x/cb",
                expected_nonce=_DEFAULT_NONCE,
            )
        )
    assert exc.value.token == "oauth_exchange"


def test_exchange_code_rejects_expired_id_token(settings):
    """An id_token whose exp is past is rejected."""
    import asyncio

    expired = _make_id_token(exp_offset_seconds=-60)  # expired 60s ago
    provider = OAuthProvider(
        settings, _http_transport=_signing_mock_transport(id_token=expired)
    )
    with pytest.raises(AuthError) as exc:
        asyncio.run(
            provider.exchange_code(
                code="x",
                code_verifier="v",
                redirect_uri="http://x/cb",
                expected_nonce=_DEFAULT_NONCE,
            )
        )
    assert exc.value.token == "oauth_exchange"


def test_oauth_provider_close_is_idempotent(provider):
    """close() can be called more than once without raising."""
    import asyncio

    asyncio.run(provider.close())
    asyncio.run(provider.close())  # second call should also not raise


def test_exchange_code_rejects_id_token_signed_with_wrong_key(settings):
    """An id_token signed with a different RSA key (not in the JWKS) is rejected."""
    import asyncio

    other_pem, _other_jwk = _generate_keypair()
    forged = pyjwt.encode(
        {
            "sub": "abc-123",
            "iss": ISSUER,
            "aud": "iris",
            "iat": 0,
            "exp": 9999999999,
            "nonce": _DEFAULT_NONCE,
        },
        other_pem,
        algorithm="RS256",
        headers={
            "kid": "test-key-1"
        },  # claim same kid, but signed with a different key
    )
    provider = OAuthProvider(
        settings, _http_transport=_signing_mock_transport(id_token=forged)
    )
    with pytest.raises(AuthError) as exc:
        asyncio.run(
            provider.exchange_code(
                code="x",
                code_verifier="v",
                redirect_uri="http://x/cb",
                expected_nonce=_DEFAULT_NONCE,
            )
        )
    assert exc.value.token == "oauth_exchange"


def test_user_from_id_and_userinfo_falls_back_to_sub_when_preferred_username_absent(
    provider,
):
    """When userinfo lacks preferred_username, User.username falls back to id_token sub."""
    user = provider._user_from_id_and_userinfo(
        id_claims={"sub": "abc-123"},
        ui_claims={"sub": "abc-123", "groups": ["users"]},
    )
    assert user.username == "abc-123"


def test_nonce_mismatch_is_rejected(settings):
    """If the id_token's nonce does not match the cookie's nonce, fail."""
    import asyncio

    # Mock issues an id_token with a fixed nonce; we pass a different one.
    provider = OAuthProvider(
        settings,
        _http_transport=_signing_mock_transport(
            id_token=_make_id_token(nonce="alpha")
        ),
    )
    with pytest.raises(AuthError) as exc:
        asyncio.run(
            provider.exchange_code(
                code="dummy",
                code_verifier="v",
                redirect_uri="http://localhost/cb",
                expected_nonce="beta",
            )
        )
    assert exc.value.token == "oauth_exchange"


def test_sub_mismatch_is_rejected(settings):
    """If userinfo.sub != id_token.sub, fail with oauth_sub_mismatch.

    The id_token still has sub='abc-123' (the default in _make_id_token);
    we override the userinfo sub to a different value.
    """
    import asyncio

    provider = OAuthProvider(
        settings,
        _http_transport=_signing_mock_transport(userinfo_sub="someone-else"),
    )
    with pytest.raises(AuthError) as exc:
        asyncio.run(
            provider.exchange_code(
                code="dummy",
                code_verifier="v",
                redirect_uri="http://localhost/cb",
                expected_nonce=_DEFAULT_NONCE,
            )
        )
    assert exc.value.token == "oauth_sub_mismatch"


def test_groups_not_a_list_is_treated_as_empty(settings, caplog):
    """If userinfo returns groups as a string instead of a list, ignore it
    and log a warning (do not iterate per-character)."""
    import asyncio
    import logging

    caplog.set_level(logging.WARNING, logger="iris.auth.oauth")
    provider = OAuthProvider(
        settings,
        _http_transport=_signing_mock_transport(userinfo_groups="admin"),
    )
    user = asyncio.run(
        provider.exchange_code(
            code="dummy",
            code_verifier="v",
            redirect_uri="http://localhost/cb",
            expected_nonce=_DEFAULT_NONCE,
        )
    )
    assert user.groups == ()
    assert any("not a list" in rec.message for rec in caplog.records)


def test_id_token_missing_nonce_is_rejected(settings):
    """jwt.decode requires the nonce claim; an id_token without one is rejected."""
    import asyncio
    import time as _time

    now = int(_time.time())
    no_nonce = pyjwt.encode(
        {
            "sub": "abc-123",
            "iss": ISSUER,
            "aud": "iris",
            "iat": now,
            "exp": now + 300,
        },
        _PRIVATE_PEM,
        algorithm="RS256",
        headers={"kid": "test-key-1"},
    )
    provider = OAuthProvider(
        settings, _http_transport=_signing_mock_transport(id_token=no_nonce)
    )
    with pytest.raises(AuthError) as exc:
        asyncio.run(
            provider.exchange_code(
                code="x",
                code_verifier="v",
                redirect_uri="http://x/cb",
                expected_nonce=_DEFAULT_NONCE,
            )
        )
    assert exc.value.token == "oauth_exchange"


def test_callback_error_clears_state_cookie(provider):
    """A failed callback (bad state cookie / mismatched state / missing code)
    must delete OAUTH_STATE_COOKIE so a stale signed cookie can't replay."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from iris.auth.exceptions import install_exception_handlers
    from iris.auth.providers.oauth import OAUTH_STATE_COOKIE
    from iris.auth.routes import build_auth_router
    from iris.auth.store import SessionStore
    from iris.templates import TEMPLATES

    app = FastAPI()
    app.state.shutdown_hooks = []
    app.state.auth_cookie_secure = False
    app.state.auth_cookie_name = "iris_session"
    app.state.post_login_hooks = []
    app.state.templates = TEMPLATES
    store = SessionStore(
        path=":memory:", ttl_seconds=3600, absolute_ttl_seconds=86400, max_per_user=10
    )
    app.state.auth_session_store = store
    router = build_auth_router(
        app=app, provider=provider, store=store,
        cookie_name="iris_session", cookie_secure=False, ttl_seconds=3600,
    )
    app.include_router(router)
    install_exception_handlers(app, cookie_name="iris_session")

    client = TestClient(app)
    # No state cookie set -> AuthError("oauth_state") -> redirect to /login?error=...
    r = client.get("/login/callback", follow_redirects=False)
    assert r.status_code == 302
    set_cookie = r.headers.get("set-cookie", "")
    assert OAUTH_STATE_COOKIE in set_cookie, (
        f"expected Set-Cookie clearing {OAUTH_STATE_COOKIE}; got: {set_cookie!r}"
    )
    # delete_cookie sets Max-Age=0 (or expires in the past); confirm it's a clear, not a set.
    lower = set_cookie.lower()
    assert ("max-age=0" in lower) or ("expires=" in lower)


def test_oauth_state_signing_key_is_not_the_client_secret(settings):
    """The state-signing key must not equal the OAuth client_secret. The signer
    is constructed from a SHA-256 derivation, so introspecting the signer's
    secret_keys shows the derived bytes, not the raw secret."""
    import hashlib

    provider = OAuthProvider(settings)
    expected_derived = hashlib.sha256(
        b"iris-oauth-state-signing-v1:" + settings.client_secret.encode()
    ).digest()

    keys = list(provider._signer.secret_keys)
    assert keys, "signer should have at least one secret key"
    assert expected_derived in keys
    assert settings.client_secret.encode() not in keys


def test_oauth_state_round_trips_with_derived_key(settings):
    """End-to-end: signing then loading a state payload still works after
    the derivation change."""
    from iris.auth.providers.oauth import STATE_COOKIE_TTL

    provider = OAuthProvider(settings)
    signed = provider._signer.dumps({"state": "x", "verifier": "y", "next": "/"})
    loaded = provider._signer.loads(signed, max_age=STATE_COOKIE_TTL)
    assert loaded == {"state": "x", "verifier": "y", "next": "/"}
