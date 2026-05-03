import os

import pytest

from iris.auth.config import AuthSettings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("AUTH_", "SESSION_", "COOKIE_", "OIDC_", "LDAP_", "MOCK_")):
            monkeypatch.delenv(key, raising=False)


def test_defaults_when_only_method_set(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "mock")
    monkeypatch.setenv("MOCK_USERNAME", "alice")
    monkeypatch.setenv("MOCK_PASSWORD", "secret")
    s = AuthSettings.from_env()
    assert s.method == "mock"
    assert s.cookie_name == "iris_session"
    assert s.ttl_seconds == 43200
    assert s.max_per_user == 10
    assert s.cookie_secure is True


def test_session_max_per_user_override(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "mock")
    monkeypatch.setenv("MOCK_USERNAME", "alice")
    monkeypatch.setenv("MOCK_PASSWORD", "secret")
    monkeypatch.setenv("SESSION_MAX_PER_USER", "3")
    s = AuthSettings.from_env()
    assert s.max_per_user == 3


def test_unknown_method_raises(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "saml")
    with pytest.raises(ValueError, match="AUTH_METHOD"):
        AuthSettings.from_env()


def test_missing_method_raises(monkeypatch):
    with pytest.raises(ValueError, match="AUTH_METHOD"):
        AuthSettings.from_env()


def test_cookie_secure_false(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "mock")
    monkeypatch.setenv("MOCK_USERNAME", "alice")
    monkeypatch.setenv("MOCK_PASSWORD", "secret")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    s = AuthSettings.from_env()
    assert s.cookie_secure is False


def test_oauth_settings(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "oauth")
    monkeypatch.setenv("OIDC_ISSUER_URL", "https://kc.example/realms/iris")
    monkeypatch.setenv("OIDC_CLIENT_ID", "iris")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setenv("OIDC_SCOPES", "openid profile email groups")
    s = AuthSettings.from_env()
    assert s.oidc.issuer_url == "https://kc.example/realms/iris"
    assert s.oidc.client_id == "iris"
    assert s.oidc.client_secret == "shh"
    assert s.oidc.scopes == ("openid", "profile", "email", "groups")


def test_oauth_missing_required(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "oauth")
    with pytest.raises(ValueError, match="OIDC_ISSUER_URL"):
        AuthSettings.from_env()


def test_ldap_settings(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "ldap")
    monkeypatch.setenv("LDAP_URL", "ldaps://ldap.example:636")
    monkeypatch.setenv("LDAP_BIND_DN_TEMPLATE", "uid={username},ou=people,dc=corp,dc=local")
    monkeypatch.setenv("LDAP_GROUP_BASE_DN", "ou=groups,dc=corp,dc=local")
    s = AuthSettings.from_env()
    assert s.ldap.url == "ldaps://ldap.example:636"
    assert s.ldap.bind_dn_template == "uid={username},ou=people,dc=corp,dc=local"


def test_mock_settings(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "mock")
    monkeypatch.setenv("MOCK_USERNAME", "alice")
    monkeypatch.setenv("MOCK_PASSWORD", "secret")
    monkeypatch.setenv("MOCK_GROUPS", "admins,users")
    monkeypatch.setenv("MOCK_DISPLAY_NAME", "Alice (mock)")
    s = AuthSettings.from_env()
    assert s.mock.username == "alice"
    assert s.mock.password == "secret"
    assert s.mock.groups == ("admins", "users")
    assert s.mock.display_name == "Alice (mock)"


def test_mock_missing_required(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "mock")
    monkeypatch.setenv("MOCK_USERNAME", "alice")
    with pytest.raises(ValueError, match="MOCK_PASSWORD"):
        AuthSettings.from_env()


def test_cookie_secure_invalid_raises(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "mock")
    monkeypatch.setenv("MOCK_USERNAME", "alice")
    monkeypatch.setenv("MOCK_PASSWORD", "secret")
    monkeypatch.setenv("COOKIE_SECURE", "ture")  # typo
    with pytest.raises(ValueError, match="COOKIE_SECURE"):
        AuthSettings.from_env()


def test_ttl_seconds_custom(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "mock")
    monkeypatch.setenv("MOCK_USERNAME", "alice")
    monkeypatch.setenv("MOCK_PASSWORD", "secret")
    monkeypatch.setenv("SESSION_TTL_SECONDS", "3600")
    s = AuthSettings.from_env()
    assert s.ttl_seconds == 3600


