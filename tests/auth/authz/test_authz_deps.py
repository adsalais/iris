import asyncio
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from iris.auth import Session
from iris.auth.authz.deps import require_role
from iris.auth.authz.store import RoleMappingStore
from iris.auth.deps import set_session_store, set_settings
from iris.auth.exceptions import install_exception_handlers
from iris.auth.identity import User
from iris.auth.session import SessionView
from iris.auth.sessions import SessionStore


class _NoSeedSettings:
    bootstrap_role = "admin"
    bootstrap_user = None


def _seed_authz_fixture(store: RoleMappingStore) -> None:
    """Mirror the previous YAML fixture:
       reader: groups=[], users=[]
       writer: groups=["editors"], users=["bob"], includes=["reader"]
       admin:  groups=["admins"], users=["Alice"], includes=["writer"]
    """
    store.bootstrap(_NoSeedSettings())  # create schema, no seeding
    asyncio.run(store.add_role("reader"))
    asyncio.run(store.add_role("writer"))
    asyncio.run(store.add_role("admin"))
    asyncio.run(store.add_include("writer", "reader"))
    asyncio.run(store.add_include("admin", "writer"))
    asyncio.run(store.add_group_to_role("writer", "editors"))
    asyncio.run(store.add_user_to_role("writer", "bob"))
    asyncio.run(store.add_group_to_role("admin", "admins"))
    asyncio.run(store.add_user_to_role("admin", "Alice"))


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

    @app.get("/reader-only")
    async def reader_only(session: SessionView = Depends(require_role("reader"))):
        return {"subject": session.user.subject}

    @app.get("/admin-only")
    async def admin_only(session: SessionView = Depends(require_role("admin"))):
        return {"subject": session.user.subject}

    @app.get("/needs-undefined-role")
    async def needs_undefined(session: SessionView = Depends(require_role("super_admin"))):
        return {"subject": session.user.subject}

    @app.get("/my-roles")
    async def my_roles(session: Session):
        return {"roles": sorted(session.roles)}

    return app, sess_store, authz_store


def _seed(store: SessionStore, *, username: str, groups: tuple[str, ...]) -> str:
    user = User(
        subject=f"mock:{username}",
        username=username,
        display_name=username.title(),
        groups=groups,
    )
    session = asyncio.run(store.create(user))
    return session.id


def _close(sess_store: SessionStore, authz_store: RoleMappingStore) -> None:
    asyncio.run(sess_store.close())
    asyncio.run(authz_store.close())


def test_admin_via_group_reaches_reader_only_route(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, username="charlie", groups=("admins",))
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/reader-only", headers={"accept": "application/json"})
        assert r.status_code == 200
    finally:
        _close(sess_store, authz_store)


def test_writer_via_username_reaches_reader_only_route(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, username="bob", groups=())
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/reader-only", headers={"accept": "application/json"})
        assert r.status_code == 200
    finally:
        _close(sess_store, authz_store)


def test_username_match_is_case_insensitive(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, username="alice", groups=())
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/admin-only", headers={"accept": "application/json"})
        assert r.status_code == 200
    finally:
        _close(sess_store, authz_store)


def test_user_with_no_matching_role_is_forbidden(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, username="dave", groups=("strangers",))
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/reader-only", headers={"accept": "application/json"})
        assert r.status_code == 403
    finally:
        _close(sess_store, authz_store)


def test_unauthenticated_user_gets_401(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        r = TestClient(app).get(
            "/reader-only", headers={"accept": "application/json"}
        )
        assert r.status_code == 401
    finally:
        _close(sess_store, authz_store)


def test_route_requiring_undefined_role_returns_500(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, username="alice", groups=("admins",))
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/needs-undefined-role", headers={"accept": "application/json"})
        assert r.status_code == 500
        assert "super_admin" not in r.text
    finally:
        _close(sess_store, authz_store)


def test_session_roles_returns_full_effective_set_for_admin(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, username="charlie", groups=("admins",))
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/my-roles", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"roles": ["admin", "reader", "writer"]}
    finally:
        _close(sess_store, authz_store)


def test_session_roles_returns_empty_set_for_user_with_no_match(tmp_path):
    app, sess_store, authz_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store, username="nobody", groups=())
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/my-roles", headers={"accept": "application/json"})
        assert r.json() == {"roles": []}
    finally:
        _close(sess_store, authz_store)
