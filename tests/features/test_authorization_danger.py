from __future__ import annotations

import asyncio


def _seed(app, sid: str, database="marketing", tab_id="DG12CD34"):
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": tab_id, "feature": "auth", "intent": "manage",
         "params": {"database": database}, "title": f"Manage {database}"},
    ]}))


def _csrf(client):
    client.get("/")
    return {"Datastar-Request": "true",
            "X-CSRF-Token": client.cookies.get("iris_csrf") or ""}


def test_delete_database_403_when_not_db_admin(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed(app, sid)
    r = client.delete(
        "/feature/auth/DG12CD34/database",
        params={"confirm": "marketing"},
        headers=_csrf(client),
    )
    assert r.status_code == 403


def test_delete_database_400_when_confirm_mismatches(app, capability_session, monkeypatch):
    async def must_not_call(self):  # noqa: ARG001
        msg = "delete_database should not have been called"
        raise AssertionError(msg)
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.delete_database", must_not_call,
    )
    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    r = client.delete(
        "/feature/auth/DG12CD34/database",
        params={"confirm": "wrong-name"},
        headers=_csrf(client),
    )
    assert r.status_code == 400


def test_delete_database_calls_method_and_closes_tab(app, capability_session, monkeypatch):
    called = []
    async def fake_delete(self):  # noqa: ARG001
        called.append("delete")
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.delete_database", fake_delete,
    )
    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    r = client.delete(
        "/feature/auth/DG12CD34/database",
        params={"confirm": "marketing"},
        headers=_csrf(client),
    )
    assert r.status_code == 200
    assert called == ["delete"]
    refreshed = asyncio.run(app.state.auth_session_store.get_and_refresh(sid))
    assert refreshed is not None
    assert refreshed.data.get("tabs", []) == []
    assert "tab-button-DG12CD34" in r.text
    assert "tab-content-DG12CD34" in r.text
