from __future__ import annotations

import asyncio


def _seed(app, sid: str, tab_id="AC12CD34"):
    asyncio.run(
        app.state.auth_session_store.update_data(
            sid,
            {
                "tabs": [
                    {
                        "id": tab_id,
                        "feature": "authorization",
                        "intent": "admin_console",
                        "params": {},
                        "title": "Org admin console",
                    },
                ]
            },
        )
    )


def _csrf(client):
    client.get("/")
    return {
        "Datastar-Request": "true",
        "X-CSRF-Token": client.cookies.get("iris_csrf") or "",
    }


def test_admin_console_intent_registered(app):
    spec = app.state.intent_dispatcher.resolve("authorization", "admin_console")
    assert spec.title({}) == "Org admin console"


def test_admin_console_required_is_admin_only(app):
    from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities

    spec = app.state.intent_dispatcher.resolve("authorization", "admin_console")
    assert spec.required(EMPTY_CAPABILITIES) is False
    assert (
        spec.required(
            Capabilities(
                is_admin=False,
                can_create_database=True,
                db_admin=frozenset({"x"}),
                db_writer=frozenset(),
                db_reader=frozenset(),
            )
        )
        is False
    )
    assert (
        spec.required(
            Capabilities(
                is_admin=True,
                can_create_database=False,
                db_admin=frozenset(),
                db_writer=frozenset(),
                db_reader=frozenset(),
            )
        )
        is True
    )


def test_org_admin_nav_has_four_sub_entries(app):
    contribs = app.state.contributions
    g = next(g for g in contribs.nav.groups if g.label == "Org admin")
    labels = [e.label for e in g.entries]
    assert labels == ["All users", "All databases", "Row policies", "Audit"]


def test_render_admin_console_shows_subtabs(app, capability_session):
    client, sid = asyncio.run(capability_session(is_admin=True))
    _seed(app, sid)
    r = client.get("/feature/authorization/AC12CD34/admin_console")
    assert r.status_code == 200
    # Sub-tab buttons: text appears between <button> tags (with whitespace)
    assert "Users" in r.text
    assert "Databases" in r.text
    assert "Row policies" in r.text
    assert "Audit" in r.text
    assert "iris-subtabs" in r.text


def test_render_admin_console_403_when_not_admin(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed(app, sid)
    r = client.get("/feature/authorization/AC12CD34/admin_console")
    assert r.status_code == 403


def test_subtab_get_users_403_when_not_admin(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed(app, sid)
    r = client.get("/feature/authorization/AC12CD34/admin/users")
    assert r.status_code == 403


def test_subtab_get_users_returns_users_table(app, capability_session, monkeypatch):
    async def fake_users(self):  # noqa: ARG001
        return [{"name": "alice", "roles": ["data-team"]}]

    monkeypatch.setattr(
        "iris.auth.views.AdminSession.list_users",
        fake_users,
    )
    client, sid = asyncio.run(capability_session(is_admin=True))
    _seed(app, sid)
    r = client.get("/feature/authorization/AC12CD34/admin/users")
    assert r.status_code == 200
    assert "alice" in r.text


def test_subtab_get_databases_returns_databases_table(
    app, capability_session, monkeypatch
):
    async def fake_dbs(self):  # noqa: ARG001
        return [
            {
                "name": "marketing",
                "admin_count": 1,
                "writer_count": 0,
                "reader_count": 3,
            }
        ]

    monkeypatch.setattr(
        "iris.auth.views.AdminSession.list_databases",
        fake_dbs,
    )
    client, sid = asyncio.run(capability_session(is_admin=True))
    _seed(app, sid)
    r = client.get("/feature/authorization/AC12CD34/admin/databases")
    assert r.status_code == 200
    assert "marketing" in r.text


def test_subtab_get_policies_returns_policies_table(
    app, capability_session, monkeypatch
):
    async def fake_pol(self):
        return [
            {
                "database": "marketing",
                "table": "events",
                "name": "p1",
                "select_filter": "user_id = $alice",
            }
        ]

    monkeypatch.setattr(
        "iris.auth.views.AdminSession.list_all_row_policies",
        fake_pol,
    )
    client, sid = asyncio.run(capability_session(is_admin=True))
    _seed(app, sid)
    r = client.get("/feature/authorization/AC12CD34/admin/policies")
    assert r.status_code == 200
    assert "marketing" in r.text and "events" in r.text


def test_reprovision_user_403_when_not_admin(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed(app, sid)
    r = client.post(
        "/feature/authorization/AC12CD34/admin/users/alice/reprovision",
        headers=_csrf(client),
    )
    assert r.status_code == 403


def test_reprovision_user_calls_admin_session_method(
    app,
    capability_session,
    monkeypatch,
):
    calls = []

    async def fake_reprov(self, *, username, groups):  # noqa: ARG001
        calls.append(("reprov", username, list(groups)))

    monkeypatch.setattr(
        "iris.auth.views.AdminSession.reprovision_user",
        fake_reprov,
    )

    async def fake_users(self):  # noqa: ARG001
        return [{"name": "alice", "roles": []}]

    monkeypatch.setattr(
        "iris.auth.views.AdminSession.list_users",
        fake_users,
    )
    client, sid = asyncio.run(capability_session(is_admin=True))
    _seed(app, sid)
    r = client.post(
        "/feature/authorization/AC12CD34/admin/users/alice/reprovision",
        headers=_csrf(client),
    )
    assert r.status_code == 200
    assert calls == [("reprov", "alice", [])]


def test_subtab_get_audit_returns_grants_table(app, capability_session, monkeypatch):
    async def fake_audit(self):  # noqa: ARG001
        return [
            {
                "user_name": "bob",
                "role_name": None,
                "access_type": "INSERT",
                "database": "events",
            }
        ]

    monkeypatch.setattr(
        "iris.auth.views.AdminSession.list_all_grants",
        fake_audit,
    )
    client, sid = asyncio.run(capability_session(is_admin=True))
    _seed(app, sid)
    r = client.get("/feature/authorization/AC12CD34/admin/audit")
    assert r.status_code == 200
    assert "bob" in r.text and "INSERT" in r.text
