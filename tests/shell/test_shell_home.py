from __future__ import annotations

import asyncio


def test_get_home_renders_shell_layout(authed_client):
    r = authed_client.get("/")
    assert r.status_code == 200
    body = r.text
    assert '<aside class="iris-left-panel">' in body
    assert '<main class="iris-right-panel">' in body
    assert 'id="tab-strip"' in body
    assert 'id="tab-content"' in body


def test_home_seeds_tabs_signal_from_session_data(app, capability_session):
    """If session.data['tabs'] has entries, they appear in the rendered tab strip."""
    client, sid = asyncio.run(capability_session())
    store = app.state.auth_session_store
    asyncio.run(store.update_data(sid, {"tabs": [
        {"id": "AB12CD34", "feature": "auth", "intent": "my_access",
         "params": {}, "title": "My access"},
    ]}))
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="tab-button-AB12CD34"' in r.text
    assert 'id="tab-content-AB12CD34"' in r.text
    assert 'My access' in r.text


def test_home_includes_account_popover(authed_client):
    r = authed_client.get("/")
    assert "iris-account-popover" in r.text
    assert "Sign out" in r.text
