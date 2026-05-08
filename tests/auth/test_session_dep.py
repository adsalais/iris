import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from iris.auth.deps import (
    Session,
    SessionAdmin,
    SessionDatabaseAdmin,
    SessionDatabaseCreator,
    SessionOptional,
    SessionRead,
    SessionWrite,
    set_session_store,
    set_settings,
)
from iris.auth.exceptions import install_exception_handlers
from iris.auth.identity import (
    AdminSession,
    DatabaseAdminSession,
    DatabaseCreatorSession,
    DatabaseSession,
    User,
)
from iris.auth.session import Rights
from iris.auth.sessions import SessionStore


def _build_app(tmp_path: Path) -> tuple[FastAPI, SessionStore]:
    app = FastAPI()
    db_path = tmp_path / "auth.db"
    sess_store = SessionStore(
        path=str(db_path), ttl_seconds=60, absolute_ttl_seconds=3600
    )
    set_session_store(app, sess_store)
    set_settings(app, cookie_name="iris_session")
    install_exception_handlers(app, cookie_name="iris_session")

    @app.get("/me")
    async def me(session: Session):
        return {"subject": session.user.subject}

    @app.get("/optional")
    async def optional(session: SessionOptional):
        return {"present": session is not None}

    @app.get("/whoami-full")
    async def whoami_full(session: Session):
        return {
            "id": session.id,
            "subject": session.user.subject,
            "data_keys": sorted(session.data.keys()),
            "rights": {
                "is_admin": session.rights.is_admin,
                "can_create_database": session.rights.can_create_database,
                "db_admin": sorted(session.rights.db_admin),
                "db_writer": sorted(session.rights.db_writer),
                "db_reader": sorted(session.rights.db_reader),
            },
        }

    @app.get("/data")
    async def read_data(session: Session):
        return {"counter": session.data.get("counter", 0)}

    @app.post("/data")
    async def bump_data(session: Session):
        session.data["counter"] = session.data.get("counter", 0) + 1
        await session.persist_data()
        return {"counter": session.data["counter"]}

    return app, sess_store


def _seed(store: SessionStore, *, rights: Rights | None = None, **overrides) -> str:
    user = User(
        subject=overrides.get("subject", "alice"),
        username=overrides.get("username", overrides.get("subject", "alice")),
        display_name=overrides.get("display_name", "Alice"),
        groups=overrides.get("groups", ("admins",)),
    )
    session = asyncio.run(store.create(user))
    if rights is not None:
        asyncio.run(store.set_rights(session.id, rights))
    return session.id


def _close(sess_store: SessionStore) -> None:
    asyncio.run(sess_store.close())


def test_no_credentials_returns_401(tmp_path):
    app, sess_store = _build_app(tmp_path)
    try:
        r = TestClient(app).get("/me", headers={"accept": "application/json"})
        assert r.status_code == 401
    finally:
        _close(sess_store)


def test_cookie_credential_resolves_session(tmp_path):
    app, sess_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/me", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"subject": "alice"}
    finally:
        _close(sess_store)


def test_bearer_token_no_longer_accepted(tmp_path):
    """Authorization: Bearer <sid> is no longer a valid credential mode.
    Sessions must come from the iris_session cookie."""
    app, sess_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)
        r = TestClient(app).get(
            "/me",
            headers={"accept": "application/json", "authorization": f"Bearer {sid}"},
        )
        assert r.status_code == 401
    finally:
        _close(sess_store)


def test_optional_session_returns_none_when_unauthenticated(tmp_path):
    app, sess_store = _build_app(tmp_path)
    try:
        r = TestClient(app).get("/optional", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"present": False}
    finally:
        _close(sess_store)


def test_optional_session_returns_session_when_authenticated(tmp_path):
    app, sess_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/optional", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"present": True}
    finally:
        _close(sess_store)


def test_session_data_round_trip(tmp_path):
    app, sess_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        assert c.get("/data").json() == {"counter": 0}
        assert c.post("/data").json() == {"counter": 1}
        assert c.post("/data").json() == {"counter": 2}
        assert c.get("/data").json() == {"counter": 2}
    finally:
        _close(sess_store)


def test_session_data_isolated_between_sessions(tmp_path):
    app, sess_store = _build_app(tmp_path)
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
        _close(sess_store)


def test_session_data_requires_auth(tmp_path):
    app, sess_store = _build_app(tmp_path)
    try:
        r = TestClient(app).get("/data", headers={"accept": "application/json"})
        assert r.status_code == 401
    finally:
        _close(sess_store)


def test_session_exposes_id_user_and_data(tmp_path):
    app, sess_store = _build_app(tmp_path)
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
        _close(sess_store)


def test_rights_default_to_empty_when_not_set(tmp_path):
    app, sess_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)  # no rights argument
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/whoami-full", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json()["rights"] == {
            "is_admin": False,
            "can_create_database": False,
            "db_admin": [],
            "db_writer": [],
            "db_reader": [],
        }
    finally:
        _close(sess_store)


def test_rights_round_trip_through_set_rights(tmp_path):
    app, sess_store = _build_app(tmp_path)
    try:
        rights = Rights(
            is_admin=False,
            can_create_database=True,
            db_admin=frozenset({"finance"}),
            db_writer=frozenset({"hr"}),
            db_reader=frozenset({"clickstream"}),
        )
        sid = _seed(sess_store, subject="bob", rights=rights)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/whoami-full", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json()["rights"] == {
            "is_admin": False,
            "can_create_database": True,
            "db_admin": ["finance"],
            "db_writer": ["hr"],
            "db_reader": ["clickstream"],
        }
    finally:
        _close(sess_store)


