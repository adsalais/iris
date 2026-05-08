from __future__ import annotations

from iris.auth.config import AuthSettings
from iris.auth.providers.base import Provider
from iris.auth.providers.mock import MockProvider


def build_provider(settings: AuthSettings) -> Provider:
    if settings.method == "mock":
        if settings.mock is None:
            raise RuntimeError(
                "AUTH_METHOD=mock requires settings.mock to be configured"
            )
        return MockProvider(settings.mock)
    if settings.method == "ldap":
        from iris.auth.providers.ldap import LDAPProvider

        if settings.ldap is None:
            raise RuntimeError(
                "AUTH_METHOD=ldap requires settings.ldap to be configured"
            )
        return LDAPProvider(settings.ldap)
    if settings.method == "oauth":
        from iris.auth.providers.oauth import OAuthProvider

        if settings.oidc is None:
            raise RuntimeError(
                "AUTH_METHOD=oauth requires settings.oidc to be configured"
            )
        return OAuthProvider(settings.oidc)
    raise ValueError(f"Unknown AUTH_METHOD: {settings.method}")
