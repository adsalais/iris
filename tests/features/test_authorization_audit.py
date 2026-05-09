from __future__ import annotations

import asyncio


def test_audit_section_renders_grants_list(app, capability_session, monkeypatch):
    async def fake_list_members(self):  # noqa: ARG001
        return {"admin": [], "reader": [], "writer": []}
    async def fake_list_policies(self):  # noqa: ARG001
        return []
    async def fake_list_grants(self):  # noqa: ARG001
        return [
            {"user_name": "alice", "role_name": None, "access_type": "SELECT",
             "database": "marketing", "table": None, "column": None,
             "is_partial_revoke": 0, "grant_option": 0},
        ]
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.list_members", fake_list_members,
    )
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.list_row_policies", fake_list_policies,
    )
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.list_grants", fake_list_grants,
    )

    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": "AU12CD34", "feature": "auth", "intent": "manage",
         "params": {"database": "marketing"}, "title": "Manage marketing"},
    ]}))
    r = client.get("/feature/auth/AU12CD34/manage?database=marketing")
    assert r.status_code == 200
    assert "Audit" in r.text
    assert "alice" in r.text
    assert "SELECT" in r.text
