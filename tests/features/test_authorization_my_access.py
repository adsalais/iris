"""my_access render adapts to capabilities."""
from __future__ import annotations

import asyncio


def _seed_my_access_tab(app, sid: str, tab_id: str = "AB12CD34") -> None:
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": tab_id, "feature": "auth", "intent": "my_access",
         "params": {}, "title": "My access"},
    ]}))


def test_my_access_shows_identity(app, capability_session):
    client, sid = asyncio.run(capability_session(
        username="alice", display_name="Alice",
        groups=("data-team", "dev"),
    ))
    _seed_my_access_tab(app, sid)
    r = client.get("/feature/auth/AB12CD34/render")
    assert r.status_code == 200
    assert "alice" in r.text or "Alice" in r.text
    assert "data-team" in r.text


def test_my_access_omits_reader_section_when_empty(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed_my_access_tab(app, sid)
    r = client.get("/feature/auth/AB12CD34/render")
    assert r.status_code == 200
    assert "Databases you can read" not in r.text


def test_my_access_lists_reader_databases(app, capability_session):
    client, sid = asyncio.run(capability_session(db_reader={"marketing", "analytics"}))
    _seed_my_access_tab(app, sid)
    r = client.get("/feature/auth/AB12CD34/render")
    assert r.status_code == 200
    assert "Databases you can read" in r.text
    assert "marketing" in r.text
    assert "analytics" in r.text


def test_my_access_lists_writer_and_admin_databases(app, capability_session):
    client, sid = asyncio.run(capability_session(
        db_writer={"events"}, db_admin={"sales"},
    ))
    _seed_my_access_tab(app, sid)
    r = client.get("/feature/auth/AB12CD34/render")
    assert "Databases you can write to" in r.text and "events" in r.text
    assert "Databases you administer" in r.text and "sales" in r.text


def test_my_access_shows_create_when_can_create_database(app, capability_session):
    client_no, sid_no = asyncio.run(capability_session())
    _seed_my_access_tab(app, sid_no)
    r = client_no.get("/feature/auth/AB12CD34/render")
    assert "Create new database" not in r.text

    client_yes, sid_yes = asyncio.run(capability_session(can_create_database=True))
    _seed_my_access_tab(app, sid_yes)
    r2 = client_yes.get("/feature/auth/AB12CD34/render")
    assert "Create new database" in r2.text


def test_my_access_shows_admin_console_when_is_admin(app, capability_session):
    client_no, sid_no = asyncio.run(capability_session())
    _seed_my_access_tab(app, sid_no)
    r = client_no.get("/feature/auth/AB12CD34/render")
    assert "Open admin console" not in r.text

    client_yes, sid_yes = asyncio.run(capability_session(is_admin=True))
    _seed_my_access_tab(app, sid_yes)
    r2 = client_yes.get("/feature/auth/AB12CD34/render")
    assert "Open admin console" in r2.text


def test_my_access_render_route_returns_404_for_wrong_feature(app, capability_session):
    client, sid = asyncio.run(capability_session())
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": "AB12CD34", "feature": "ghost", "intent": "x",
         "params": {}, "title": "T"},
    ]}))
    r = client.get("/feature/auth/AB12CD34/render")
    assert r.status_code == 404


def test_my_access_render_route_returns_404_for_unknown_intent(app, capability_session):
    client, sid = asyncio.run(capability_session())
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": "AB12CD34", "feature": "auth", "intent": "nope",
         "params": {}, "title": "T"},
    ]}))
    r = client.get("/feature/auth/AB12CD34/render")
    assert r.status_code == 404
