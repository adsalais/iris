import asyncio

import pytest
from ldap3 import MOCK_SYNC, Connection, Server

from iris.auth.config import LDAPSettings
from iris.auth.exceptions import AuthError
from iris.auth.providers.ldap import LDAPProvider


@pytest.fixture
def settings() -> LDAPSettings:
    return LDAPSettings(
        url="fake://offline",
        bind_dn_template="uid={username},ou=people,dc=corp,dc=local",
        group_base_dn="ou=groups,dc=corp,dc=local",
        require_tls=False,
        ca_cert_path=None,
    )


@pytest.fixture
def directory():
    server = Server("fake")
    conn = Connection(server, client_strategy=MOCK_SYNC)
    conn.strategy.add_entry(
        "uid=alice,ou=people,dc=corp,dc=local",
        {"userPassword": "secret", "cn": "Alice", "objectClass": ["inetOrgPerson"]},
    )
    conn.strategy.add_entry(
        "uid=bob,ou=people,dc=corp,dc=local",
        {"userPassword": "hunter2", "cn": "Bob", "objectClass": ["inetOrgPerson"]},
    )
    conn.strategy.add_entry(
        "cn=admins,ou=groups,dc=corp,dc=local",
        {"member": ["uid=alice,ou=people,dc=corp,dc=local"], "objectClass": ["groupOfNames"]},
    )
    conn.strategy.add_entry(
        "cn=users,ou=groups,dc=corp,dc=local",
        {
            "member": [
                "uid=alice,ou=people,dc=corp,dc=local",
                "uid=bob,ou=people,dc=corp,dc=local",
            ],
            "objectClass": ["groupOfNames"],
        },
    )
    return conn


@pytest.fixture
def provider(settings, directory):
    return LDAPProvider(settings, _connection_factory=lambda: directory)


def test_authenticate_returns_user_with_groups(provider):
    user = asyncio.run(provider.authenticate("alice", "secret"))
    assert user.subject == "uid=alice,ou=people,dc=corp,dc=local"
    assert user.display_name == "Alice"
    assert set(user.groups) == {"admins", "users"}


def test_authenticate_returns_user_with_only_user_group(provider):
    user = asyncio.run(provider.authenticate("bob", "hunter2"))
    assert set(user.groups) == {"users"}


def test_authenticate_with_bad_password_raises(provider):
    with pytest.raises(AuthError) as exc:
        asyncio.run(provider.authenticate("alice", "wrong"))
    assert exc.value.token == "invalid_credentials"


def test_authenticate_with_unknown_user_raises(provider):
    with pytest.raises(AuthError) as exc:
        asyncio.run(provider.authenticate("nobody", "anything"))
    assert exc.value.token == "invalid_credentials"


def test_authenticate_rejects_dn_injection_in_username(provider):
    """Usernames containing DN metacharacters or out-of-charset bytes are rejected before bind."""
    import asyncio

    payloads = [
        "alice,ou=evil,dc=corp,dc=local",
        "alice=admin",
        "alice;ou=evil",
        "alice\\ou=evil",
        'alice"ou=evil',
        "alice\x00",
        "alice<>",
        "",                  # empty
        "a" * 65,            # over length cap
        " alice",            # leading whitespace
        "alice ",            # trailing whitespace
        "alice@example.com", # @ not in charset
    ]
    for p in payloads:
        with pytest.raises(AuthError) as exc:
            asyncio.run(provider.authenticate(p, "anything"))
        assert exc.value.token == "invalid_credentials", f"username={p!r} should be rejected"


def test_authenticate_accepts_normal_usernames(provider):
    """Allowed: letters, digits, underscore, dot, hyphen, up to 64 chars."""
    import asyncio

    user = asyncio.run(provider.authenticate("alice", "secret"))
    assert user.subject == "uid=alice,ou=people,dc=corp,dc=local"


def test_open_connection_classifies_invalid_credentials_via_typed_exception(settings):
    """When ldap3 raises LDAPInvalidCredentialsResult, surface as _BindFailed."""
    from ldap3.core.exceptions import LDAPInvalidCredentialsResult
    from iris.auth.providers.ldap import LDAPProvider, _BindFailed

    def factory():
        class _C:
            def rebind(self, *, user, password):
                raise LDAPInvalidCredentialsResult(result=49)
        return _C()

    provider = LDAPProvider(settings, _connection_factory=factory)
    with pytest.raises(_BindFailed):
        provider._open_connection("uid=x,...", "pw")


def test_open_connection_classifies_socket_open_as_unreachable(settings):
    """When ldap3 raises LDAPSocketOpenError, surface as _Unreachable."""
    from ldap3.core.exceptions import LDAPSocketOpenError
    from iris.auth.providers.ldap import LDAPProvider, _Unreachable

    def factory():
        class _C:
            def rebind(self, *, user, password):
                raise LDAPSocketOpenError("connection refused")
        return _C()

    provider = LDAPProvider(settings, _connection_factory=factory)
    with pytest.raises(_Unreachable):
        provider._open_connection("uid=x,...", "pw")


def test_open_connection_localized_invalid_credentials_classified_correctly(settings):
    """Locale-independent classification: a non-English error msg from ldap3 is still
    classified as bad creds via the typed exception, not the substring match."""
    from ldap3.core.exceptions import LDAPInvalidCredentialsResult
    from iris.auth.providers.ldap import LDAPProvider, _BindFailed

    def factory():
        class _C:
            def rebind(self, *, user, password):
                # Force a non-English message; the typed exception still wins.
                raise LDAPInvalidCredentialsResult(
                    result=49,
                    description="identifiants invalides",
                )
        return _C()

    provider = LDAPProvider(settings, _connection_factory=factory)
    with pytest.raises(_BindFailed):
        provider._open_connection("uid=x,...", "pw")
