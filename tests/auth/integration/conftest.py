"""Auth-integration-specific fixtures.

Keycloak container + TLS paths now live in the top-level
``tests/conftest.py`` so they can be shared with
``tests/clickhouse/integration/``. This file owns only the
auth-specific ``oauth_app`` and ``keycloak_http`` fixtures.

Run only this tier:        uv run pytest tests/auth/integration
Skip this tier (no Docker): uv run pytest --ignore=tests/auth/integration
"""

from __future__ import annotations

import ssl
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI


def _ssl_context_trusting(ca_pem: Path) -> ssl.SSLContext:
    """SSLContext that verifies against the integration tier's self-signed CA."""
    return ssl.create_default_context(cafile=str(ca_pem))


@pytest.fixture
def oauth_app(monkeypatch, keycloak_container, tls_paths) -> FastAPI:
    """A fresh iris app configured to authenticate against the Keycloak container.

    Uses monkeypatch.setenv to override the AUTH_METHOD=mock that
    tests/conftest.py set at module scope. Each test gets a freshly-built
    app via build_app(); env is restored after the test.
    """
    monkeypatch.setenv("AUTH_METHOD", "oauth")
    monkeypatch.setenv("OIDC_ISSUER_URL", keycloak_container.issuer_url)
    monkeypatch.setenv("OIDC_CLIENT_ID", "iris")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "iris-test-secret")
    # The 'groups' claim is emitted by a client-level oidc-group-membership-mapper
    # (see tests/seed/keycloak-realm.json), so we don't need a 'groups' client
    # scope — which Keycloak doesn't ship by default.
    monkeypatch.setenv("OIDC_SCOPES", "openid profile email")
    monkeypatch.setenv("OIDC_CA_CERT_PATH", str(tls_paths.ca_pem))
    # Keycloak issues redirect_uri checks against http://testserver/login/callback,
    # which is TestClient's default. Cookie-Secure must be off because TestClient
    # uses http://, not https://.
    monkeypatch.setenv("COOKIE_SECURE", "false")

    from iris.app import build_app
    return build_app(install_clickhouse=False)


@pytest.fixture
def keycloak_http(tls_paths):
    """A real httpx.Client that trusts the Keycloak self-signed cert.

    Used by simulate_login as the user-agent that visits Keycloak's login
    page. Defaults to follow_redirects=True (Keycloak typically 302s the
    authorize URL once before rendering the login form); the helper overrides
    per-call where capturing a specific 302 matters (the form POST).

    Lifetime: per-test, so cookies/state don't leak between tests.
    """
    with httpx.Client(
        verify=_ssl_context_trusting(tls_paths.ca_pem),
        follow_redirects=True,
        timeout=10.0,
    ) as client:
        yield client
