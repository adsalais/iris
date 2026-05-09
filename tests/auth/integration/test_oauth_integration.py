"""OAuth integration tests against a real Keycloak container."""

from __future__ import annotations

import asyncio

import httpx
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from iris.auth.config import OIDCSettings
from iris.auth.providers.oauth import OAuthProvider

from tests.auth.integration._keycloak_helpers import (
    obtain_authorization_code,
    simulate_login,
)


def _oauth_provider(
    keycloak_container, tls_paths, *, client_secret: str = "iris-test-secret"
) -> OAuthProvider:
    """Build a fresh OAuthProvider configured against the integration Keycloak."""
    return OAuthProvider(
        OIDCSettings(
            issuer_url=keycloak_container.issuer_url,
            client_id="iris",
            client_secret=client_secret,
            scopes=("openid", "profile", "email"),
            ca_cert_path=str(tls_paths.ca_pem),
        )
    )


def _oauth_state_payload(test_client: TestClient) -> dict[str, str]:
    """Decode the oauth_state cookie iris set during /login.

    Returns the full signed payload so callers can extract verifier AND
    nonce. OAuthProvider signs the state cookie with a SHA-256 derivation
    of the client_secret (prefixed with the v1 derivation tag) plus a
    fixed salt; re-construct the same signer here. Mirrors
    OAuthProvider.__init__.
    """
    import hashlib

    signed = test_client.cookies.get("oauth_state")
    assert signed is not None, "iris should have set oauth_state on GET /login"
    derived_key = hashlib.sha256(
        b"iris-oauth-state-signing-v1:" + b"iris-test-secret"
    ).digest()
    signer = URLSafeTimedSerializer(derived_key, salt="iris-oauth-state")
    payload = signer.loads(signed)
    return payload


def _verifier_from_oauth_state(test_client: TestClient) -> str:
    """Backwards-compat shim around _oauth_state_payload."""
    return _oauth_state_payload(test_client)["verifier"]


def test_collection_smoke():
    """Placeholder: pytest can collect tests under tests/auth/integration."""
    assert True


def test_keycloak_container_serves_oidc_discovery(keycloak_container, tls_paths):
    """The fixture starts Keycloak with HTTPS + the iris-test realm imported,
    and discovery returns a valid OIDC document with the expected endpoints."""
    import ssl

    issuer = keycloak_container.issuer_url
    discovery_url = f"{issuer}/.well-known/openid-configuration"
    ctx = ssl.create_default_context(cafile=str(tls_paths.ca_pem))

    with httpx.Client(verify=ctx, timeout=10.0) as http:
        r = http.get(discovery_url)
    r.raise_for_status()
    doc = r.json()

    assert doc["issuer"] == issuer
    assert doc["authorization_endpoint"].startswith(issuer)
    assert doc["token_endpoint"].startswith(issuer)
    assert "openid" in doc.get("scopes_supported", [])


def test_oauth_provider_discovers_against_real_keycloak(
    keycloak_container, tls_paths
):
    """OAuthProvider with OIDC_CA_CERT_PATH set can discover endpoints
    against a self-signed Keycloak."""
    settings = OIDCSettings(
        issuer_url=keycloak_container.issuer_url,
        client_id="iris",
        client_secret="iris-test-secret",
        scopes=("openid", "profile", "email", "groups"),
        ca_cert_path=str(tls_paths.ca_pem),
    )
    provider = OAuthProvider(settings)

    async def _discover_and_close() -> dict[str, str]:
        try:
            return await provider._ensure_discovered()
        finally:
            await provider.close()

    doc = asyncio.run(_discover_and_close())
    assert doc["authorization_endpoint"].startswith(settings.issuer_url)
    assert doc["token_endpoint"].startswith(settings.issuer_url)
    assert doc["userinfo_endpoint"].startswith(settings.issuer_url)


def test_simulate_login_drives_authorize_to_callback(oauth_app, keycloak_http):
    """The helper drives the full OAuth code flow against real Keycloak and
    returns the iris-side response holding the iris_session cookie. Groups
    from the realm's group-membership mapper land in the session user.

    TestClient runs inside a ``with`` block so the lifespan shutdown
    closes the OAuthProvider's async httpx client; otherwise its pooled
    TLS connections survive into the next test's portal loop and trip
    "Event loop is closed" during gc.
    """
    with TestClient(oauth_app) as test_client:
        response = simulate_login(
            test_client=test_client, http=keycloak_http,
            username="alice", password="secret",
        )
        assert response.status_code == 302
        assert response.cookies.get("iris_session") is not None

        # The iris_session cookie should let /api/whoami succeed; groups come
        # from the oidc-group-membership-mapper attached to the iris client in
        # the realm seed (no `groups` scope is required because the mapper is
        # client-level, not scope-gated).
        me = test_client.get("/api/whoami")
        assert me.status_code == 200
        body = me.json()
        assert set(body["groups"]) == {"admins", "users"}


