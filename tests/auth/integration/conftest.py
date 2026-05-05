"""Fixtures for the auth integration tier (Keycloak via real container).

Spins up Keycloak via testcontainers-python, generates a self-signed CA + leaf
cert in pure Python, and yields per-test FastAPI apps configured to use the
real OAuth provider.

LDAP integration was descoped in favor of focusing on the OAuth path; the
existing offline LDAP tests in tests/auth/test_provider_ldap.py remain.

This conftest layers on top of tests/conftest.py: the parent conftest sets
AUTH_METHOD=mock at module scope; integration tests use monkeypatch.setenv to
override that for the duration of the test.

Run only this tier:        uv run pytest tests/auth/integration
Skip this tier (no Docker): uv run pytest --ignore=tests/auth/integration
"""

from __future__ import annotations

import pytest

from tests.auth.integration._tls import TLSPaths, generate_ca_and_leaf


@pytest.fixture(scope="session")
def tls_paths(tmp_path_factory) -> TLSPaths:
    """Generate a CA + leaf cert once per pytest session.

    The same paths are consumed by:
    - keycloak_container (mounted as HTTPS cert + key)
    - OAuthProvider via OIDC_CA_CERT_PATH
    """
    target = tmp_path_factory.mktemp("auth-certs")
    return generate_ca_and_leaf(target)
