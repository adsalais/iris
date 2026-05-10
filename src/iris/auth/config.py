from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from iris.envtools import (
    get_bool as _get_bool,
    get_int as _get_int,
    required as _get_required,
    split_csv as _split_csv,
    split_ws as _split_ws,
)


@dataclass(frozen=True)
class OIDCSettings:
    issuer_url: str
    client_id: str
    client_secret: str
    scopes: tuple[str, ...]
    ca_cert_path: str | None = None


@dataclass(frozen=True)
class LDAPSettings:
    url: str
    bind_dn_template: str
    group_base_dn: str
    require_tls: bool
    ca_cert_path: str | None


@dataclass(frozen=True)
class MockSettings:
    username: str
    password: str
    groups: tuple[str, ...]
    display_name: str


@dataclass(frozen=True)
class AuthSettings:
    method: Literal["oauth", "ldap", "mock"]
    cookie_name: str
    ttl_seconds: int
    absolute_ttl_seconds: int
    max_per_user: int
    cookie_secure: bool
    auth_db_path: str
    trust_forwarded_for: bool
    oidc: OIDCSettings | None
    ldap: LDAPSettings | None
    mock: MockSettings | None

    @classmethod
    def from_env(cls) -> AuthSettings:
        method = os.environ.get("AUTH_METHOD", "").strip()
        if method not in ("oauth", "ldap", "mock"):
            raise ValueError(
                f"AUTH_METHOD must be one of 'oauth' | 'ldap' | 'mock', got {method!r}"
            )

        cookie_name = os.environ.get("SESSION_COOKIE_NAME", "iris_session")
        ttl_seconds = _get_int("SESSION_TTL_SECONDS", default=43200)
        absolute_ttl_seconds = _get_int("SESSION_ABSOLUTE_TTL_SECONDS", default=2_592_000)  # 30 days
        max_per_user = _get_int("SESSION_MAX_PER_USER", default=10)
        cookie_secure = _get_bool("COOKIE_SECURE", default=True)
        trust_forwarded_for = _get_bool("IRIS_TRUST_FORWARDED_FOR", default=False)
        auth_db_path = (
            os.environ.get("AUTH_DB_PATH", "").strip() or "./iris-auth.db"
        )

        oidc = ldap = mock = None
        if method == "oauth":
            oidc = OIDCSettings(
                issuer_url=_get_required("OIDC_ISSUER_URL"),
                client_id=_get_required("OIDC_CLIENT_ID"),
                client_secret=_get_required("OIDC_CLIENT_SECRET"),
                scopes=_split_ws(os.environ.get("OIDC_SCOPES", "openid profile email groups")),
                ca_cert_path=os.environ.get("OIDC_CA_CERT_PATH") or None,
            )
        elif method == "ldap":
            url = _get_required("LDAP_URL")
            require_tls = _get_bool("LDAP_REQUIRE_TLS", default=True)
            if require_tls and not url.startswith("ldaps://"):
                raise ValueError(
                    "LDAP_URL must use ldaps:// when LDAP_REQUIRE_TLS=true; "
                    + f"got {url!r}. Set LDAP_REQUIRE_TLS=false to allow plaintext "
                    + "(development only)."
                )
            ldap = LDAPSettings(
                url=url,
                bind_dn_template=_get_required("LDAP_BIND_DN_TEMPLATE"),
                group_base_dn=_get_required("LDAP_GROUP_BASE_DN"),
                require_tls=require_tls,
                ca_cert_path=os.environ.get("LDAP_CA_CERT_PATH") or None,
            )
        elif method == "mock":
            username = _get_required("MOCK_USERNAME")
            mock = MockSettings(
                username=username,
                password=_get_required("MOCK_PASSWORD"),
                groups=_split_csv(os.environ.get("MOCK_GROUPS", "")),
                display_name=os.environ.get("MOCK_DISPLAY_NAME", username),
            )

        return cls(
            method=method,
            cookie_name=cookie_name,
            ttl_seconds=ttl_seconds,
            absolute_ttl_seconds=absolute_ttl_seconds,
            max_per_user=max_per_user,
            cookie_secure=cookie_secure,
            auth_db_path=auth_db_path,
            trust_forwarded_for=trust_forwarded_for,
            oidc=oidc,
            ldap=ldap,
            mock=mock,
        )