async def _exchange_and_close(
    provider: OAuthProvider,
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
    nonce: str,
):
    """Run exchange_code and provider.close() inside a single event loop.

    Calling asyncio.run() twice on the same provider (once for exchange,
    once for close) leaves dangling SSL transport callbacks scheduled on
    the just-closed loop — pytest reports those as 'Event loop is closed'.
    Doing both awaits in the same loop avoids that.
    """
    try:
        return await provider.exchange_code(
            code=code,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            expected_nonce=nonce,
        )
    finally:
        await provider.close()


def test_provider_exchange_code_returns_alice_with_groups(
    keycloak_container, tls_paths, oauth_app, keycloak_http
):
    """exchange_code directly against Keycloak returns User(alice) carrying
    both admins and users groups."""
    test_client = TestClient(oauth_app)
    code, _state = obtain_authorization_code(
        test_client=test_client, http=keycloak_http,
        username="alice", password="secret",
    )
    payload = _oauth_state_payload(test_client)

    user = asyncio.run(
        _exchange_and_close(
            _oauth_provider(keycloak_container, tls_paths),
            code=code,
            verifier=payload["verifier"],
            redirect_uri="http://testserver/login/callback",
            nonce=payload["nonce"],
        )
    )

    assert user.username == "alice"
    assert user.display_name == "Alice Example"
    assert set(user.groups) == {"admins", "users"}


def test_provider_exchange_code_returns_bob_with_users_group(
    keycloak_container, tls_paths, oauth_app, keycloak_http
):
    """exchange_code returns User(bob) with only the `users` group."""
    test_client = TestClient(oauth_app)
    code, _state = obtain_authorization_code(
        test_client=test_client, http=keycloak_http,
        username="bob", password="hunter2",
    )
    payload = _oauth_state_payload(test_client)

    user = asyncio.run(
        _exchange_and_close(
            _oauth_provider(keycloak_container, tls_paths),
            code=code,
            verifier=payload["verifier"],
            redirect_uri="http://testserver/login/callback",
            nonce=payload["nonce"],
        )
    )

    assert user.username == "bob"
    # bob's seed groups now include 'creators' (used by the clickhouse
    # integration suite). 'users' must still be present; the earlier
    # exact-equality assertion was unnecessarily strict.
    assert "users" in set(user.groups)
    assert "admins" not in set(user.groups)


def test_provider_wrong_client_secret_raises_oauth_exchange(
    keycloak_container, tls_paths, oauth_app, keycloak_http
):
    """An OAuthProvider configured with a wrong client_secret must fail
    the token exchange with AuthError('oauth_exchange')."""
    import pytest

    from iris.auth.exceptions import AuthError

    test_client = TestClient(oauth_app)
    code, _state = obtain_authorization_code(
        test_client=test_client, http=keycloak_http,
        username="alice", password="secret",
    )
    payload = _oauth_state_payload(test_client)

    with pytest.raises(AuthError) as exc:
        asyncio.run(
            _exchange_and_close(
                _oauth_provider(
                    keycloak_container, tls_paths, client_secret="WRONG-SECRET"
                ),
                code=code,
                verifier=payload["verifier"],
                redirect_uri="http://testserver/login/callback",
                nonce=payload["nonce"],
            )
        )
    assert exc.value.token == "oauth_exchange"


def test_provider_redirect_uri_mismatch_raises_oauth_exchange(
    keycloak_container, tls_paths, oauth_app, keycloak_http
):
    """Code obtained against http://testserver/login/callback exchanged with
    a different redirect_uri must be rejected by Keycloak."""
    import pytest

    from iris.auth.exceptions import AuthError

    test_client = TestClient(oauth_app)
    code, _state = obtain_authorization_code(
        test_client=test_client, http=keycloak_http,
        username="alice", password="secret",
    )
    payload = _oauth_state_payload(test_client)

    with pytest.raises(AuthError) as exc:
        asyncio.run(
            _exchange_and_close(
                _oauth_provider(keycloak_container, tls_paths),
                code=code,
                verifier=payload["verifier"],
                redirect_uri="http://testserver/some-other-path",
                nonce=payload["nonce"],
            )
        )
    assert exc.value.token == "oauth_exchange"


