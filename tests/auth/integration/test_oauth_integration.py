"""OAuth integration tests against a real Keycloak container."""

from __future__ import annotations

import httpx


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
