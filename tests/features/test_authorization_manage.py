"""Manage intent: nav contribution + capability-aware sections."""
from __future__ import annotations

import asyncio


def _seed_manage_tab(app, sid: str, database: str = "marketing",
                     tab_id: str = "MG12CD34") -> None:
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": tab_id, "feature": "auth", "intent": "manage",
         "params": {"database": database}, "title": f"Manage {database}"},
    ]}))


def test_manage_intent_registered(app):
    spec = app.state.intent_dispatcher.resolve("auth", "manage")
    assert spec.title({"database": "marketing"}) == "Manage marketing"


def test_manage_required_predicate_checks_db_admin(app):
    from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
    spec = app.state.intent_dispatcher.resolve("auth", "manage")
    assert spec.required(EMPTY_CAPABILITIES) is False
    assert spec.required(Capabilities(
        is_admin=False, can_create_database=False,
        db_admin=frozenset({"marketing"}), db_writer=frozenset(),
        db_reader=frozenset(),
    )) is True
    assert spec.required(Capabilities(
        is_admin=True, can_create_database=False,
        db_admin=frozenset(), db_writer=frozenset(), db_reader=frozenset(),
    )) is True


def test_databases_i_admin_nav_entry_visible_when_db_admin_nonempty(app):
    from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
    contribs = app.state.contributions
    auth_group = next(g for g in contribs.nav.groups if g.label == "Authorization")
    db_admin_entry = next(
        (e for e in auth_group.entries if e.label == "Databases I admin"),
        None,
    )
    assert db_admin_entry is not None
    assert db_admin_entry.visible(EMPTY_CAPABILITIES) is False
    caps = Capabilities(
        is_admin=False, can_create_database=False,
        db_admin=frozenset({"x"}), db_writer=frozenset(), db_reader=frozenset(),
    )
    assert db_admin_entry.visible(caps) is True


def test_manage_render_renders_database_name(app, capability_session, monkeypatch):
    async def fake_view(session):
        return {"members": {"admin": [], "reader": [], "writer": []},
                "row_policies": [], "audit": []}
    monkeypatch.setattr(
        "iris.features.authorization.service.manage_view", fake_view,
    )
    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed_manage_tab(app, sid)
    r = client.get("/feature/auth/MG12CD34/render")
    assert r.status_code == 200
    assert "Manage marketing" in r.text


def test_manage_render_returns_403_when_not_db_admin(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed_manage_tab(app, sid)
    r = client.get("/feature/auth/MG12CD34/render")
    assert r.status_code == 403
