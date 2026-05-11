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


def test_home_renders_bottom_user_element(authed_client):
    """The user element pins to the bottom of the left panel and carries the
    display name (visible when expanded, hidden via CSS when collapsed)."""
    r = authed_client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "iris-bottom-user" in body
    assert "iris-user-btn" in body
    assert "iris-user-name" in body
    assert "Alice" in body
    aside_start = body.index('class="iris-left-panel"')
    aside_end = body.index("</aside>", aside_start)
    aside = body[aside_start:aside_end]
    assert aside.index("iris-nav-wrap") < aside.index("iris-bottom-user")


def test_home_top_buttons_no_longer_include_account(authed_client):
    """The account toggle moved to the bottom user element; the top row keeps
    only the nav toggle and the settings placeholder."""
    r = authed_client.get("/")
    body = r.text
    top_start = body.index("iris-top-buttons")
    top_end = body.index("</div>", top_start)
    top = body[top_start:top_end]
    assert "iris-toggle-nav" in top
    assert "Settings" in top
    assert "$account_open" not in top
