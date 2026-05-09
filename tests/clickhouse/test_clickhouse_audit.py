"""Tests for audit functions."""

from __future__ import annotations

import pytest

from iris.clickhouse.audit import (
    role_grants,
    role_row_policies,
    table_row_policies,
    user_grants,
    user_role_memberships,
    user_row_policies,
)
from iris.clickhouse.grants import grant_select_to_database
from iris.clickhouse.identifiers import InvalidIdentifierError
from iris.clickhouse.policies import add_row_policy
from iris.clickhouse.users import provision_user


def test_user_grants_lists_user_grants(ch_client, ch_settings, prefix):
    username = f"{prefix}_aud_u"
    db = f"{prefix}_aud_u_db"
    provision_user(ch_client, username=username, groups=[], settings=ch_settings)
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(f"GRANT SELECT ON `{db}`.* TO `{username}`")

    rows = user_grants(ch_client, username=username)
    select_grant = next(
        (r for r in rows if r["access_type"] == "SELECT" and r["database"] == db),
        None,
    )
    assert select_grant is not None, rows


def test_role_grants_lists_role_grants(ch_client, ch_settings, prefix):
    db = f"{prefix}_aud_db"
    role = f"{prefix}_aud_role"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    grant_select_to_database(ch_client, database=db, role=role)

    rows = role_grants(ch_client, role=role)
    select_grant = next(
        (r for r in rows if r["access_type"] == "SELECT" and r["database"] == db),
        None,
    )
    assert select_grant is not None, rows


def test_audit_validates_inputs(ch_client):
    with pytest.raises(InvalidIdentifierError):
        user_grants(ch_client, username="bad name")
    with pytest.raises(InvalidIdentifierError):
        role_grants(ch_client, role="bad role")


def test_user_role_memberships(ch_client, ch_settings, prefix):
    username = f"{prefix}_mem"
    provision_user(
        ch_client,
        username=username,
        groups=["alpha", "beta"],
        settings=ch_settings,
    )

    rows = user_role_memberships(ch_client, username=username)
    granted = {r["granted_role_name"] for r in rows}
    assert f"{username}_USER" in granted
    assert "alpha_GRP" in granted
    assert "beta_GRP" in granted


def _setup_policy_for_role(ch_client, ch_settings, prefix_db, role):
    from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
    from iris.clickhouse.grants import TIER_DBADMIN, tier_role_name

    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{prefix_db}`")
    ch_client.command(
        f"CREATE TABLE IF NOT EXISTS `{prefix_db}`.`t` (id UInt64, region String) ENGINE = MergeTree ORDER BY id"
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    # Wildcard targets must exist before add_row_policy creates policies on them.
    ch_client.command(
        f"CREATE ROLE IF NOT EXISTS `{tier_role_name(prefix_db, TIER_DBADMIN)}`"
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{GLOBAL_ADMIN_ROLE}`")
    add_row_policy(
        ch_client,
        database=prefix_db, table="t", column="region", role=role, value="EU",
    )


def test_role_row_policies(ch_client, ch_settings, prefix):
    db = f"{prefix}_rrp_db"
    role = f"{prefix}_rrp_role"
    _setup_policy_for_role(ch_client, ch_settings, db, role)

    rows = role_row_policies(ch_client, role=role)
    assert any(r["database"] == db for r in rows), rows


def test_user_row_policies(ch_client, ch_settings, prefix):
    db = f"{prefix}_urp_db"
    role = f"{prefix}_urp_role"
    user = f"{prefix}_urp_user"
    _setup_policy_for_role(ch_client, ch_settings, db, role)
    ch_client.command(f"CREATE USER IF NOT EXISTS `{user}` IDENTIFIED WITH no_password")
    ch_client.command(f"GRANT `{role}` TO `{user}`")

    rows = user_row_policies(ch_client, username=user)
    assert any(r["database"] == db for r in rows), rows


def test_table_row_policies(ch_client, ch_settings, prefix):
    db = f"{prefix}_trp_db"
    role = f"{prefix}_trp_role"
    _setup_policy_for_role(ch_client, ch_settings, db, role)

    rows = table_row_policies(ch_client, database=db, table="t")
    assert any(r["database"] == db and r["table"] == "t" for r in rows), rows


def test_list_all_users_returns_users_with_role_lists(ch_client, ch_settings, prefix):
    """Each user row carries the names of granted roles."""
    from iris.clickhouse.audit import list_all_users

    user = f"{prefix}_listusr"
    role = f"{prefix}_listrole"
    ch_client.command(f"CREATE USER `{user}` IDENTIFIED BY 'pw'")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    ch_client.command(f"GRANT `{role}` TO `{user}`")

    result = list_all_users(ch_client)
    by_name = {row["name"]: row for row in result}
    assert user in by_name
    assert role in by_name[user]["groups"]


def test_list_all_databases_returns_tier_counts(ch_client, ch_settings, prefix):
    """Each database row carries admin_count, writer_count, reader_count
    derived from system.role_grants."""
    from iris.clickhouse.audit import list_all_databases
    from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
    from iris.clickhouse.grants import (
        TIER_DBADMIN,
        TIER_DBWRITER,
        create_tier_roles,
        grant_tier_to_user,
    )

    db = f"{prefix}_listdb"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{GLOBAL_ADMIN_ROLE}`")
    create_tier_roles(ch_client, database=db)
    grant_tier_to_user(
        ch_client, database=db, tier=TIER_DBADMIN,
        username=f"{prefix}_listdb_alice",
    )
    grant_tier_to_user(
        ch_client, database=db, tier=TIER_DBWRITER,
        username=f"{prefix}_listdb_bob",
    )

    result = list_all_databases(ch_client)
    by_name = {row["name"]: row for row in result}
    assert db in by_name
    assert by_name[db]["admin_count"] >= 1
    assert by_name[db]["writer_count"] >= 1
    assert by_name[db]["reader_count"] == 0


def test_list_all_row_policies_includes_seeded_policy(ch_client, ch_settings, prefix):
    """Returns full system.row_policies rows; seeded policy must appear."""
    from iris.clickhouse.audit import list_all_row_policies

    db = f"{prefix}_listpol_db"
    role = f"{prefix}_listpol_role"
    _setup_policy_for_role(ch_client, ch_settings, db, role)

    result = list_all_row_policies(ch_client)
    seen = {(row["database"], row["table"]) for row in result}
    assert (db, "t") in seen


def test_list_all_grants_includes_seeded_grant(ch_client, ch_settings, prefix):
    """Returns full system.grants rows; seeded grant must appear."""
    from iris.clickhouse.audit import list_all_grants

    db = f"{prefix}_listgrants"
    user = f"{prefix}_listgrants_alice"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(f"CREATE USER `{user}` IDENTIFIED BY 'pw'")
    ch_client.command(f"GRANT SELECT ON `{db}`.* TO `{user}`")

    result = list_all_grants(ch_client)
    seen = {
        (row.get("user_name"), row.get("database"), row.get("access_type"))
        for row in result
    }
    assert (user, db, "SELECT") in seen
