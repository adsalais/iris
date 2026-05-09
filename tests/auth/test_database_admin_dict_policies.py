"""Tests for the DatabaseAdminSession.add_row_dict_policy / revoke wrappers."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock


def _session():
    from iris.auth.identity import User
    from iris.auth.rights import EMPTY_CAPABILITIES
    from iris.auth.views import DatabaseAdminSession

    return DatabaseAdminSession(
        id="x", user=User("s", "u", "U", ()),
        created_at=datetime.now(UTC), expires_at=datetime.now(UTC),
        data={}, capabilities=EMPTY_CAPABILITIES,
        client=MagicMock(), http_client=MagicMock(), settings=MagicMock(),
        store=MagicMock(), database="marketing",
    )


def test_add_row_dict_policy_calls_policies_helper(monkeypatch):
    captured = {}
    def fake_add(client, *, database, table, auth_id, dictionary,  # noqa: ARG001
                 authorisations, role, value):
        captured["args"] = (
            database, table, auth_id, dictionary, authorisations, role, value,
        )
    monkeypatch.setattr(
        "iris.auth.views.policies.add_row_dict_policy", fake_add,
    )
    s = _session()
    asyncio.run(s.add_row_dict_policy(
        table="events", auth_id="auth_id",
        dictionary="iris_dicts.auth_map", authorisations="authorisations",
        role="readers_GRP", value="public",
    ))
    assert captured["args"] == (
        "marketing", "events", "auth_id",
        "iris_dicts.auth_map", "authorisations",
        "readers_GRP", "public",
    )


def test_revoke_row_dict_policy_calls_policies_helper(monkeypatch):
    captured = {}
    def fake_revoke(client, *, database, table, auth_id, dictionary,  # noqa: ARG001
                    authorisations, role, value):
        captured["args"] = (
            database, table, auth_id, dictionary, authorisations, role, value,
        )
    monkeypatch.setattr(
        "iris.auth.views.policies.revoke_row_dict_policy", fake_revoke,
    )
    s = _session()
    asyncio.run(s.revoke_row_dict_policy(
        table="events", auth_id="auth_id",
        dictionary="iris_dicts.auth_map", authorisations="authorisations",
        role="readers_GRP", value="public",
    ))
    assert captured["args"] == (
        "marketing", "events", "auth_id",
        "iris_dicts.auth_map", "authorisations",
        "readers_GRP", "public",
    )
