import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from iris.auth import Session, OptionalSession
from iris.auth.authz.loader import RoleMappingLoader
from iris.auth.deps import set_session_store, set_settings
from iris.auth.exceptions import install_exception_handlers
from iris.auth.identity import User
from iris.auth.sessions import SessionStore


_FIXTURE_YAML = """
roles:
  reader:
    groups: []
    users: []
  writer:
    groups: ["editors"]
    users: []
    includes: ["reader"]
  admin:
    groups: ["admins"]
    users: []
    includes: ["writer"]
"""


def _build_app(tmp_path: Path) -> tuple[FastAPI, SessionStore]:
    yaml_path = tmp_path / "authz.yaml"
    yaml_path.write_text(_FIXTURE_YAML)

    app = FastAPI()
    db_path = tmp_path / "sessions.db"
    store = SessionStore(
        path=str(db_path), ttl_seconds=60, absolute_ttl_seconds=3600
    )
    set_session_store(app, store)
    set_settings(app, cookie_name="iris_session")
    install_exception_handlers(app, cookie_name="iris_session")
    app.state.authz_loader = RoleMappingLoader(yaml_path)

    @app.get("/me")
    async def me(session: Session):
        return {"subject": session.user.subject}

    @app.get("/optional")
    async def optional(session: OptionalSession):
        return {"present": session is not None}

    @app.get("/whoami-full")
    async def whoami_full(session: Session):
        return {
            "id": session.id,
            "subject": session.user.subject,
            "data_keys": sorted(session.data.keys()),
            "roles": sorted(session.roles),
        }

    @app.get("/data")
    async def read_data(session: Session):
        return {"counter": session.data.get("counter", 0)}

    @app.post("/data")
    async def bump_data(request: Request, session: Session):
        session.data["counter"] = session.data.get("counter", 0) + 1
        await request.app.state.auth_session_store.update_data(
            session.id, session.data
        )
        return {"counter": session.data["counter"]}

    return app, store


def _seed(store: SessionStore, **overrides) -> str:
    user = User(
        subject=overrides.get("subject", "alice"),
        username=overrides.get("username", overrides.get("subject", "alice")),
        display_name=overrides.get("display_name", "Alice"),
        groups=overrides.get("groups", ("admins",)),
    )
    session = asyncio.run(store.create(user))
    return session.id


def test_no_credentials_returns_401(tmp_path):
    app, store = _build_app(tmp_path)
    try:
        r = TestClient(app).get("/me", headers={"accept": "application/json"})
        assert r.status_code == 401
    finally:
        asyncio.run(store.close())


def test_cookie_credential_resolves_session(tmp_path):
    app, store = _build_app(tmp_path)
    try:
        sid = _seed(store)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/me", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"subject": "alice"}
    finally:
        asyncio.run(store.close())


def test_bearer_credential_resolves_session(tmp_path):
    app, store = _build_app(tmp_path)
    try:
        sid = _seed(store)
        r = TestClient(app).get(
            "/me",
            headers={"accept": "application/json", "authorization": f"Bearer {sid}"},
        )
        assert r.status_code == 200
        assert r.json() == {"subject": "alice"}
    finally:
        asyncio.run(store.close())


def test_optional_session_returns_none_when_unauthenticated(tmp_path):
    app, store = _build_app(tmp_path)
    try:
        r = TestClient(app).get("/optional", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"present": False}
    finally:
        asyncio.run(store.close())


def test_optional_session_returns_session_when_authenticated(tmp_path):
    app, store = _build_app(tmp_path)
    try:
        sid = _seed(store)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/optional", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"present": True}
    finally:
        asyncio.run(store.close())


def test_session_data_round_trip(tmp_path):
    """Mutations followed by update_data persist across requests with the same session id."""
    app, store = _build_app(tmp_path)
    try:
        sid = _seed(store)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        assert c.get("/data").json() == {"counter": 0}
        assert c.post("/data").json() == {"counter": 1}
        assert c.post("/data").json() == {"counter": 2}
        assert c.get("/data").json() == {"counter": 2}
    finally:
        asyncio.run(store.close())


def test_session_data_isolated_between_sessions(tmp_path):
    app, store = _build_app(tmp_path)
    try:
        sid_a = _seed(store, subject="alice")
        sid_b = _seed(store, subject="bob")
        ca = TestClient(app)
        ca.cookies.set("iris_session", sid_a)
        cb = TestClient(app)
        cb.cookies.set("iris_session", sid_b)
        ca.post("/data")
        ca.post("/data")
        cb.post("/data")
        assert ca.get("/data").json() == {"counter": 2}
        assert cb.get("/data").json() == {"counter": 1}
    finally:
        asyncio.run(store.close())


def test_session_data_requires_auth(tmp_path):
    app, store = _build_app(tmp_path)
    try:
        r = TestClient(app).get("/data", headers={"accept": "application/json"})
        assert r.status_code == 401
    finally:
        asyncio.run(store.close())


def test_session_exposes_id_user_and_data(tmp_path):
    app, store = _build_app(tmp_path)
    try:
        sid = _seed(store)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        c.post("/data")
        r = c.get("/whoami-full", headers={"accept": "application/json"})
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == sid
        assert body["subject"] == "alice"
        assert body["data_keys"] == ["counter"]
    finally:
        asyncio.run(store.close())


def test_session_roles_includes_closure(tmp_path):
    app, store = _build_app(tmp_path)
    try:
        sid = _seed(store, subject="charlie", groups=("admins",))
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/whoami-full", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json()["roles"] == ["admin", "reader", "writer"]
    finally:
        asyncio.run(store.close())


def test_session_roles_empty_for_user_without_match(tmp_path):
    app, store = _build_app(tmp_path)
    try:
        sid = _seed(store, subject="dave", groups=("strangers",))
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/whoami-full", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json()["roles"] == []
    finally:
        asyncio.run(store.close())
