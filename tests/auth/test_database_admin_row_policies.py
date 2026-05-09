from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock


def test_add_row_policy_calls_policies_helper(monkeypatch):
    captured = {}
    def fake_add(client, *, database, table, column, role, value):  # noqa: ARG001
        captured["args"] = (database, table, column, role, value)
    monkeypatch.setattr(
        "iris.auth.views.policies.add_row_policy", fake_add,
    )
    from iris.auth.identity import User
    from iris.auth.rights import EMPTY_CAPABILITIES
    from iris.auth.views import DatabaseAdminSession

    s = DatabaseAdminSession(
        id="x", user=User("s", "u", "U", ()),
        created_at=datetime.now(UTC), expires_at=datetime.now(UTC),
        data={}, capabilities=EMPTY_CAPABILITIES,
        client=MagicMock(), http_client=MagicMock(), settings=MagicMock(),
        store=MagicMock(), database="marketing",
    )
    asyncio.run(s.add_row_policy(table="events", column="user_id",
                                  role="r1", value="alice"))
    assert captured["args"] == ("marketing", "events", "user_id", "r1", "alice")


def test_revoke_row_policy_calls_policies_helper(monkeypatch):
    captured = {}
    def fake_revoke(client, *, database, table, role, value):  # noqa: ARG001
        captured["args"] = (database, table, role, value)
    monkeypatch.setattr(
        "iris.auth.views.policies.revoke_row_policy", fake_revoke,
    )
    from iris.auth.identity import User
    from iris.auth.rights import EMPTY_CAPABILITIES
    from iris.auth.views import DatabaseAdminSession

    s = DatabaseAdminSession(
        id="x", user=User("s", "u", "U", ()),
        created_at=datetime.now(UTC), expires_at=datetime.now(UTC),
        data={}, capabilities=EMPTY_CAPABILITIES,
        client=MagicMock(), http_client=MagicMock(), settings=MagicMock(),
        store=MagicMock(), database="marketing",
    )
    asyncio.run(s.revoke_row_policy(table="events", role="r1", value="alice"))
    assert captured["args"] == ("marketing", "events", "r1", "alice")
