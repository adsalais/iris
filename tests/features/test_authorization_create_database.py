from __future__ import annotations

import asyncio


def _seed(app, sid: str, tab_id="CR12CD34"):
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": tab_id, "feature": "auth", "intent": "create_database",
         "params": {}, "title": "Create database"},
    ]}))


def _csrf(client):
    client.get("/")
    return {"Datastar-Request": "true",
            "X-CSRF-Token": client.cookies.get("iris_csrf") or ""}


def test_create_database_intent_registered(app):
    spec = app.state.intent_dispatcher.resolve("auth", "create_database")
    assert spec.title({}) == "Create database"


def test_create_database_required_predicate(app):
    from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
    spec = app.state.intent_dispatcher.resolve("auth", "create_database")
    assert spec.required(EMPTY_CAPABILITIES) is False
    assert spec.required(Capabilities(
        is_admin=False, can_create_database=True,
        db_admin=frozenset(), db_writer=frozenset(), db_reader=frozenset(),
    )) is True
    assert spec.required(Capabilities(
        is_admin=True, can_create_database=False,
        db_admin=frozenset(), db_writer=frozenset(), db_reader=frozenset(),
    )) is True


def test_create_database_nav_entry_visible_when_can_create(app):
    from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
    contribs = app.state.contributions
    auth_group = next(g for g in contribs.nav.groups if g.label == "Authorization")
    create_entry = next(
        (e for e in auth_group.entries if e.label == "Create database"),
        None,
    )
    assert create_entry is not None
    assert create_entry.visible(EMPTY_CAPABILITIES) is False
    caps = Capabilities(
        is_admin=False, can_create_database=True,
        db_admin=frozenset(), db_writer=frozenset(), db_reader=frozenset(),
    )
    assert create_entry.visible(caps) is True


def test_render_create_database_shows_form(app, capability_session):
    client, sid = asyncio.run(capability_session(can_create_database=True))
    _seed(app, sid)
    r = client.get("/feature/auth/CR12CD34/render")
    assert r.status_code == 200
    assert "Create database" in r.text
    assert "data-bind=\"tabs.CR12CD34.new_db_name\"" in r.text


def test_submit_create_database_403_when_not_creator(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed(app, sid)
    r = client.post(
        "/feature/auth/CR12CD34/submit",
        params={"name": "marketing"},
        headers=_csrf(client),
    )
    assert r.status_code == 403


def test_submit_create_database_calls_method_and_retargets_tab(
    app, capability_session, monkeypatch,
):
    calls = []
    async def fake_create(self, name):  # noqa: ARG001
        calls.append(("create", name))
    monkeypatch.setattr(
        "iris.auth.views.DatabaseCreatorSession.create_database", fake_create,
    )
    client, sid = asyncio.run(capability_session(can_create_database=True))
    _seed(app, sid)
    r = client.post(
        "/feature/auth/CR12CD34/submit",
        params={"name": "shiny_new_db"},
        headers=_csrf(client),
    )
    assert r.status_code == 200
    assert calls == [("create", "shiny_new_db")]
    refreshed = asyncio.run(app.state.auth_session_store.get_and_refresh(sid))
    assert refreshed is not None
    tabs = refreshed.data["tabs"]
    assert tabs[0]["intent"] == "manage"
    assert tabs[0]["params"]["database"] == "shiny_new_db"
    assert tabs[0]["title"] == "Manage shiny_new_db"
    assert "tab-button-CR12CD34" in r.text
    assert "tab-content-CR12CD34" in r.text


def test_submit_create_database_renders_inline_error_on_invalid_name(
    app, capability_session, monkeypatch,
):
    async def fake_create(self, name):  # noqa: ARG001
        from iris.clickhouse.identifiers import InvalidIdentifierError
        msg = "invalid"
        raise InvalidIdentifierError(msg)
    monkeypatch.setattr(
        "iris.auth.views.DatabaseCreatorSession.create_database", fake_create,
    )
    client, sid = asyncio.run(capability_session(can_create_database=True))
    _seed(app, sid)
    r = client.post(
        "/feature/auth/CR12CD34/submit",
        params={"name": "bad-name"},
        headers=_csrf(client),
    )
    assert r.status_code == 200
    assert "invalid" in r.text.lower()
