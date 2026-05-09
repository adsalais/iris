"""Members tier grants/revokes for users + groups."""
from __future__ import annotations

import asyncio


def _seed(app, sid: str, database: str = "marketing", tab_id: str = "MG12CD34"):
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": tab_id, "feature": "auth", "intent": "manage",
         "params": {"database": database}, "title": f"Manage {database}"},
    ]}))


def _csrf_headers(client):
    client.get("/")
    return {"Datastar-Request": "true",
            "X-CSRF-Token": client.cookies.get("iris_csrf") or ""}


def test_grant_reader_user_returns_403_when_not_db_admin(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed(app, sid)
    headers = _csrf_headers(client)
    r = client.post(
        "/feature/auth/MG12CD34/members/reader/user",
        params={"database": "marketing", "username": "bob"},
        headers=headers,
    )
    assert r.status_code == 403


def test_grant_reader_user_returns_422_on_empty_username(app, capability_session, monkeypatch):
    async def noop(self, username): pass
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.grant_reader", noop,
    )
    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    headers = _csrf_headers(client)
    r = client.post(
        "/feature/auth/MG12CD34/members/reader/user",
        params={"database": "marketing", "username": ""},
        headers=headers,
    )
    assert r.status_code == 422


def test_grant_reader_user_calls_db_session_method(app, capability_session, monkeypatch):
    calls = []
    async def fake_grant(self, username):
        calls.append(("grant_reader", username))
    async def fake_list(self):
        return []
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.grant_reader", fake_grant,
    )
    monkeypatch.setattr(
        "iris.features.authorization.service.list_members",
        lambda s: fake_list(s),  # noqa: ARG005
    )

    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    headers = _csrf_headers(client)
    r = client.post(
        "/feature/auth/MG12CD34/members/reader/user",
        params={"database": "marketing", "username": "bob"},
        headers=headers,
    )
    assert r.status_code == 200
    assert calls == [("grant_reader", "bob")]
    assert "datastar-patch-elements" in r.text
    assert "MG12CD34-members" in r.text


def test_revoke_admin_group_calls_remove_admin_group(
    app, capability_session, monkeypatch
):
    calls = []
    async def fake_revoke(self, group):
        calls.append(("remove_admin_group", group))
    async def fake_list(self):
        return []
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.remove_admin_group", fake_revoke,
    )
    monkeypatch.setattr(
        "iris.features.authorization.service.list_members",
        lambda s: fake_list(s),  # noqa: ARG005
    )

    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    headers = _csrf_headers(client)
    r = client.delete(
        "/feature/auth/MG12CD34/members/admin/group",
        params={"database": "marketing", "group": "data-team"},
        headers=headers,
    )
    assert r.status_code == 200
    assert calls == [("remove_admin_group", "data-team")]


def test_grant_routes_csrf_required(app, capability_session):
    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    client.get("/")
    r = client.post(
        "/feature/auth/MG12CD34/members/reader/user",
        params={"database": "marketing", "username": "bob"},
        headers={"Datastar-Request": "true"},
    )
    assert r.status_code == 400
