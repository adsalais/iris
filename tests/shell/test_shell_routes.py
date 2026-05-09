from __future__ import annotations

import asyncio


def _bootstrap_csrf(client) -> None:
    """GET / sets the CSRF cookie."""
    client.get("/")


def _datastar_headers(client) -> dict[str, str]:
    return {
        "Datastar-Request": "true",
        "X-CSRF-Token": client.cookies.get("iris_csrf") or "",
    }


def test_post_tabs_unknown_intent_returns_400(authed_client):
    _bootstrap_csrf(authed_client)
    r = authed_client.post(
        "/api/tabs",
        params={"feature": "ghost", "intent": "x", "params": "{}"},
        headers=_datastar_headers(authed_client),
    )
    assert r.status_code == 400


def test_post_tabs_without_csrf_returns_400(authed_client):
    _bootstrap_csrf(authed_client)
    r = authed_client.post(
        "/api/tabs",
        params={"feature": "auth", "intent": "my_access", "params": "{}"},
        headers={"Datastar-Request": "true"},  # no X-CSRF-Token
    )
    assert r.status_code == 400


def test_delete_tab_returns_204_when_absent(authed_client):
    _bootstrap_csrf(authed_client)
    r = authed_client.delete(
        "/api/tabs/UNKNOWN1",
        headers=_datastar_headers(authed_client),
    )
    assert r.status_code == 204


def test_delete_tab_removes_from_session_data(app, capability_session, parse_sse):
    client, sid = asyncio.run(capability_session())
    store = app.state.auth_session_store
    asyncio.run(store.update_data(sid, {"tabs": [
        {"id": "AB12CD34", "feature": "auth", "intent": "my_access",
         "params": {}, "title": "My access"},
    ]}))
    _bootstrap_csrf(client)
    r = client.delete(
        "/api/tabs/AB12CD34",
        headers={"Datastar-Request": "true",
                 "X-CSRF-Token": client.cookies.get("iris_csrf") or ""},
    )
    assert r.status_code == 200
    events = parse_sse(r.text)
    targets = " ".join(e.data for e in events)
    assert "tab-button-AB12CD34" in targets
    assert "tab-content-AB12CD34" in targets
    refreshed = asyncio.run(store.get_and_refresh(sid))
    assert refreshed is not None
    assert refreshed.data.get("tabs", []) == []


def test_render_route_returns_404_for_unknown_tab(authed_client):
    r = authed_client.get("/feature/auth/UNKNOWN1/render")
    assert r.status_code == 404


def test_render_route_returns_404_for_unknown_feature(app, capability_session):
    client, sid = asyncio.run(capability_session())
    store = app.state.auth_session_store
    asyncio.run(store.update_data(sid, {"tabs": [
        {"id": "AB12CD34", "feature": "ghost", "intent": "x",
         "params": {}, "title": "T"},
    ]}))
    r = client.get("/feature/ghost/AB12CD34/render")
    assert r.status_code == 404
