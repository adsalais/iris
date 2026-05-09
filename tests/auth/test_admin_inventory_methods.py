"""Tests for AdminSession.list_users / list_databases / list_all_row_policies / list_all_grants.

Mock-based: each test monkeypatches the underlying ``audit.list_all_*``
helper, instantiates an AdminSession with mock CH refs, awaits the typed
method, and asserts the helper was called with the client returned by
``self._ch()``.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock


def _admin_session():
    from iris.auth.identity import User
    from iris.auth.rights import EMPTY_CAPABILITIES
    from iris.auth.views import AdminSession

    return AdminSession(
        id="x",
        user=User("s", "u", "U", ()),
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
        data={},
        capabilities=EMPTY_CAPABILITIES,
        client=MagicMock(),
        http_client=MagicMock(),
        settings=MagicMock(),
        store=MagicMock(),
    )


def test_list_users_calls_audit_helper(monkeypatch):
    captured = {}
    def fake(client):
        captured["client"] = client
        return [{"name": "alice", "groups": []}]
    monkeypatch.setattr("iris.auth.views.audit.list_all_users", fake)

    s = _admin_session()
    result = asyncio.run(s.list_users())
    assert result == [{"name": "alice", "groups": []}]
    assert captured["client"] is s.client


def test_list_databases_calls_audit_helper(monkeypatch):
    captured = {}
    def fake(client):
        captured["client"] = client
        return [{
            "name": "marketing",
            "admin_count": 1, "writer_count": 0, "reader_count": 0,
        }]
    monkeypatch.setattr("iris.auth.views.audit.list_all_databases", fake)

    s = _admin_session()
    result = asyncio.run(s.list_databases())
    assert result[0]["name"] == "marketing"
    assert captured["client"] is s.client


def test_list_all_row_policies_calls_audit_helper(monkeypatch):
    captured = {}
    def fake(client):
        captured["client"] = client
        return [{"database": "marketing", "table": "events"}]
    monkeypatch.setattr("iris.auth.views.audit.list_all_row_policies", fake)

    s = _admin_session()
    result = asyncio.run(s.list_all_row_policies())
    assert result[0]["database"] == "marketing"
    assert captured["client"] is s.client


def test_list_all_grants_calls_audit_helper(monkeypatch):
    captured = {}
    def fake(client):
        captured["client"] = client
        return [{
            "user_name": "alice", "database": "marketing", "access_type": "SELECT",
        }]
    monkeypatch.setattr("iris.auth.views.audit.list_all_grants", fake)

    s = _admin_session()
    result = asyncio.run(s.list_all_grants())
    assert result[0]["user_name"] == "alice"
    assert captured["client"] is s.client
