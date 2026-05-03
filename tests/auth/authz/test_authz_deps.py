import asyncio
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from iris.auth.authz.deps import CurrentRoles, require_role
from iris.auth.authz.loader import RoleMappingLoader
from iris.auth.deps import set_session_store, set_settings
from iris.auth.exceptions import install_exception_handlers
from iris.auth.identity import User
from iris.auth.sessions import InMemorySessionStore


_FIXTURE_YAML = """
roles:
  reader:
    groups: []
    users: []
  writer:
    groups: ["editors"]
    users: ["bob"]
    includes: ["reader"]
  admin:
    groups: ["admins"]
    users: ["Alice"]
    includes: ["writer"]
"""


def _build_app(tmp_path: Path) -> tuple[FastAPI, InMemorySessionStore]:
    yaml_path = tmp_path / "authz.yaml"
    yaml_path.write_text(_FIXTURE_YAML)

    app = FastAPI()
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600)
    set_session_store(app, store)
    set_settings(app, cookie_name="iris_session")
    install_exception_handlers(app, cookie_name="iris_session")
    app.state.authz_loader = RoleMappingLoader(yaml_path)

    @app.get("/reader-only")
    async def reader_only(user: User = Depends(require_role("reader"))):
        return {"subject": user.subject}

    @app.get("/admin-only")
    async def admin_only(user: User = Depends(require_role("admin"))):
        return {"subject": user.subject}

    @app.get("/needs-undefined-role")
    async def needs_undefined(user: User = Depends(require_role("super_admin"))):
        return {"subject": user.subject}

    @app.get("/my-roles")
    async def my_roles(roles: CurrentRoles):
        return {"roles": sorted(roles)}

    return app, store


def _seed(store: InMemorySessionStore, *, username: str, groups: tuple[str, ...]) -> str:
    user = User(
        subject=f"mock:{username}",
        username=username,
        display_name=username.title(),
        groups=groups,
    )
    session = asyncio.run(store.create(user))
    return session.id


def test_admin_via_group_reaches_reader_only_route(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, username="charlie", groups=("admins",))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/reader-only", headers={"accept": "application/json"})
    assert r.status_code == 200


def test_writer_via_username_reaches_reader_only_route(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, username="bob", groups=())
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/reader-only", headers={"accept": "application/json"})
    assert r.status_code == 200


def test_username_match_is_case_insensitive(tmp_path):
    app, store = _build_app(tmp_path)
    # YAML lists "Alice" with capital A; user logs in as "alice"
    sid = _seed(store, username="alice", groups=())
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/admin-only", headers={"accept": "application/json"})
    assert r.status_code == 200


def test_user_with_no_matching_role_is_forbidden(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, username="dave", groups=("strangers",))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/reader-only", headers={"accept": "application/json"})
    assert r.status_code == 403


def test_unauthenticated_user_gets_401(tmp_path):
    app, _ = _build_app(tmp_path)
    r = TestClient(app).get(
        "/reader-only", headers={"accept": "application/json"}
    )
    assert r.status_code == 401


def test_route_requiring_undefined_role_returns_500(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, username="alice", groups=("admins",))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/needs-undefined-role", headers={"accept": "application/json"})
    assert r.status_code == 500
    assert "super_admin" not in r.text


def test_current_roles_returns_full_effective_set_for_admin(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, username="charlie", groups=("admins",))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/my-roles", headers={"accept": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"roles": ["admin", "reader", "writer"]}


def test_current_roles_returns_empty_set_for_user_with_no_match(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, username="nobody", groups=())
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/my-roles", headers={"accept": "application/json"})
    assert r.json() == {"roles": []}
