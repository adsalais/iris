"""OAuth integration tests against a real Keycloak container."""

from __future__ import annotations

import asyncio

import httpx

from iris.auth.config import OIDCSettings
from iris.auth.providers.oauth import OAuthProvider


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