# ---- Session-subclass type assertions ----
# Verify each alias dep returns the correct Session subclass. These tests
# duplicate a small slice of the admission logic but their value is in the
# isinstance check / class-name assertion: route authors rely on the type
# system showing only methods available for the tier.


def test_session_admin_alias_returns_admin_session(tmp_path):
    app, sess_store = _build_app(tmp_path)
    try:
        sid = _seed(
            sess_store,
            rights=Rights(
                is_admin=True,
                can_create_database=False,
                db_admin=frozenset(),
                db_writer=frozenset(),
                db_reader=frozenset(),
            ),
        )

        @app.get("/_admin_type")
        async def probe(session: SessionAdmin):
            return {"type": type(session).__name__}

        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/_admin_type", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json()["type"] == AdminSession.__name__
    finally:
        _close(sess_store)


def test_session_database_admin_returns_database_admin_session(tmp_path):
    app, sess_store = _build_app(tmp_path)
    try:
        sid = _seed(
            sess_store,
            rights=Rights(
                is_admin=False,
                can_create_database=False,
                db_admin=frozenset({"finance"}),
                db_writer=frozenset(),
                db_reader=frozenset(),
            ),
        )

        @app.get("/_db_admin/{database}")
        async def probe(database: str, session: SessionDatabaseAdmin):
            return {"type": type(session).__name__, "database": session.database}

        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/_db_admin/finance", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {
            "type": DatabaseAdminSession.__name__,
            "database": "finance",
        }
    finally:
        _close(sess_store)


def test_session_read_returns_database_session(tmp_path):
    app, sess_store = _build_app(tmp_path)
    try:
        sid = _seed(
            sess_store,
            rights=Rights(
                is_admin=False,
                can_create_database=False,
                db_admin=frozenset(),
                db_writer=frozenset(),
                db_reader=frozenset({"hr"}),
            ),
        )

        @app.get("/_read/{database}")
        async def probe(database: str, session: SessionRead):
            return {"type": type(session).__name__, "database": session.database}

        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/_read/hr", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {
            "type": DatabaseSession.__name__,
            "database": "hr",
        }
    finally:
        _close(sess_store)


def test_session_write_returns_database_session(tmp_path):
    app, sess_store = _build_app(tmp_path)
    try:
        sid = _seed(
            sess_store,
            rights=Rights(
                is_admin=False,
                can_create_database=False,
                db_admin=frozenset(),
                db_writer=frozenset({"orders"}),
                db_reader=frozenset(),
            ),
        )

        @app.get("/_write/{database}")
        async def probe(database: str, session: SessionWrite):
            return {"type": type(session).__name__, "database": session.database}

        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/_write/orders", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json()["type"] == DatabaseSession.__name__
    finally:
        _close(sess_store)


def test_session_database_creator_returns_creator_session(tmp_path):
    app, sess_store = _build_app(tmp_path)
    try:
        sid = _seed(
            sess_store,
            rights=Rights(
                is_admin=False,
                can_create_database=True,
                db_admin=frozenset(),
                db_writer=frozenset(),
                db_reader=frozenset(),
            ),
        )

        @app.get("/_creator")
        async def probe(session: SessionDatabaseCreator):
            return {"type": type(session).__name__}

        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        r = c.get("/_creator", headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json()["type"] == DatabaseCreatorSession.__name__
    finally:
        _close(sess_store)


# ---- session.persist_data() ----


def test_persist_data_writes_through_to_session_store(tmp_path):
    """`session.persist_data()` writes the current `data` dict back so a
    subsequent request sees the change."""
    app, sess_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)
        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        # First POST mutates data and calls persist_data() inside the route.
        assert c.post("/data").json() == {"counter": 1}
        # A separate request reads back the persisted value.
        assert c.get("/data").json() == {"counter": 1}
    finally:
        _close(sess_store)


def test_persist_data_rejects_non_json_encodable_values(tmp_path):
    """`persist_data` is a thin wrapper around `SessionStore.update_data`,
    which serializes via `json.dumps`. Non-encodable values raise."""
    app, sess_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)

        @app.post("/_bad")
        async def write_bad(session: Session):
            session.data["bad"] = object()  # not JSON-encodable
            await session.persist_data()
            return {"ok": True}

        c = TestClient(app, raise_server_exceptions=False)
        c.cookies.set("iris_session", sid)
        r = c.post("/_bad")
        assert r.status_code == 500  # TypeError from json.dumps
    finally:
        _close(sess_store)


def test_persist_data_idempotent_when_no_changes(tmp_path):
    """Calling `persist_data()` without mutating `data` is harmless — writes
    the same dict back."""
    app, sess_store = _build_app(tmp_path)
    try:
        sid = _seed(sess_store)

        @app.post("/_noop")
        async def noop(session: Session):
            await session.persist_data()
            return {"ok": True}

        c = TestClient(app)
        c.cookies.set("iris_session", sid)
        assert c.post("/_noop").status_code == 200
        # Reading back finds no data (we never wrote anything).
        assert c.get("/data").json() == {"counter": 0}
    finally:
        _close(sess_store)
