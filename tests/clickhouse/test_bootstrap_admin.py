"""Tests for iris.auth.bootstrap.bootstrap_admin against the CH testcontainer.

Lives under tests/clickhouse/ because it depends on the CH fixture; the code
under test is in iris.auth.bootstrap.

Bootstrap inspects global CH state (any role ending in _USER with ROLE ADMIN+
WGO at global scope counts as "admin already present"). The session-scoped
container shares state across tests, so tests that depend on "no admin exists"
clear matching roles at setup.
"""
from iris.auth.bootstrap import bootstrap_admin
from iris.clickhouse.rights import derive_rights


def _drop_admin_user_roles(ch_client) -> None:
    """Drop every role ending in _USER that holds ROLE ADMIN+WGO at global
    scope. Lets a test simulate a fresh CH where no admin has been seeded."""
    rows = ch_client.query(
        """
        SELECT DISTINCT role_name FROM system.grants
        WHERE access_type = 'ROLE ADMIN'
          AND grant_option = 1
          AND database IS NULL
          AND endsWith(role_name, '_USER')
        """
    ).result_rows
    for (name,) in rows:
        ch_client.command(f"DROP ROLE IF EXISTS `{name}`")


def test_bootstrap_creates_admin_when_absent(ch_client, prefix):
    _drop_admin_user_roles(ch_client)
    user = f"{prefix}_first_admin"
    bootstrap_admin(ch_client, username=user)
    r = derive_rights(ch_client, username=user, groups=[])
    assert r.is_admin is True


def test_bootstrap_skips_when_admin_exists(ch_client, prefix):
    _drop_admin_user_roles(ch_client)
    a = f"{prefix}_existing"
    b = f"{prefix}_second"
    bootstrap_admin(ch_client, username=a)
    bootstrap_admin(ch_client, username=b)
    # Second call must not seed b — it sees the existing admin and skips.
    r = derive_rights(ch_client, username=b, groups=[])
    assert r.is_admin is False


def test_bootstrap_idempotent_for_same_user(ch_client, prefix):
    """Two calls in a row must not error.

    Whether the user actually ends up admin depends on whether another admin
    already exists in CH (from a previous test in the same session) — that's
    the documented behavior: first-install seeds, subsequent calls skip. The
    test asserts only the no-error contract.
    """
    user = f"{prefix}_repeat"
    bootstrap_admin(ch_client, username=user)
    bootstrap_admin(ch_client, username=user)