def test_ttl_seconds_invalid_raises(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "mock")
    monkeypatch.setenv("MOCK_USERNAME", "alice")
    monkeypatch.setenv("MOCK_PASSWORD", "secret")
    monkeypatch.setenv("SESSION_TTL_SECONDS", "abc")
    with pytest.raises(ValueError, match="SESSION_TTL_SECONDS"):
        AuthSettings.from_env()


def test_display_name_falls_back_to_stripped_username(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "mock")
    monkeypatch.setenv("MOCK_USERNAME", "  alice  ")
    monkeypatch.setenv("MOCK_PASSWORD", "secret")
    s = AuthSettings.from_env()
    assert s.mock.username == "alice"
    assert s.mock.display_name == "alice"  # not "  alice  "


def test_ldap_url_plaintext_rejected_when_tls_required(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "ldap")
    monkeypatch.setenv("LDAP_URL", "ldap://ldap.example.com:389")  # plaintext
    monkeypatch.setenv("LDAP_BIND_DN_TEMPLATE", "uid={username},ou=people,dc=corp,dc=local")
    monkeypatch.setenv("LDAP_GROUP_BASE_DN", "ou=groups,dc=corp,dc=local")
    # LDAP_REQUIRE_TLS defaults to True
    with pytest.raises(ValueError, match="LDAP_URL"):
        AuthSettings.from_env()


def test_ldap_url_plaintext_allowed_when_tls_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "ldap")
    monkeypatch.setenv("LDAP_URL", "ldap://ldap.example.com:389")
    monkeypatch.setenv("LDAP_BIND_DN_TEMPLATE", "uid={username},ou=people,dc=corp,dc=local")
    monkeypatch.setenv("LDAP_GROUP_BASE_DN", "ou=groups,dc=corp,dc=local")
    monkeypatch.setenv("LDAP_REQUIRE_TLS", "false")
    s = AuthSettings.from_env()
    assert s.ldap.url == "ldap://ldap.example.com:389"
    assert s.ldap.require_tls is False


def test_ldap_ca_cert_path_loaded(monkeypatch, tmp_path):
    fake_ca = tmp_path / "ca.pem"
    fake_ca.write_text("-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("AUTH_METHOD", "ldap")
    monkeypatch.setenv("LDAP_URL", "ldaps://ldap.example.com:636")
    monkeypatch.setenv("LDAP_BIND_DN_TEMPLATE", "uid={username},ou=people,dc=corp,dc=local")
    monkeypatch.setenv("LDAP_GROUP_BASE_DN", "ou=groups,dc=corp,dc=local")
    monkeypatch.setenv("LDAP_CA_CERT_PATH", str(fake_ca))
    s = AuthSettings.from_env()
    assert s.ldap.ca_cert_path == str(fake_ca)
    assert s.ldap.require_tls is True


def test_ldap_default_require_tls_true_with_ldaps(monkeypatch):
    """Default LDAP_REQUIRE_TLS=true; ldaps:// URL passes through."""
    monkeypatch.setenv("AUTH_METHOD", "ldap")
    monkeypatch.setenv("LDAP_URL", "ldaps://ldap.example.com:636")
    monkeypatch.setenv("LDAP_BIND_DN_TEMPLATE", "uid={username},ou=people,dc=corp,dc=local")
    monkeypatch.setenv("LDAP_GROUP_BASE_DN", "ou=groups,dc=corp,dc=local")
    s = AuthSettings.from_env()
    assert s.ldap.require_tls is True
    assert s.ldap.ca_cert_path is None


def test_session_absolute_ttl_default(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "mock")
    monkeypatch.setenv("MOCK_USERNAME", "alice")
    monkeypatch.setenv("MOCK_PASSWORD", "secret")
    s = AuthSettings.from_env()
    assert s.absolute_ttl_seconds == 2_592_000  # 30 days


def test_session_absolute_ttl_custom(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "mock")
    monkeypatch.setenv("MOCK_USERNAME", "alice")
    monkeypatch.setenv("MOCK_PASSWORD", "secret")
    monkeypatch.setenv("SESSION_ABSOLUTE_TTL_SECONDS", "86400")
    s = AuthSettings.from_env()
    assert s.absolute_ttl_seconds == 86_400
