from __future__ import annotations

from iris.auth.config import AuthSettings
from iris.auth.providers.base import Provider
from iris.auth.providers.mock import MockProvider


def build_provider(settings: AuthSettings) -> Provider:
    if settings.method == "mock":
        assert settings.mock is not None
        return MockProvider(settings.mock)
    if settings.method == "ldap":
        from iris.auth.providers.ldap import LDAPProvider  # not implemented yet (Task 10)

        assert settings.ldap is not None
        return LDAPProvider(settings.ldap)
    if settings.method == "oauth":
        from iris.auth.providers.oauth import OAuthProvider  # not implemented yet (Task 11)

        assert settings.oidc is not None
        return OAuthProvider(settings.oidc)
    raise ValueError(f"Unknown AUTH_METHOD: {settings.method}")
