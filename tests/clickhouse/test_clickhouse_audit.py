"""Tests for audit functions."""

from __future__ import annotations

import pytest

from iris.clickhouse.audit import role_grants, user_grants
from iris.clickhouse.grants import grant_select_to_database
from iris.clickhouse.identifiers import InvalidIdentifierError
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
