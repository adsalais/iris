from __future__ import annotations

import asyncio


def _seed(app, sid: str, database="marketing", tab_id="MG12CD34"):
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": tab_id, "feature": "auth", "intent": "manage",
         "params": {"database": database}, "title": f"Manage {database}"},
    ]}))


def _csrf(client):
    client.get("/")
    return {"Datastar-Request": "true",
            "X-CSRF-Token": client.cookies.get("iris_csrf") or ""}


def test_add_policy_403_when_not_db_admin(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed(app, sid)
    r = client.post(
        "/feature/auth/MG12CD34/policies",
        params={"database": "marketing", "table": "events",
                "column": "user_id", "role": "r1", "value": "alice"},
        headers=_csrf(client),
    )
    assert r.status_code == 403


def test_add_policy_calls_db_session_method(app, capability_session, monkeypatch):
    calls = []
    async def fake_add(self, *, table, column, role, value):  # noqa: ARG001
        calls.append(("add", table, column, role, value))
    async def fake_list(self):  # noqa: ARG001
        return []
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.add_row_policy", fake_add,
    )
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.list_row_policies", fake_list,
    )
    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    r = client.post(
        "/feature/auth/MG12CD34/policies",
        params={"database": "marketing", "table": "events",
                "column": "user_id", "role": "r1", "value": "alice"},
        headers=_csrf(client),
    )
    assert r.status_code == 200
    assert calls == [("add", "events", "user_id", "r1", "alice")]
    assert "MG12CD34-policies" in r.text


def test_revoke_policy_calls_db_session_method(app, capability_session, monkeypatch):
    calls = []
    async def fake_rev(self, *, table, role, value):  # noqa: ARG001
        calls.append(("rev", table, role, value))
    async def fake_list(self):  # noqa: ARG001
        return []
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.revoke_row_policy", fake_rev,
    )
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.list_row_policies", fake_list,
    )
    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    r = client.delete(
        "/feature/auth/MG12CD34/policies",
        params={"database": "marketing", "table": "events",
                "role": "r1", "value": "alice"},
        headers=_csrf(client),
    )
    assert r.status_code == 200
    assert calls == [("rev", "events", "r1", "alice")]