def test_provider_code_reuse_raises_oauth_exchange_on_second_call(
    keycloak_container, tls_paths, oauth_app, keycloak_http
):
    """Keycloak invalidates an authorization code on first use. Reusing it
    for a second exchange must fail with AuthError('oauth_exchange')."""
    import pytest

    from iris.auth.exceptions import AuthError

    test_client = TestClient(oauth_app)
    code, _state = obtain_authorization_code(
        test_client=test_client, http=keycloak_http,
        username="alice", password="secret",
    )
    payload = _oauth_state_payload(test_client)
    redirect_uri = "http://testserver/login/callback"

    async def _exchange_twice():
        provider = _oauth_provider(keycloak_container, tls_paths)
        try:
            user = await provider.exchange_code(
                code=code,
                code_verifier=payload["verifier"],
                redirect_uri=redirect_uri,
                expected_nonce=payload["nonce"],
            )
            assert user.username == "alice"
            # Second call against the same code: Keycloak invalidates on use.
            await provider.exchange_code(
                code=code,
                code_verifier=payload["verifier"],
                redirect_uri=redirect_uri,
                expected_nonce=payload["nonce"],
            )
        finally:
            await provider.close()

    with pytest.raises(AuthError) as exc:
        asyncio.run(_exchange_twice())
    assert exc.value.token == "oauth_exchange"


def test_route_oauth_alice_full_flow_creates_session(
    oauth_app, keycloak_http
):
    """End-to-end: authorize -> Keycloak login -> callback -> iris session
    cookie set; whoami succeeds and surfaces the user's groups.

    With install_clickhouse=False these tests don't run the post-login
    capabilities derivation — alice's session lands with EMPTY_CAPABILITIES. The
    Keycloak integration still validates the OAuth + session-creation
    pipeline; capabilities derivation has its own dedicated tests under
    tests/clickhouse/.
    """
    with TestClient(oauth_app) as test_client:
        response = simulate_login(
            test_client=test_client, http=keycloak_http,
            username="alice", password="secret",
        )
        assert response.status_code == 302
        sid = response.cookies.get("iris_session")
        assert sid is not None

        me = test_client.get("/api/whoami")
        assert me.status_code == 200
        body = me.json()
        assert body["display_name"] == "Alice Example"
        assert set(body["groups"]) == {"admins", "users"}
        # Capabilities default to empty when CH isn't installed.
        assert body["capabilities"] == {
            "is_admin": False,
            "can_create_database": False,
            "db_admin": [],
            "db_writer": [],
            "db_reader": [],
        }


def test_route_oauth_bob_full_flow_creates_session_with_empty_capabilities(
    oauth_app, keycloak_http
):
    """Bob authenticates and gets a session with empty capabilities (CH not
    installed for these tests). Authentication succeeds independently of
    whether the user has any capabilities."""
    with TestClient(oauth_app) as test_client:
        response = simulate_login(
            test_client=test_client, http=keycloak_http,
            username="bob", password="hunter2",
        )
        assert response.status_code == 302
        sid = response.cookies.get("iris_session")
        assert sid is not None

        me = test_client.get("/api/whoami")
        assert me.status_code == 200
        groups = set(me.json()["groups"])
        assert "users" in groups
        assert "admins" not in groups

        store = oauth_app.state.auth_session_store
        user_session = asyncio.run(store.get_and_refresh(sid))
        assert user_session is not None
        assert user_session.capabilities.is_admin is False


def test_provider_wrong_ca_bundle_raises_oauth_discovery(
    keycloak_container, tmp_path
):
    """OAuthProvider configured with a CA bundle that doesn't include
    Keycloak's CA must fail discovery with AuthError('oauth_discovery')."""
    import pytest

    from iris.auth.exceptions import AuthError
    from tests._tls import generate_ca_and_leaf

    bad_paths = generate_ca_and_leaf(tmp_path / "wrong-ca")
    settings = OIDCSettings(
        issuer_url=keycloak_container.issuer_url,
        client_id="iris",
        client_secret="iris-test-secret",
        scopes=("openid",),
        ca_cert_path=str(bad_paths.ca_pem),
    )
    provider = OAuthProvider(settings)

    async def _trigger_discovery_and_close():
        try:
            await provider._ensure_discovered()
        finally:
            await provider.close()

    with pytest.raises(AuthError) as exc:
        asyncio.run(_trigger_discovery_and_close())
    assert exc.value.token == "oauth_discovery"
