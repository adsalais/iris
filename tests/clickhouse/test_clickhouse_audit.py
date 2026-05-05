"""Tests for audit functions."""

from __future__ import annotations

import pytest

from iris.clickhouse.audit import (
    role_grants,
    role_row_policies,
    user_grants,
    user_role_memberships,
    user_row_policies,
)
from iris.clickhouse.grants import grant_select_to_database
from iris.clickhouse.identifiers import InvalidIdentifierError
from iris.clickhouse.policies import add_row_policy
from iris.clickhouse.users import init_user_rights


def test_user_grants_lists_user_grants(ch_client, ch_settings, prefix):
    username = f"{prefix}_aud_u"
    init_user_rights(ch_client, username=username, groups=[], settings=ch_settings)

    rows = user_grants(ch_client, username=username)
    # The user has no direct grants yet (their per-user role does, not the user).
    # Just verify the call succeeds and returns a list.
    assert isinstance(rows, list)


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
    init_user_rights(
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
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{prefix_db}`")
    ch_client.command(
        f"CREATE TABLE IF NOT EXISTS `{prefix_db}`.`t` (id UInt64, region String) ENGINE = MergeTree ORDER BY id"
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    add_row_policy(
        ch_client,
        database=prefix_db, table="t", column="region", role=role, value="EU",
        settings=ch_settings,
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
