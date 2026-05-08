"""Tests for iris.clickhouse.bootstrap.bootstrap_admin against the CH testcontainer.

Bootstrap inspects global CH state (any role with the appropriate suffix
holding ROLE ADMIN+WGO at global scope counts as "admin already present").
The session-scoped container shares state across tests, so tests that
depend on "no admin exists" clear matching roles at setup.
"""
from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE, bootstrap_admin
from iris.clickhouse.rights import derive_rights


def _drop_admin_roles_with_suffix(ch_client, suffix: str) -> None:
    rows = ch_client.query(
        """
        SELECT DISTINCT role_name FROM system.grants
        WHERE access_type = 'ROLE ADMIN'
          AND grant_option = 1
          AND database IS NULL
          AND endsWith(role_name, {s:String})
        """,
        parameters={"s": suffix},
    ).result_rows
    for (name,) in rows:
        ch_client.command(f"DROP ROLE IF EXISTS `{name}`")


def test_bootstrap_creates_global_admin_role_unconditionally(ch_client):
    bootstrap_admin(ch_client)
    rows = ch_client.query(
        "SELECT count() FROM system.roles WHERE name = {n:String}",
        parameters={"n": GLOBAL_ADMIN_ROLE},
    ).result_rows
    assert rows[0][0] == 1


def test_bootstrap_user_channel_creates_admin_user_role(ch_client, prefix):
    _drop_admin_roles_with_suffix(ch_client, "_USER")
    user = f"{prefix}_first_admin"
    bootstrap_admin(ch_client, admin_user=user)

    r = derive_rights(ch_client, username=user, groups=[])
    assert r.is_admin is True

    granted = ch_client.query(
        """
        SELECT granted_role_name FROM system.role_grants
        WHERE role_name = {r:String}
        """,
        parameters={"r": f"{user}_USER"},
    ).result_rows
    assert any(g[0] == GLOBAL_ADMIN_ROLE for g in granted)


def test_bootstrap_group_channel_creates_admin_group_role(ch_client, prefix):
    _drop_admin_roles_with_suffix(ch_client, "_GRP")
    group = f"{prefix}_iris_admin"
    bootstrap_admin(ch_client, admin_group=group)

    group_role = f"{group}_GRP"

    rows = ch_client.query(
        """
        SELECT count() FROM system.grants
        WHERE role_name = {r:String}
          AND access_type = 'ROLE ADMIN'
          AND grant_option = 1
        """,
        parameters={"r": group_role},
    ).result_rows
    assert rows[0][0] == 1

    granted = ch_client.query(
        """
        SELECT granted_role_name FROM system.role_grants
        WHERE role_name = {r:String}
        """,
        parameters={"r": group_role},
    ).result_rows
    assert any(g[0] == GLOBAL_ADMIN_ROLE for g in granted)


def test_bootstrap_user_channel_skips_when_admin_already_exists(ch_client, prefix):
    _drop_admin_roles_with_suffix(ch_client, "_USER")
    a = f"{prefix}_existing_admin"
    b = f"{prefix}_second_admin"
    bootstrap_admin(ch_client, admin_user=a)
    bootstrap_admin(ch_client, admin_user=b)
    r = derive_rights(ch_client, username=b, groups=[])
    assert r.is_admin is False


def test_bootstrap_group_channel_skips_when_admin_already_exists(ch_client, prefix):
    _drop_admin_roles_with_suffix(ch_client, "_GRP")
    a = f"{prefix}_existing_grp"
    b = f"{prefix}_second_grp"
    bootstrap_admin(ch_client, admin_group=a)
    bootstrap_admin(ch_client, admin_group=b)
    rows = ch_client.query(
        """
        SELECT count() FROM system.grants
        WHERE role_name = {r:String}
          AND access_type = 'ROLE ADMIN'
          AND grant_option = 1
        """,
        parameters={"r": f"{b}_GRP"},
    ).result_rows
    assert rows[0][0] == 0


def test_bootstrap_idempotent_for_same_inputs(ch_client, prefix):
    """Two calls in a row don't error."""
    user = f"{prefix}_repeat"
    bootstrap_admin(ch_client, admin_user=user)
    bootstrap_admin(ch_client, admin_user=user)


def test_bootstrap_both_channels_independent(ch_client, prefix):
    _drop_admin_roles_with_suffix(ch_client, "_USER")
    _drop_admin_roles_with_suffix(ch_client, "_GRP")
    user = f"{prefix}_both_user"
    group = f"{prefix}_both_grp"
    bootstrap_admin(ch_client, admin_user=user, admin_group=group)

    r = derive_rights(ch_client, username=user, groups=[])
    assert r.is_admin is True

    rows = ch_client.query(
        """
        SELECT count() FROM system.grants
        WHERE role_name = {r:String}
          AND access_type = 'ROLE ADMIN'
          AND grant_option = 1
        """,
        parameters={"r": f"{group}_GRP"},
    ).result_rows
    assert rows[0][0] == 1
