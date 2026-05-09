"""End-to-end: nav has the entry, tab opens, panel renders."""
from __future__ import annotations

import asyncio
import json


def test_home_includes_authorization_my_access_in_nav(authed_client):
    r = authed_client.get("/")
    assert r.status_code == 200
    assert "Authorization" in r.text
    assert "My access" in r.text


def test_open_my_access_tab_then_render(authed_client, parse_sse):
    home = authed_client.get("/")
    assert home.status_code == 200
    csrf = authed_client.cookies.get("iris_csrf")
    assert csrf

    open_r = authed_client.post(
        "/api/tabs",
        params={"feature": "auth", "intent": "my_access", "params": "{}"},
        headers={"Datastar-Request": "true", "X-CSRF-Token": csrf},
    )
    assert open_r.status_code == 200
    events = parse_sse(open_r.text)
    event_names = [e.event for e in events]
    assert event_names.count("datastar-patch-elements") == 2
    assert "datastar-patch-signals" in event_names

    sig_event = next(e for e in events if e.event == "datastar-patch-signals")
    sig_payload = sig_event.data
    if sig_payload.startswith("signals "):
        sig_payload = sig_payload[len("signals "):]
    sig = json.loads(sig_payload)
    assert "tabs" in sig and len(sig["tabs"]) == 1
    tab_id = next(iter(sig["tabs"]))
    assert sig["active"] == tab_id

    render_r = authed_client.get(f"/feature/auth/{tab_id}/render")
    assert render_r.status_code == 200
    assert "My access" in render_r.text


def test_my_access_intent_works_for_minimum_cap_session(app, capability_session):
    """my_access has required=lambda c: True, so any logged-in user passes."""
    client, _sid = asyncio.run(capability_session())
    home = client.get("/")
    assert home.status_code == 200
    csrf = client.cookies.get("iris_csrf")
    open_r = client.post(
        "/api/tabs",
        params={"feature": "auth", "intent": "my_access", "params": "{}"},
        headers={"Datastar-Request": "true", "X-CSRF-Token": csrf},
    )
    assert open_r.status_code == 200
