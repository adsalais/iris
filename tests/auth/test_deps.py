import asyncio

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from iris.auth.authz.deps import require_role
from iris.auth.authz.loader import RoleMappingLoader
from iris.auth.deps import (
    CurrentSession,
    CurrentUser,
    OptionalCurrentUser,
    SessionData,
    set_session_store,
    set_settings,
)
from iris.auth.exceptions import install_exception_handlers
from iris.auth.identity import User
from iris.auth.sessions import InMemorySessionStore


def _build_app(tmp_path) -> tuple[FastAPI, InMemorySessionStore]:
    yaml_path = tmp_path / "authz.yaml"
    yaml_path.write_text(
        "roles:\n"
        "  admin:\n"
        "    groups: [\"admins\"]\n"
    )
    app = FastAPI()
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600)
    set_session_store(app, store)
    set_settings(app, cookie_name="iris_session")
    install_exception_handlers(app, cookie_name="iris_session")
    app.state.authz_loader = RoleMappingLoader(yaml_path)

    @app.get("/me")
    async def me(user: CurrentUser):
        return {"subject": user.subject}

    @app.get("/optional")
    async def optional(user: OptionalCurrentUser):
        return {"present": user is not None}

    @app.get("/admin")
    async def admin(user: User = Depends(require_role("admin"))):
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


def test_no_credentials_returns_401_for_api(tmp_path):
    app, _ = _build_app(tmp_path)
    r = TestClient(app).get("/me", headers={"accept": "application/json"})
    assert r.status_code == 401


def test_cookie_credential_resolves_user(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store)
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/me", headers={"accept": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"subject": "alice"}


def test_bearer_credential_resolves_user(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store)
    r = TestClient(app).get(
        "/me",
        headers={"accept": "application/json", "authorization": f"Bearer {sid}"},
    )
    assert r.status_code == 200
    assert r.json() == {"subject": "alice"}


def test_optional_returns_none_when_unauthenticated(tmp_path):
    app, _ = _build_app(tmp_path)
    r = TestClient(app).get("/optional", headers={"accept": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"present": False}


def test_optional_returns_user_when_authenticated(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store)
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/optional", headers={"accept": "application/json"})
    assert r.json() == {"present": True}


def test_require_role_admits_member(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, groups=("admins", "users"))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/admin", headers={"accept": "application/json"})
    assert r.status_code == 200


def test_require_role_rejects_non_member(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, groups=("users",))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/admin", headers={"accept": "application/json"})
    assert r.status_code == 403


def test_session_data_round_trip(tmp_path):
    """Mutations to SessionData persist across requests with the same session id."""
    app, store = _build_app(tmp_path)
    sid = _seed(store)
    c = TestClient(app)
    c.cookies.set("iris_session", sid)

    assert c.get("/data").json() == {"counter": 0}
    assert c.post("/data").json() == {"counter": 1}
    assert c.post("/data").json() == {"counter": 2}
    assert c.get("/data").json() == {"counter": 2}


def test_session_data_isolated_between_sessions(tmp_path):
    """Two sessions don't see each other's data."""
    app, store = _build_app(tmp_path)
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


def test_session_data_requires_auth(tmp_path):
    """Without a session cookie or bearer, /data 401s like CurrentUser would."""
    app, _ = _build_app(tmp_path)
    r = TestClient(app).get("/data", headers={"accept": "application/json"})
    assert r.status_code == 401


def test_current_session_exposes_id_user_and_data(tmp_path):
    """CurrentSession returns the full UserSession; .id, .user, .data are reachable."""
    app, store = _build_app(tmp_path)
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
