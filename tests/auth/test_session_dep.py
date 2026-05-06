import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from iris.auth import Session, OptionalSession
from iris.auth.authz.store import RoleMappingStore
from iris.auth.deps import set_session_store, set_settings
from iris.auth.exceptions import install_exception_handlers
from iris.auth.identity import User
from iris.auth.sessions import SessionStore


class _NoSeedSettings:
    bootstrap_role = "admin"
    bootstrap_user = None


def _seed_authz_fixture(store: RoleMappingStore) -> None:
    """reader -> writer -> admin closure; admin gated on the 'admins' group."""
    store.bootstrap(_NoSeedSettings())  # create schema, no seeding
    asyncio.run(store.add_role("reader"))
    asyncio.run(store.add_role("writer"))
    asyncio.run(store.add_role("admin"))
    asyncio.run(store.add_include("writer", "reader"))
    asyncio.run(store.add_include("admin", "writer"))
    asyncio.run(store.add_group_to_role("admin", "admins"))


def _build_app(tmp_path: Path) -> tuple[FastAPI, SessionStore, RoleMappingStore]:
    app = FastAPI()
    db_path = tmp_path / "auth.db"
    sess_store = SessionStore(
        path=str(db_path), ttl_seconds=60, absolute_ttl_seconds=3600
    )
    set_session_store(app, sess_store)
    set_settings(app, cookie_name="iris_session")
    install_exception_handlers(app, cookie_name="iris_session")

    authz_store = RoleMappingStore(path=str(db_path))
    _seed_authz_fixture(authz_store)
    app.state.authz_store = authz_store

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

    return app, sess_store, authz_store


def _seed(store: SessionStore, **overrides) -> str:
    user = User(
        subject=overrides.get("subject", "alice"),
        username=overrides.get("username", overrides.get("subject", "alice")),
        display_name=overrides.get("display_name", "Alice"),
        groups=overrides.get("groups", ("admins",)),
    )
    session = asyncio.run(store.create(user))
    return session.id


def _close(sess_store: SessionStore, authz_store: RoleMappingStore) -> None:
    asyncio.run(sess_store.close())
    asyncio.run(authz_store.close())


def test_no_credentials_returns_401(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        r = TestClient(app).get("/me", headers={"accept": "application/json"})
        assert r.status_code == 401
    finally:
        _close(sess_store, authz_store)


def test_cookie_credential_resolves_session(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/me", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"subject": "alice"}
    finally:
        _close(sess_store, authz_store)


def test_bearer_credential_resolves_session(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)
        r = TestClient(app).get(
            "/me",
            headers={"accept": "application/json", "authorization": f"Bearer {sid}"},
        )
        assert r.status_code == 200
        assert r.json() == {"subject": "alice"}
    finally:
        _close(sess_store, authz_store)


def test_optional_session_returns_none_when_unauthenticated(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        r = TestClient(app).get("/optional", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"present": False}
    finally:
        _close(sess_store, authz_store)


def test_optional_session_returns_session_when_authenticated(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/optional", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"present": True}
    finally:
        _close(sess_store, authz_store)


def test_session_data_round_trip(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        assert c.get("/data").json() == {"counter": 0}
        assert c.post("/data").json() == {"counter": 1}
        assert c.post("/data").json() == {"counter": 2}
        assert c.get("/data").json() == {"counter": 2}
    finally:
        _close(sess_store, authz_store)


def test_session_data_isolated_between_sessions(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid_a = _seed(sess_store, subject="alice")
        sid_b = _seed(sess_store, subject="bob")
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
        _close(sess_store, authz_store)


def test_session_data_requires_auth(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        r = TestClient(app).get("/data", headers={"accept": "application/json"})
        assert r.status_code == 401
    finally:
        _close(sess_store, authz_store)


def test_session_exposes_id_user_and_data(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)
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
        _close(sess_store, authz_store)


def test_session_roles_includes_closure(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, subject="charlie", groups=("admins",))
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/whoami-full", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json()["roles"] == ["admin", "reader", "writer"]
    finally:
        _close(sess_store, authz_store)


def test_session_roles_empty_for_user_without_match(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, subject="dave", groups=("strangers",))
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/whoami-full", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json()["roles"] == []
    finally:
        _close(sess_store, authz_store)
