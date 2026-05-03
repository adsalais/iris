import asyncio

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from iris.auth.deps import (
    CurrentSession,
    CurrentUser,
    OptionalCurrentUser,
    SessionData,
    require_group,
    set_session_store,
    set_settings,
)
from iris.auth.exceptions import install_exception_handlers
from iris.auth.identity import User
from iris.auth.sessions import InMemorySessionStore


def _build_app() -> tuple[FastAPI, InMemorySessionStore]:
    app = FastAPI()
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600)
    set_session_store(app, store)
    set_settings(app, cookie_name="iris_session")
    install_exception_handlers(app, cookie_name="iris_session")

    @app.get("/me")
    async def me(user: CurrentUser):
        return {"subject": user.subject}

    @app.get("/optional")
    async def optional(user: OptionalCurrentUser):
        return {"present": user is not None}

    @app.get("/admin")
    async def admin(user: User = Depends(require_group("admins"))):
        return {"subject": user.subject}

    @app.get("/data")
    async def read_data(data: SessionData):
        return {"counter": data.get("counter", 0)}

    @app.post("/data")
    async def bump_data(data: SessionData):
        data["counter"] = data.get("counter", 0) + 1
        return {"counter": data["counter"]}

    @app.get("/whoami-full")
    async def whoami_full(session: CurrentSession):
        return {
            "id": session.id,
            "subject": session.user.subject,
            "data_keys": sorted(session.data.keys()),
        }

    return app, store


def _seed(store: InMemorySessionStore, **overrides) -> str:
    user = User(
        subject=overrides.get("subject", "alice"),
        username=overrides.get("username", overrides.get("subject", "alice")),
        display_name=overrides.get("display_name", "Alice"),
        groups=overrides.get("groups", ("admins",)),
    )
    session = asyncio.run(store.create(user))
    return session.id


def test_no_credentials_returns_401_for_api():
    app, _ = _build_app()
    r = TestClient(app).get("/me", headers={"accept": "application/json"})
    assert r.status_code == 401


def test_cookie_credential_resolves_user():
    app, store = _build_app()
    sid = _seed(store)
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/me", headers={"accept": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"subject": "alice"}


def test_bearer_credential_resolves_user():
    app, store = _build_app()
    sid = _seed(store)
    r = TestClient(app).get(
        "/me",
        headers={"accept": "application/json", "authorization": f"Bearer {sid}"},
    )
    assert r.status_code == 200
    assert r.json() == {"subject": "alice"}


def test_optional_returns_none_when_unauthenticated():
    app, _ = _build_app()
    r = TestClient(app).get("/optional", headers={"accept": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"present": False}


def test_optional_returns_user_when_authenticated():
    app, store = _build_app()
    sid = _seed(store)
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/optional", headers={"accept": "application/json"})
    assert r.json() == {"present": True}


def test_require_group_admits_member():
    app, store = _build_app()
    sid = _seed(store, groups=("admins", "users"))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/admin", headers={"accept": "application/json"})
    assert r.status_code == 200


def test_require_group_rejects_non_member():
    app, store = _build_app()
    sid = _seed(store, groups=("users",))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/admin", headers={"accept": "application/json"})
    assert r.status_code == 403


def test_session_data_round_trip():
    """Mutations to SessionData persist across requests with the same session id."""
    app, store = _build_app()
    sid = _seed(store)
    c = TestClient(app)
    c.cookies.set("iris_session", sid)

    assert c.get("/data").json() == {"counter": 0}
    assert c.post("/data").json() == {"counter": 1}
    assert c.post("/data").json() == {"counter": 2}
    assert c.get("/data").json() == {"counter": 2}


def test_session_data_isolated_between_sessions():
    """Two sessions don't see each other's data."""
    app, store = _build_app()
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


def test_session_data_requires_auth():
    """Without a session cookie or bearer, /data 401s like CurrentUser would."""
    app, _ = _build_app()
    r = TestClient(app).get("/data", headers={"accept": "application/json"})
    assert r.status_code == 401


def test_current_session_exposes_id_user_and_data():
    """CurrentSession returns the full UserSession; .id, .user, .data are reachable."""
    app, store = _build_app()
    sid = _seed(store)
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    # First write some data so the read shows a non-empty data_keys
    c.post("/data")
    r = c.get("/whoami-full", headers={"accept": "application/json"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == sid
    assert body["subject"] == "alice"
    assert body["data_keys"] == ["counter"]
