"""OAuth integration tests against a real Keycloak container."""

from __future__ import annotations

import asyncio

import httpx
from fastapi.testclient import TestClient

from iris.auth.config import OIDCSettings
from iris.auth.providers.oauth import OAuthProvider

from tests.auth.integration._keycloak_helpers import simulate_login


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
