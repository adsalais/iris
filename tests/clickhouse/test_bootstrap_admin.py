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

    # Either an expanded ROLE ADMIN row or a condensed ALL row counts as
    # admin coverage. CH stores GRANT ALL as an 'ALL' row when the granter
    # holds the full privilege set.
    rows = ch_client.query(
        """
        SELECT count() FROM system.grants
        WHERE role_name = {r:String}
          AND database IS NULL
          AND access_type IN ('ROLE ADMIN', 'ALL')
          AND grant_option = 1
        """,
        parameters={"r": group_role},
    ).result_rows
    assert rows[0][0] >= 1

    granted = ch_client.query(
        """
        SELECT granted_role_name FROM system.role_grants
        WHERE role_name = {r:String}
        """,
        parameters={"r": group_role},
    ).result_rows
    assert any(g[0] == GLOBAL_ADMIN_ROLE for g in granted)


def test_bootstrap_user_channel_seeds_each_distinct_admin_name(ch_client, prefix):
    """Detection is per-configured-name, not per-channel. If the operator
    rotates CLICKHOUSE_ADMIN_USER from a -> b, the new admin gets seeded.

    Replaces a prior test that enforced the old heuristic (skip-on-any-admin
    in the channel). That heuristic was vulnerable to false positives from
    manual operator grants on unrelated _USER roles.
    """
    _drop_admin_roles_with_suffix(ch_client, "_USER")
    a = f"{prefix}_existing_admin"
    b = f"{prefix}_second_admin"
    bootstrap_admin(ch_client, admin_user=a)
    bootstrap_admin(ch_client, admin_user=b)
    # Both admins are seeded under the new deterministic detection.
    assert derive_rights(ch_client, username=a, groups=[]).is_admin is True
    assert derive_rights(ch_client, username=b, groups=[]).is_admin is True


def test_bootstrap_group_channel_seeds_each_distinct_admin_name(ch_client, prefix):
    _drop_admin_roles_with_suffix(ch_client, "_GRP")
    a = f"{prefix}_existing_grp"
    b = f"{prefix}_second_grp"
    bootstrap_admin(ch_client, admin_group=a)
    bootstrap_admin(ch_client, admin_group=b)
    rows = ch_client.query(
        """
        SELECT count() FROM system.grants
        WHERE role_name = {r:String}
          AND database IS NULL
          AND access_type IN ('ROLE ADMIN', 'ALL')
          AND grant_option = 1
        """,
        parameters={"r": f"{b}_GRP"},
    ).result_rows
    assert rows[0][0] >= 1


def test_bootstrap_user_channel_runs_when_unrelated_user_holds_role_admin(ch_client, prefix):
    """If an unrelated _USER role holds ROLE ADMIN WGO (e.g., manual operator
    grant), bootstrap should still seed the configured admin user. The old
    heuristic skipped here, leaving the configured admin un-bootstrapped."""
    _drop_admin_roles_with_suffix(ch_client, "_USER")

    # Pre-seed an unrelated _USER role that holds ROLE ADMIN WGO.
    decoy_role = f"{prefix}_decoy_USER"
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{decoy_role}`")
    ch_client.command(f"GRANT ROLE ADMIN ON *.* TO `{decoy_role}` WITH GRANT OPTION")

    # The configured admin is a different user.
    user = f"{prefix}_real_admin"
    bootstrap_admin(ch_client, admin_user=user)

    # The configured admin must have been seeded (with iris_global_admin grant).
    granted = ch_client.query(
        """
        SELECT granted_role_name FROM system.role_grants
        WHERE role_name = {r:String}
        """,
        parameters={"r": f"{user}_USER"},
    ).result_rows
    assert any(g[0] == GLOBAL_ADMIN_ROLE for g in granted), (
        f"configured admin {user}_USER was not bootstrapped despite decoy "
        f"holding ROLE ADMIN; granted = {granted}"
    )

    # Cleanup so subsequent tests aren't affected.
    ch_client.command(f"DROP ROLE IF EXISTS `{decoy_role}`")


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
          AND database IS NULL
          AND access_type IN ('ROLE ADMIN', 'ALL')
          AND grant_option = 1
        """,
        parameters={"r": f"{group}_GRP"},
    ).result_rows
    assert rows[0][0] >= 1
