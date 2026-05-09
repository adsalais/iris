"""Tests for DatabaseAdminSession.list_members.

Mock-based: monkeypatches ``grants.list_tier_members``, instantiates
DatabaseAdminSession with mock CH refs, awaits the typed method, and
asserts the helper received the client from ``self._ch()`` and the
session's database name.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock


def test_list_members_calls_grants_helper_with_self_database(monkeypatch):
    captured = {}
    def fake(client, *, database):
        captured["client"] = client
        captured["database"] = database
        return {
            "admin": [{"kind": "role", "name": "alice_USER"}],
            "reader": [],
            "writer": [],
        }
    monkeypatch.setattr("iris.auth.views.grants.list_tier_members", fake)

    from iris.auth.identity import User
    from iris.auth.rights import EMPTY_CAPABILITIES
    from iris.auth.views import DatabaseAdminSession

    s = DatabaseAdminSession(
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
        database="marketing",
    )

    result = asyncio.run(s.list_members())
    assert result["admin"][0]["name"] == "alice_USER"
    assert captured["database"] == "marketing"
    assert captured["client"] is s.client
