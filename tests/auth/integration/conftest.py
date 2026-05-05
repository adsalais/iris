"""Fixtures for the auth integration tier (LDAP + OAuth via real containers).

Spins up bitnami/openldap and Keycloak via testcontainers-python, generates a
self-signed CA + leaf cert in pure Python, and yields per-test FastAPI apps
configured to use the real provider.

This conftest layers on top of tests/conftest.py: the parent conftest sets
AUTH_METHOD=mock at module scope; integration tests use monkeypatch.setenv to
override that for the duration of the test.

Run only this tier:        uv run pytest tests/auth/integration
Skip this tier (no Docker): uv run pytest --ignore=tests/auth/integration
"""

from __future__ import annotations
