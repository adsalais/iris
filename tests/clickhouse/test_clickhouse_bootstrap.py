"""Tests for ensure_service_admin."""

from __future__ import annotations

from iris.clickhouse.bootstrap import ensure_service_admin


def test_ensure_service_admin_creates_role_and_grants_to_user(ch_settings, ch_client):
    # ch_client fixture has already invoked ensure_service_admin; verify state.
    rows = list(
        ch_client.query(
            "SELECT name FROM system.roles WHERE name = {r:String}",
            parameters={"r": ch_settings.service_admin_role},
        ).named_results()
    )
    assert rows == [{"name": ch_settings.service_admin_role}]

    grants = list(
        ch_client.query(
            "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String} AND granted_role_name = {r:String}",
            parameters={
                "u": ch_settings.service_admin_user,
                "r": ch_settings.service_admin_role,
            },
        ).named_results()
    )
    assert grants == [{"granted_role_name": ch_settings.service_admin_role}]


def test_ensure_service_admin_is_idempotent(ch_settings, ch_client):
    # Running again should not raise.
    ensure_service_admin(ch_client, ch_settings)
    ensure_service_admin(ch_client, ch_settings)
    rows = list(
        ch_client.query(
            "SELECT count() AS n FROM system.roles WHERE name = {r:String}",
            parameters={"r": ch_settings.service_admin_role},
        ).named_results()
    )
    assert rows == [{"n": 1}]
