"""Tests for init_user_rights — staged across Tasks 11/12/13."""

from __future__ import annotations

import pytest

from iris.clickhouse.identifiers import InvalidIdentifierError
from iris.clickhouse.users import (
    GROUP_ROLE_SUFFIX,
    USER_ROLE_SUFFIX,
    init_user_rights,
)


def test_init_user_rights_creates_user_and_per_user_role(ch_client, ch_settings, prefix):
    username = f"{prefix}_alice"
    init_user_rights(ch_client, username=username, groups=[], settings=ch_settings)

    users = list(
        ch_client.query(
            "SELECT name FROM system.users WHERE name = {u:String}",
            parameters={"u": username},
        ).named_results()
    )
    assert users == [{"name": username}]

    user_role = username + USER_ROLE_SUFFIX
    roles = list(
        ch_client.query(
            "SELECT name FROM system.roles WHERE name = {r:String}",
            parameters={"r": user_role},
        ).named_results()
    )
    assert roles == [{"name": user_role}]

    role_grants = list(
        ch_client.query(
            "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String} AND granted_role_name = {r:String}",
            parameters={"u": username, "r": user_role},
        ).named_results()
    )
    assert role_grants == [{"granted_role_name": user_role}]


def test_init_user_rights_is_idempotent(ch_client, ch_settings, prefix):
    username = f"{prefix}_idem"
    init_user_rights(ch_client, username=username, groups=[], settings=ch_settings)
    init_user_rights(ch_client, username=username, groups=[], settings=ch_settings)

    user_role = username + USER_ROLE_SUFFIX
    n = list(
        ch_client.query(
            "SELECT count() AS n FROM system.role_grants WHERE user_name = {u:String} AND granted_role_name = {r:String}",
            parameters={"u": username, "r": user_role},
        ).named_results()
    )
    assert n == [{"n": 1}]


def test_init_user_rights_rejects_bad_username(ch_client, ch_settings):
    with pytest.raises(InvalidIdentifierError):
        init_user_rights(ch_client, username="bad name", groups=[], settings=ch_settings)


def test_init_user_rights_rejects_bad_group(ch_client, ch_settings, prefix):
    with pytest.raises(InvalidIdentifierError):
        init_user_rights(
            ch_client,
            username=f"{prefix}_u",
            groups=["good", "bad group"],
            settings=ch_settings,
        )


def test_user_role_suffix_constant():
    assert USER_ROLE_SUFFIX == "_USER"
    assert GROUP_ROLE_SUFFIX == "_GRP"


def _grp_roles_for(client, username):
    rows = list(
        client.query(
            "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String}",
            parameters={"u": username},
        ).named_results()
    )
    return {
        row["granted_role_name"]
        for row in rows
        if row["granted_role_name"].endswith(GROUP_ROLE_SUFFIX)
    }


def test_init_user_rights_grants_group_roles(ch_client, ch_settings, prefix):
    username = f"{prefix}_g"
    init_user_rights(
        ch_client,
        username=username,
        groups=["sales", "ops"],
        settings=ch_settings,
    )
    assert _grp_roles_for(ch_client, username) == {"sales_GRP", "ops_GRP"}


def test_init_user_rights_revokes_groups_user_no_longer_has(ch_client, ch_settings, prefix):
    username = f"{prefix}_r"
    init_user_rights(
        ch_client,
        username=username,
        groups=["a", "b"],
        settings=ch_settings,
    )
    assert _grp_roles_for(ch_client, username) == {"a_GRP", "b_GRP"}

    init_user_rights(
        ch_client,
        username=username,
        groups=["b", "c"],
        settings=ch_settings,
    )
    assert _grp_roles_for(ch_client, username) == {"b_GRP", "c_GRP"}


def test_init_user_rights_does_not_touch_user_role_during_reconcile(
    ch_client, ch_settings, prefix
):
    """The per-user `_USER` role must stay granted regardless of `groups` content."""
    username = f"{prefix}_keep"
    init_user_rights(
        ch_client,
        username=username,
        groups=["x"],
        settings=ch_settings,
    )
    init_user_rights(
        ch_client,
        username=username,
        groups=[],
        settings=ch_settings,
    )
    user_role = username + USER_ROLE_SUFFIX
    rows = list(
        ch_client.query(
            "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String} AND granted_role_name = {r:String}",
            parameters={"u": username, "r": user_role},
        ).named_results()
    )
    assert rows == [{"granted_role_name": user_role}]


def test_init_user_rights_grants_impersonate_to_service_admin(
    ch_client, ch_settings, prefix
):
    username = f"{prefix}_imp"
    init_user_rights(ch_client, username=username, groups=[], settings=ch_settings)

    # Verify the service admin can impersonate the newly provisioned user.
    # The check queries system.grants for IMPERSONATE rows held by the service
    # admin; coverage is satisfied by either a specific grant (access_object ==
    # username) or a wildcard grant (access_object == '', which covers all users
    # and absorbs per-user grants when both exist).
    rows = list(
        ch_client.query(
            "SELECT * FROM system.grants WHERE user_name = {sa:String} AND access_type = 'IMPERSONATE'",
            parameters={"sa": ch_settings.service_admin_user},
        ).named_results()
    )
    covered = any(r.get("access_object") in (username, "") for r in rows)
    assert covered, f"No IMPERSONATE coverage for {username!r} in: {rows}"
