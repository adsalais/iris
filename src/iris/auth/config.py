from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


def _get_required(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise ValueError(f"Missing required env var: {key}")
    return val


def _get_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"{key} must be a boolean (true/false), got {raw!r}")


def _get_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from e


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _split_ws(raw: str) -> tuple[str, ...]:
    return tuple(p for p in raw.split() if p)


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
        ttl_seconds = _get_int("SESSION_TTL_SECONDS", 43200)
        absolute_ttl_seconds = _get_int("SESSION_ABSOLUTE_TTL_SECONDS", 2_592_000)  # 30 days
        max_per_user = _get_int("SESSION_MAX_PER_USER", 10)
        cookie_secure = _get_bool("COOKIE_SECURE", True)
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
            require_tls = _get_bool("LDAP_REQUIRE_TLS", True)
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
            oidc=oidc,
            ldap=ldap,
            mock=mock,
        )
