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


def _verifier_from_oauth_state(test_client: TestClient) -> str:
    """Decode the verifier out of the oauth_state cookie iris set during /login.

    OAuthProvider signs the state cookie with the client_secret + a fixed salt;
    re-construct the same signer with the realm's client_secret to extract the
    verifier so we can hand it back to exchange_code.
    """
    signed = test_client.cookies.get("oauth_state")
    assert signed is not None, "iris should have set oauth_state on GET /login"
    signer = URLSafeTimedSerializer("iris-test-secret", salt="iris-oauth-state")
    payload = signer.loads(signed)
    return payload["verifier"]


def test_collection_smoke():
    """Placeholder: pytest can collect tests under tests/auth/integration."""
    assert True


def test_keycloak_container_serves_oidc_discovery(keycloak_container, tls_paths):
    """The fixture starts Keycloak with HTTPS + the iris-test realm imported,
    and discovery returns a valid OIDC document with the expected endpoints."""
    issuer = keycloak_container.issuer_url
    discovery_url = f"{issuer}/.well-known/openid-configuration"

    with httpx.Client(verify=str(tls_paths.ca_pem), timeout=10.0) as http:
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
    try:
        # Property access triggers _ensure_discovered().
        assert provider.authorize_endpoint.startswith(settings.issuer_url)
        assert provider.token_endpoint.startswith(settings.issuer_url)
        assert provider.userinfo_endpoint.startswith(settings.issuer_url)
    finally:
        asyncio.run(provider.close())


def test_simulate_login_drives_authorize_to_callback(oauth_app, keycloak_http):
    """The helper drives the full OAuth code flow against real Keycloak and
    returns the iris-side response holding the iris_session cookie. Groups
    from the realm's group-membership mapper land in the session user."""
    test_client = TestClient(oauth_app)
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
    provider: OAuthProvider, *, code: str, verifier: str, redirect_uri: str
):
    """Run exchange_code and provider.close() inside a single event loop.

    Calling asyncio.run() twice on the same provider (once for exchange,
    once for close) leaves dangling SSL transport callbacks scheduled on
    the just-closed loop — pytest reports those as 'Event loop is closed'.
    Doing both awaits in the same loop avoids that.
    """
    try:
        return await provider.exchange_code(
            code=code, code_verifier=verifier, redirect_uri=redirect_uri,
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
    verifier = _verifier_from_oauth_state(test_client)

    user = asyncio.run(
        _exchange_and_close(
            _oauth_provider(keycloak_container, tls_paths),
            code=code, verifier=verifier,
            redirect_uri="http://testserver/login/callback",
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
    verifier = _verifier_from_oauth_state(test_client)

    user = asyncio.run(
        _exchange_and_close(
            _oauth_provider(keycloak_container, tls_paths),
            code=code, verifier=verifier,
            redirect_uri="http://testserver/login/callback",
        )
    )

    assert user.username == "bob"
    assert set(user.groups) == {"users"}


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
    verifier = _verifier_from_oauth_state(test_client)

    with pytest.raises(AuthError) as exc:
        asyncio.run(
            _exchange_and_close(
                _oauth_provider(
                    keycloak_container, tls_paths, client_secret="WRONG-SECRET"
                ),
                code=code, verifier=verifier,
                redirect_uri="http://testserver/login/callback",
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
    verifier = _verifier_from_oauth_state(test_client)

    with pytest.raises(AuthError) as exc:
        asyncio.run(
            _exchange_and_close(
                _oauth_provider(keycloak_container, tls_paths),
                code=code, verifier=verifier,
                redirect_uri="http://testserver/some-other-path",
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
    verifier = _verifier_from_oauth_state(test_client)
    redirect_uri = "http://testserver/login/callback"

    async def _exchange_twice():
        provider = _oauth_provider(keycloak_container, tls_paths)
        try:
            user = await provider.exchange_code(
                code=code, code_verifier=verifier, redirect_uri=redirect_uri,
            )
            assert user.username == "alice"
            # Second call against the same code: Keycloak invalidates on use.
            await provider.exchange_code(
                code=code, code_verifier=verifier, redirect_uri=redirect_uri,
            )
        finally:
            await provider.close()

    with pytest.raises(AuthError) as exc:
        asyncio.run(_exchange_twice())
    assert exc.value.token == "oauth_exchange"


def test_route_oauth_alice_full_flow_creates_session_with_admin_role(
    oauth_app, keycloak_http
):
    """End-to-end: authorize -> Keycloak login -> callback -> iris session
    cookie set; whoami succeeds and the realm's admins group resolves
    transitively to {reader, writer, admin} per the test authz YAML."""
    from iris.auth.authz.core import resolve_roles

    test_client = TestClient(oauth_app)
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

    # Roles aren't surfaced by /api/whoami; resolve them via the store and
    # the role-mapping loader to confirm the YAML mapping was applied.
    store = oauth_app.state.auth_session_store
    user_session = asyncio.run(store.get_and_refresh(sid))
    assert user_session is not None
    mapping = oauth_app.state.authz_loader.get()
    roles = resolve_roles(user_session.user, mapping)
    assert {"reader", "writer", "admin"} <= roles


def test_route_oauth_bob_full_flow_creates_session_with_no_roles(
    oauth_app, keycloak_http
):
    """Bob is only in the `users` group; the test authz YAML maps no role
    to that group, so the session is valid but resolves to an empty role
    set. Whoami still succeeds — being authenticated doesn't require a role."""
    from iris.auth.authz.core import resolve_roles

    test_client = TestClient(oauth_app)
    response = simulate_login(
        test_client=test_client, http=keycloak_http,
        username="bob", password="hunter2",
    )
    assert response.status_code == 302
    sid = response.cookies.get("iris_session")
    assert sid is not None

    me = test_client.get("/api/whoami")
    assert me.status_code == 200
    assert set(me.json()["groups"]) == {"users"}

    store = oauth_app.state.auth_session_store
    user_session = asyncio.run(store.get_and_refresh(sid))
    assert user_session is not None
    mapping = oauth_app.state.authz_loader.get()
    assert resolve_roles(user_session.user, mapping) == frozenset()


def test_provider_wrong_ca_bundle_raises_oauth_discovery(
    keycloak_container, tmp_path
):
    """OAuthProvider configured with a CA bundle that doesn't include
    Keycloak's CA must fail discovery with AuthError('oauth_discovery')."""
    import pytest

    from iris.auth.exceptions import AuthError
    from tests.auth.integration._tls import generate_ca_and_leaf

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
            # Property access triggers _ensure_discovered().
            _ = provider.authorize_endpoint
        finally:
            await provider.close()

    with pytest.raises(AuthError) as exc:
        asyncio.run(_trigger_discovery_and_close())
    assert exc.value.token == "oauth_discovery"
