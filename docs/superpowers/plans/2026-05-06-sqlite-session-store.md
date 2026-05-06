# SQLite Session Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `InMemorySessionStore` with a SQLite-backed `SessionStore` to lift the `--workers 1` deployment constraint without introducing Redis. Sessions become persistent across process restarts as a side benefit.

**Architecture:** Single `SessionStore` class in `src/iris/auth/sessions.py` opens one `sqlite3.Connection` per process in WAL mode. All sync calls wrapped in `asyncio.to_thread`. `session.data` becomes a per-request snapshot; routes call `await store.update_data(...)` to persist. New env var `SESSION_DB_PATH` (default `./iris-sessions.db`).

**Tech Stack:** Python 3.13, stdlib `sqlite3`, FastAPI, pytest, multiprocessing.

**Spec:** `docs/superpowers/specs/2026-05-06-sqlite-session-store-design.md`.

---

## File Structure

NEW files:

| Path | Responsibility |
|---|---|
| `tests/auth/test_session_store_multiprocess.py` | Fork-based test: process A writes a session, process B reads it through a separate `SessionStore` against the same DB file. Proves multi-worker actually works. |

MODIFIED files:

| Path | Change |
|---|---|
| `src/iris/auth/sessions.py` | Replace `InMemorySessionStore` with `SessionStore` (SQLite-backed). Same async API plus `update_data` and `close`. |
| `src/iris/auth/config.py` | Add `session_db_path: str` to `AuthSettings`; read `SESSION_DB_PATH` env var (default `./iris-sessions.db`). |
| `src/iris/auth/deps.py` | Type annotation for `set_session_store` and the resolver changes from `InMemorySessionStore` to `SessionStore`. |
| `src/iris/auth/routes.py` | `install()` constructs `SessionStore(path=settings.session_db_path, ...)` and registers `app.state.auth_close_session_store`. |
| `src/iris/app.py` | `_lifespan` calls `app.state.auth_close_session_store` on shutdown alongside the auth/CH closers. |
| `tests/conftest.py` | `os.environ.setdefault("SESSION_DB_PATH", ":memory:")` at module scope. |
| `tests/auth/test_session_store.py` | Retarget to `SessionStore` with a tempfile path; add `update_data` round-trip, persistence-across-reopen, and JSON-encodable validation tests. |
| `tests/auth/test_session_dep.py` | `_build_app` uses `SessionStore`; `bump_data` route calls `update_data`. |
| `CLAUDE.md` | Remove "single worker only" deployment constraint; add `SESSION_DB_PATH` to env-var block; document the `update_data` contract for `Session.data`. |

---

## Task 1: SessionStore class — basic CRUD + TTL

**Files:**
- Modify: `src/iris/auth/sessions.py`
- Modify: `tests/auth/test_session_store.py`

This task replaces `InMemorySessionStore` entirely. It implements the full SessionStore class (create / get_and_refresh / delete / update_data / close) and rewrites the test file to test it. Subsequent tasks wire it through the rest of the app.

- [ ] **Step 1.1: Replace `tests/auth/test_session_store.py` with the SQLite-targeted version**

```python
import asyncio
import json
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest

from iris.auth.identity import User
from iris.auth.sessions import SessionStore


@pytest.fixture
def user() -> User:
    return User(subject="alice", username="alice", display_name="Alice", groups=("admins",))


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "sessions.db"


@pytest.fixture
def store(store_path):
    s = SessionStore(
        path=str(store_path),
        ttl_seconds=60,
        absolute_ttl_seconds=3600,
    )
    yield s
    asyncio.run(s.close())


def test_create_returns_session_with_id(store, user):
    session = asyncio.run(store.create(user))
    assert isinstance(session.id, str)
    assert len(session.id) >= 32
    assert session.user == user


def test_get_returns_session_when_present(store, user):
    session = asyncio.run(store.create(user))
    fetched = asyncio.run(store.get_and_refresh(session.id))
    assert fetched is not None
    assert fetched.id == session.id
    assert fetched.user == user


def test_get_returns_none_when_absent(store):
    assert asyncio.run(store.get_and_refresh("not-a-real-id")) is None


def test_get_refreshes_expires_at(store, user):
    session = asyncio.run(store.create(user))
    original = session.expires_at
    asyncio.run(asyncio.sleep(1.1))  # SECOND-resolution timestamps need >=1s gap
    fetched = asyncio.run(store.get_and_refresh(session.id))
    assert fetched is not None
    assert fetched.expires_at > original


def test_get_evicts_expired_session(store_path, user):
    s = SessionStore(path=str(store_path), ttl_seconds=1, absolute_ttl_seconds=3600)
    try:
        session = asyncio.run(s.create(user))
        asyncio.run(asyncio.sleep(2))  # let TTL elapse
        assert asyncio.run(s.get_and_refresh(session.id)) is None
        # Second lookup confirms eviction
        assert asyncio.run(s.get_and_refresh(session.id)) is None
    finally:
        asyncio.run(s.close())


def test_get_evicts_when_absolute_expired(store_path, user):
    s = SessionStore(path=str(store_path), ttl_seconds=3600, absolute_ttl_seconds=1)
    try:
        session = asyncio.run(s.create(user))
        asyncio.run(asyncio.sleep(2))  # absolute TTL elapses
        assert asyncio.run(s.get_and_refresh(session.id)) is None
    finally:
        asyncio.run(s.close())


def test_delete_removes_session(store, user):
    session = asyncio.run(store.create(user))
    asyncio.run(store.delete(session.id))
    assert asyncio.run(store.get_and_refresh(session.id)) is None


def test_delete_unknown_id_is_noop(store):
    asyncio.run(store.delete("not-a-real-id"))  # must not raise


def test_session_ids_are_unique(store, user):
    s1 = asyncio.run(store.create(user))
    s2 = asyncio.run(store.create(user))
    assert s1.id != s2.id


def test_user_groups_round_trip(store_path):
    s = SessionStore(path=str(store_path), ttl_seconds=60, absolute_ttl_seconds=3600)
    try:
        u = User(subject="x", username="x", display_name="X", groups=("a", "b", "c"))
        sess = asyncio.run(s.create(u))
        fetched = asyncio.run(s.get_and_refresh(sess.id))
        assert fetched is not None
        assert fetched.user.groups == ("a", "b", "c")
    finally:
        asyncio.run(s.close())


def test_create_evicts_oldest_when_cap_exceeded(store_path, user):
    s = SessionStore(
        path=str(store_path),
        ttl_seconds=60,
        absolute_ttl_seconds=3600,
        max_per_user=3,
    )
    try:
        sessions = []
        for _ in range(5):
            sessions.append(asyncio.run(s.create(user)))
            # SQLite stores INTEGER seconds — without a sleep, two sessions can
            # share created_at_ts and the eviction order becomes undefined.
            import time
            time.sleep(1.05)
        assert asyncio.run(s.get_and_refresh(sessions[0].id)) is None
        assert asyncio.run(s.get_and_refresh(sessions[1].id)) is None
        assert asyncio.run(s.get_and_refresh(sessions[2].id)) is not None
        assert asyncio.run(s.get_and_refresh(sessions[3].id)) is not None
        assert asyncio.run(s.get_and_refresh(sessions[4].id)) is not None
    finally:
        asyncio.run(s.close())


def test_cap_is_per_user_not_global(store_path):
    s = SessionStore(
        path=str(store_path),
        ttl_seconds=60,
        absolute_ttl_seconds=3600,
        max_per_user=2,
    )
    try:
        alice = User(subject="alice", username="alice", display_name="A", groups=())
        bob = User(subject="bob", username="bob", display_name="B", groups=())
        a1 = asyncio.run(s.create(alice))
        a2 = asyncio.run(s.create(alice))
        b1 = asyncio.run(s.create(bob))
        b2 = asyncio.run(s.create(bob))
        for sid in (a1.id, a2.id, b1.id, b2.id):
            assert asyncio.run(s.get_and_refresh(sid)) is not None
    finally:
        asyncio.run(s.close())


def test_update_data_round_trips(store, user):
    session = asyncio.run(store.create(user))
    asyncio.run(store.update_data(session.id, {"key": "value", "n": 42}))
    fetched = asyncio.run(store.get_and_refresh(session.id))
    assert fetched is not None
    assert fetched.data == {"key": "value", "n": 42}


def test_update_data_preserves_nested_types(store, user):
    session = asyncio.run(store.create(user))
    payload = {
        "list": [1, 2, 3],
        "dict": {"nested": True, "deep": {"x": None}},
        "float": 1.5,
        "bool": False,
        "null": None,
    }
    asyncio.run(store.update_data(session.id, payload))
    fetched = asyncio.run(store.get_and_refresh(session.id))
    assert fetched is not None
    assert fetched.data == payload


def test_update_data_rejects_non_json_encodable(store, user):
    session = asyncio.run(store.create(user))

    class Opaque:
        pass

    with pytest.raises(TypeError):
        asyncio.run(store.update_data(session.id, {"x": Opaque()}))


def test_update_data_unknown_id_is_noop(store):
    asyncio.run(store.update_data("not-a-real-id", {"x": 1}))  # must not raise


def test_persistence_across_reopen(store_path, user):
    """Closing and reopening the store sees existing sessions — the multi-worker payoff."""
    s1 = SessionStore(path=str(store_path), ttl_seconds=60, absolute_ttl_seconds=3600)
    try:
        session = asyncio.run(s1.create(user))
        asyncio.run(s1.update_data(session.id, {"k": "v"}))
        sid = session.id
    finally:
        asyncio.run(s1.close())

    s2 = SessionStore(path=str(store_path), ttl_seconds=60, absolute_ttl_seconds=3600)
    try:
        fetched = asyncio.run(s2.get_and_refresh(sid))
        assert fetched is not None
        assert fetched.data == {"k": "v"}
    finally:
        asyncio.run(s2.close())


def test_close_is_idempotent(store_path, user):
    s = SessionStore(path=str(store_path), ttl_seconds=60, absolute_ttl_seconds=3600)
    asyncio.run(s.close())
    asyncio.run(s.close())  # must not raise
```

- [ ] **Step 1.2: Run the tests to verify they fail**

```
uv run pytest tests/auth/test_session_store.py -v
```
Expected: All tests fail with `ImportError: cannot import name 'SessionStore'` (the class doesn't exist yet) or `AttributeError`.

- [ ] **Step 1.3: Replace `src/iris/auth/sessions.py` with the SessionStore implementation**

```python
"""SQLite-backed session store.

One sqlite3.Connection per process. WAL mode + synchronous=NORMAL handle
cross-process locking so multiple uvicorn workers can share a single DB
file. All sync sqlite3 calls are wrapped in asyncio.to_thread to keep the
FastAPI event loop unblocked.

Schema:

    CREATE TABLE sessions (
        id                       TEXT PRIMARY KEY,
        subject                  TEXT NOT NULL,
        username                 TEXT NOT NULL,
        display_name             TEXT NOT NULL,
        groups_json              TEXT NOT NULL,
        created_at_ts            INTEGER NOT NULL,
        expires_at_ts            INTEGER NOT NULL,
        absolute_expires_at_ts   INTEGER NOT NULL,
        data_json                TEXT NOT NULL DEFAULT '{}'
    );

Timestamps are Unix epoch INTEGER. Groups and data are JSON text.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, UTC
from typing import Any

from iris.auth.identity import User, UserSession

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                       TEXT PRIMARY KEY,
    subject                  TEXT NOT NULL,
    username                 TEXT NOT NULL,
    display_name             TEXT NOT NULL,
    groups_json              TEXT NOT NULL,
    created_at_ts            INTEGER NOT NULL,
    expires_at_ts            INTEGER NOT NULL,
    absolute_expires_at_ts   INTEGER NOT NULL,
    data_json                TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sessions_subject ON sessions(subject);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at_ts);
"""


def _to_ts(dt: datetime) -> int:
    return int(dt.timestamp())


def _from_ts(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


def _row_to_session(row: sqlite3.Row) -> UserSession:
    user = User(
        subject=row["subject"],
        username=row["username"],
        display_name=row["display_name"],
        groups=tuple(json.loads(row["groups_json"])),
    )
    return UserSession(
        id=row["id"],
        user=user,
        created_at=_from_ts(row["created_at_ts"]),
        expires_at=_from_ts(row["expires_at_ts"]),
        absolute_expires_at=_from_ts(row["absolute_expires_at_ts"]),
        data=json.loads(row["data_json"]),
    )


class SessionStore:
    def __init__(
        self,
        *,
        path: str,
        ttl_seconds: int,
        absolute_ttl_seconds: int,
        max_per_user: int = 10,
    ) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._absolute_ttl = timedelta(seconds=absolute_ttl_seconds)
        self._max_per_user = max_per_user
        self._lock = asyncio.Lock()
        self._closed = False
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; we issue BEGIN/COMMIT explicitly
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        # journal_mode=WAL is per-database and persists in the file. Safe to set
        # every time; no-op on :memory: (returns "memory").
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    async def create(self, user: User) -> UserSession:
        async with self._lock:
            return await asyncio.to_thread(self._create_sync, user)

    def _create_sync(self, user: User) -> UserSession:
        now = datetime.now(UTC)
        session = UserSession(
            id=secrets.token_urlsafe(32),
            user=user,
            created_at=now,
            expires_at=now + self._ttl,
            absolute_expires_at=now + self._absolute_ttl,
        )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT INTO sessions ("
                "  id, subject, username, display_name, groups_json,"
                "  created_at_ts, expires_at_ts, absolute_expires_at_ts, data_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session.id,
                    session.user.subject,
                    session.user.username,
                    session.user.display_name,
                    json.dumps(list(session.user.groups)),
                    _to_ts(session.created_at),
                    _to_ts(session.expires_at),
                    _to_ts(session.absolute_expires_at),
                    "{}",
                ),
            )
            rows = self._conn.execute(
                "SELECT id FROM sessions WHERE subject = ? "
                "ORDER BY created_at_ts ASC",
                (session.user.subject,),
            ).fetchall()
            excess = len(rows) - self._max_per_user
            if excess > 0:
                ids_to_delete = [r["id"] for r in rows[:excess]]
                self._conn.executemany(
                    "DELETE FROM sessions WHERE id = ?",
                    [(sid,) for sid in ids_to_delete],
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return session

    async def get_and_refresh(self, session_id: str) -> UserSession | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_and_refresh_sync, session_id)

    def _get_and_refresh_sync(self, session_id: str) -> UserSession | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        now = datetime.now(UTC)
        expires_at = _from_ts(row["expires_at_ts"])
        absolute_expires_at = _from_ts(row["absolute_expires_at_ts"])
        if expires_at <= now or absolute_expires_at <= now:
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return None
        new_expires = now + self._ttl
        self._conn.execute(
            "UPDATE sessions SET expires_at_ts = ? WHERE id = ?",
            (_to_ts(new_expires), session_id),
        )
        # Build the session view from the row, but with the refreshed expires_at
        session = _row_to_session(row)
        return UserSession(
            id=session.id,
            user=session.user,
            created_at=session.created_at,
            expires_at=new_expires,
            absolute_expires_at=session.absolute_expires_at,
            data=session.data,
        )

    async def update_data(self, session_id: str, data: dict[str, Any]) -> None:
        # Serialize OUTSIDE the lock so TypeError surfaces before we touch the DB.
        data_json = json.dumps(data)
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "UPDATE sessions SET data_json = ? WHERE id = ?",
                (data_json, session_id),
            )

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "DELETE FROM sessions WHERE id = ?",
                (session_id,),
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._conn.close)
```

- [ ] **Step 1.4: Run the tests to verify they pass**

```
uv run pytest tests/auth/test_session_store.py -v
```
Expected: All tests pass.

- [ ] **Step 1.5: Type-check**

```
uv run basedpyright --level error src/iris/auth/sessions.py tests/auth/test_session_store.py
```
Expected: Errors only from other files that still reference `InMemorySessionStore` (resolved in Task 3). The two files we just wrote should be clean.

- [ ] **Step 1.6: Commit**

```
git add src/iris/auth/sessions.py tests/auth/test_session_store.py
git commit -m "feat(auth): SQLite-backed SessionStore replaces in-memory store

Single SessionStore class with create/get_and_refresh/update_data/delete/
close. WAL mode + synchronous=NORMAL handle cross-process locking. All
sqlite3 calls wrapped in asyncio.to_thread. session.data is now a
per-request snapshot — routes call update_data explicitly to persist.

Sessions survive process restarts as a side benefit. Wiring into
AuthSettings/install/conftest follows in subsequent commits."
```

---

## Task 2: Multi-process test

**Files:**
- Create: `tests/auth/test_session_store_multiprocess.py`

Prove the multi-worker scenario actually works: a session written from process A is visible to process B through a separate `SessionStore` instance against the same DB file.

- [ ] **Step 2.1: Create the test**

```python
"""Multi-process test: process A creates a session, process B reads it.

Proves SessionStore can back multiple uvicorn workers that share one DB file.
Skipped on platforms where ``fork`` start method isn't available (i.e. Windows).
"""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import sys
from pathlib import Path

import pytest

from iris.auth.identity import User
from iris.auth.sessions import SessionStore


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="multiprocessing.fork is not available on Windows",
)


def _writer(db_path: str, queue) -> None:
    store = SessionStore(path=db_path, ttl_seconds=60, absolute_ttl_seconds=3600)
    try:
        user = User(
            subject="cross_proc_user",
            username="cross_proc_user",
            display_name="Cross",
            groups=("g1", "g2"),
        )
        session = asyncio.run(store.create(user))
        asyncio.run(store.update_data(session.id, {"shared": "yes"}))
        queue.put(session.id)
    finally:
        asyncio.run(store.close())


def _reader(db_path: str, session_id: str, queue) -> None:
    store = SessionStore(path=db_path, ttl_seconds=60, absolute_ttl_seconds=3600)
    try:
        session = asyncio.run(store.get_and_refresh(session_id))
        if session is None:
            queue.put(None)
        else:
            queue.put(
                {
                    "subject": session.user.subject,
                    "groups": list(session.user.groups),
                    "data": session.data,
                }
            )
    finally:
        asyncio.run(store.close())


def test_session_visible_across_processes(tmp_path: Path) -> None:
    db_path = str(tmp_path / "shared.db")
    ctx = mp.get_context("fork")
    q = ctx.Queue()

    writer = ctx.Process(target=_writer, args=(db_path, q))
    writer.start()
    writer.join(timeout=10)
    assert writer.exitcode == 0, "writer process failed"
    session_id = q.get(timeout=5)
    assert isinstance(session_id, str)

    reader = ctx.Process(target=_reader, args=(db_path, session_id, q))
    reader.start()
    reader.join(timeout=10)
    assert reader.exitcode == 0, "reader process failed"
    result = q.get(timeout=5)

    assert result is not None, "reader saw no session"
    assert result["subject"] == "cross_proc_user"
    assert result["groups"] == ["g1", "g2"]
    assert result["data"] == {"shared": "yes"}
```

- [ ] **Step 2.2: Run the test**

```
uv run pytest tests/auth/test_session_store_multiprocess.py -v
```
Expected: PASS.

- [ ] **Step 2.3: Commit**

```
git add tests/auth/test_session_store_multiprocess.py
git commit -m "test(auth): cross-process SessionStore visibility

Forks two child processes; writer creates a session + sets data,
reader (in a separate process, separate sqlite3.Connection) sees both.
Validates the multi-worker scenario the SQLite swap was designed to unblock."
```

---

## Task 3: Wire SessionStore into AuthSettings + routes.install + conftest

**Files:**
- Modify: `src/iris/auth/config.py`
- Modify: `src/iris/auth/routes.py`
- Modify: `src/iris/auth/deps.py`
- Modify: `tests/conftest.py`

The store class is in place. Now make the rest of the codebase use it. After this task `InMemorySessionStore` no longer exists anywhere; only `SessionStore` is imported.

- [ ] **Step 3.1: Add `session_db_path` to `AuthSettings`**

In `src/iris/auth/config.py`, add the field and the env-var read:

```python
@dataclass(frozen=True)
class AuthSettings:
    method: Literal["oauth", "ldap", "mock"]
    cookie_name: str
    ttl_seconds: int
    absolute_ttl_seconds: int
    max_per_user: int
    cookie_secure: bool
    session_db_path: str          # NEW
    oidc: OIDCSettings | None
    ldap: LDAPSettings | None
    mock: MockSettings | None

    @classmethod
    def from_env(cls) -> AuthSettings:
        # ... existing parsing ...
        cookie_secure = _get_bool("COOKIE_SECURE", True)
        session_db_path = os.environ.get(
            "SESSION_DB_PATH", "./iris-sessions.db"
        ).strip() or "./iris-sessions.db"      # NEW
        # ... rest unchanged, but the final cls(...) call must pass session_db_path:
        return cls(
            method=method,
            cookie_name=cookie_name,
            ttl_seconds=ttl_seconds,
            absolute_ttl_seconds=absolute_ttl_seconds,
            max_per_user=max_per_user,
            cookie_secure=cookie_secure,
            session_db_path=session_db_path,    # NEW
            oidc=oidc,
            ldap=ldap,
            mock=mock,
        )
```

- [ ] **Step 3.2: Update `src/iris/auth/deps.py` to import `SessionStore`**

Change all references from `InMemorySessionStore` to `SessionStore`. Use Edit's replace_all on the file:

```python
# In src/iris/auth/deps.py:
# - Change `from iris.auth.sessions import InMemorySessionStore` to `from iris.auth.sessions import SessionStore`
# - Change every `InMemorySessionStore` annotation to `SessionStore`
```

After the edit, the file imports and uses `SessionStore` exclusively.

- [ ] **Step 3.3: Update `src/iris/auth/routes.py` to construct `SessionStore`**

In `src/iris/auth/routes.py`'s `install` function (around line 168), change:

```python
# OLD
store = InMemorySessionStore(
    ttl_seconds=settings.ttl_seconds,
    absolute_ttl_seconds=settings.absolute_ttl_seconds,
    max_per_user=settings.max_per_user,
)
```

to:

```python
# NEW
from iris.auth.sessions import SessionStore

store = SessionStore(
    path=settings.session_db_path,
    ttl_seconds=settings.ttl_seconds,
    absolute_ttl_seconds=settings.absolute_ttl_seconds,
    max_per_user=settings.max_per_user,
)
app.state.auth_close_session_store = store.close
```

The top-of-file `from iris.auth.sessions import InMemorySessionStore` import is no longer needed; remove it.

- [ ] **Step 3.4: Update `tests/conftest.py`**

Add the env var setdefault near the other auth env vars:

```python
os.environ.setdefault("COOKIE_SECURE", "false")
# Tests don't want the CH bridge installed by default ...
os.environ.setdefault("IRIS_NO_CLICKHOUSE", "1")
# Sessions stored per-process in-memory; one connection per process means
# :memory: works for single-process tests.
os.environ.setdefault("SESSION_DB_PATH", ":memory:")
```

- [ ] **Step 3.5: Run the auth suite**

```
uv run pytest tests/auth tests/clickhouse --ignore=tests/auth/integration -x
```
Expected: Most tests pass. `tests/auth/test_session_dep.py` may still fail because it imports `InMemorySessionStore` and uses the auto-write-through pattern — that's fixed in Task 4. If you see import errors from `test_session_dep.py`, that's expected; ignore for now.

If any other test fails (other than `test_session_dep.py`), stop and investigate — there may be a residual `InMemorySessionStore` reference.

- [ ] **Step 3.6: Commit**

```
git add src/iris/auth/config.py src/iris/auth/deps.py src/iris/auth/routes.py tests/conftest.py
git commit -m "feat(auth): wire SessionStore through AuthSettings + routes.install

AuthSettings reads SESSION_DB_PATH (default ./iris-sessions.db).
routes.install constructs SessionStore directly and registers
app.state.auth_close_session_store for the lifespan teardown.
deps.py type annotations updated. tests/conftest.py defaults
SESSION_DB_PATH to :memory: at module scope.

test_session_dep.py still references InMemorySessionStore and the
auto-write-through contract; fixed in the next commit."
```

---

## Task 4: Update test_session_dep.py + delete InMemorySessionStore

**Files:**
- Modify: `tests/auth/test_session_dep.py`
- Modify: `src/iris/auth/sessions.py` (delete the dead class — but it was already replaced wholesale in Task 1, so this is a no-op verification)

After Task 1 there was no `InMemorySessionStore` left in `sessions.py`; verify and update the one remaining caller.

- [ ] **Step 4.1: Confirm no stale `InMemorySessionStore` references**

```
grep -rn "InMemorySessionStore" src/ tests/
```
Expected: Only `tests/auth/test_session_dep.py` matches.

- [ ] **Step 4.2: Update `tests/auth/test_session_dep.py`**

Replace the file's contents with the SessionStore-aware version. Two structural changes:
1. `_build_app` opens a `SessionStore` against a per-test tempfile DB and registers a teardown.
2. The `bump_data` route gets a `Request` parameter and calls `await request.app.state.auth_session_store.update_data(session.id, session.data)` after mutating the dict.

```python
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
```

- [ ] **Step 4.3: Run the full auth suite**

```
uv run pytest tests/auth tests/clickhouse --ignore=tests/auth/integration
```
Expected: All tests pass.

- [ ] **Step 4.4: Type-check**

```
uv run basedpyright --level error
```
Expected: 0 errors.

- [ ] **Step 4.5: Commit**

```
git add tests/auth/test_session_dep.py
git commit -m "test(auth): test_session_dep uses SessionStore + explicit update_data

The bump_data route now mutates session.data and calls
update_data(session.id, session.data) before returning. Auto-write-through
is no longer a contract — sessions live in SQLite and the in-memory dict
is a per-request snapshot."
```

---

## Task 5: Lifespan close on shutdown

**Files:**
- Modify: `src/iris/app.py`

The `SessionStore` keeps a sqlite3 connection open. Close it on app shutdown alongside the existing auth and ClickHouse closers.

- [ ] **Step 5.1: Update `_lifespan` in `src/iris/app.py`**

```python
@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup is no-op; install() runs eagerly during build_app(). On shutdown,
    # close any teardown hooks registered by the auth or clickhouse layers
    # (OAuthProvider httpx client, the impersonation httpx client, the SQLite
    # session store).
    yield
    closer = getattr(app.state, "auth_close_provider", None)
    if closer is not None:
        await closer()
    ch_closer = getattr(app.state, "clickhouse_close_http", None)
    if ch_closer is not None:
        await ch_closer()
    sess_closer = getattr(app.state, "auth_close_session_store", None)
    if sess_closer is not None:
        await sess_closer()
```

- [ ] **Step 5.2: Run the full suite to confirm no regressions**

```
uv run pytest --ignore=tests/auth/integration
```
Expected: All tests pass.

- [ ] **Step 5.3: Commit**

```
git add src/iris/app.py
git commit -m "fix(app): close SessionStore connection on app shutdown

_lifespan now invokes app.state.auth_close_session_store alongside the
existing auth provider and ClickHouse http closers."
```

---

## Task 6: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

Reflect the design changes in project docs: remove the single-worker constraint; add `SESSION_DB_PATH`; document the explicit `update_data` contract.

- [ ] **Step 6.1: Replace the "Deployment constraint: single worker only" section**

Search `CLAUDE.md` for the heading `### Deployment constraint: single worker only` and replace the entire section (heading + body, through the next `### ` heading) with:

```markdown
### Multi-worker deployment

Sessions live in a SQLite file; multiple uvicorn workers share state by pointing at the same `SESSION_DB_PATH`. The store opens its connection in WAL mode (`PRAGMA journal_mode=WAL`) so concurrent readers don't block on a writer. Workers can scale freely on a single host (e.g., `uvicorn --workers 4`) as long as the DB path is on local disk reachable by every worker. Cross-host deploys still need a shared filesystem — or swap the store backend.

Sessions also survive process restarts. `uv run iris` and a redeploy no longer log every user out.
```

- [ ] **Step 6.2: Add `SESSION_DB_PATH` to the env var block**

In the auth section's "Configuration" subsection, find the env-var block:

```
SESSION_COOKIE_NAME=iris_session
SESSION_TTL_SECONDS=43200            # 12h, sliding TTL refreshed on each request
SESSION_ABSOLUTE_TTL_SECONDS=2592000 # 30d, hard cap on top of sliding TTL
SESSION_MAX_PER_USER=10              # cap concurrent sessions per User.subject (oldest evicted)
COOKIE_SECURE=true                   # set false for local dev over http
AUTHZ_CONFIG_PATH=./authz.yaml       # role mapping; required, fail-loud if unset
```

Replace with:

```
SESSION_COOKIE_NAME=iris_session
SESSION_TTL_SECONDS=43200            # 12h, sliding TTL refreshed on each request
SESSION_ABSOLUTE_TTL_SECONDS=2592000 # 30d, hard cap on top of sliding TTL
SESSION_MAX_PER_USER=10              # cap concurrent sessions per User.subject (oldest evicted)
SESSION_DB_PATH=./iris-sessions.db   # SQLite file backing the session store; :memory: for tests
COOKIE_SECURE=true                   # set false for local dev over http
AUTHZ_CONFIG_PATH=./authz.yaml       # role mapping; required, fail-loud if unset
```

- [ ] **Step 6.3: Rewrite the "Per-session server-side data" section**

Find the section starting with `### Per-session server-side data` and replace the example code blocks. The new contract: `session.data` is a per-request snapshot; mutations require an explicit `await store.update_data(session.id, session.data)`.

Replace the existing examples with:

```python
from iris.auth import Session

@app.post("/draft")
async def save_draft(request: Request, session: Session, body: dict):
    session.data["draft"] = body
    await request.app.state.auth_session_store.update_data(
        session.id, session.data
    )
    return {"ok": True}

@app.get("/draft")
async def get_draft(session: Session):
    return session.data.get("draft", {})

@app.get("/me/full")
async def me_full(session: Session):
    return {
        "id": session.id,
        "logged_in_at": session.created_at,
        "data_keys": list(session.data),
        "roles": sorted(session.roles),
    }
```

And replace the bullet list ("`Session.data` is the dict directly — mutation writes through to the store.") with:

```markdown
- `session.data` is a per-request snapshot. Mutations to the dict do not auto-persist; routes that want the change to survive call `await request.app.state.auth_session_store.update_data(session.id, session.data)` before returning.
- `Session` exposes `id`, `user`, `created_at`, `expires_at`, `data`, and `roles` on a single value. Routes that need only the user write `session.user`; routes that need the per-session bag write `session.data`.
```

Also rewrite the "Lifecycle" paragraph that follows. Old text:

```
Lifecycle: data lives in-memory alongside the session and is wiped on logout / expiry / process restart. The store API doesn't persist `data` separately; if/when the v1.1 Redis-backed store arrives, `data` will need to be serializable (JSON-ish values only). For v1, anything Python can hold is fair game. Read-modify-write across an `await` between two requests for the same session has the standard interleaving race — acceptable at ≤20-user scale; document or use `asyncio.Lock` if a route needs atomic updates.
```

New text:

```
Lifecycle: `data` is JSON-encoded into the SQLite row alongside the session. Mutations are persisted by `update_data` and survive process restarts. Values must be JSON-encodable (strings, ints, floats, bools, `None`, lists, dicts) — anything else raises `TypeError` at write time. Read-modify-write across an `await` between two requests for the same session has the standard interleaving race; acceptable at ≤20-user scale, document or use `asyncio.Lock` if a route needs atomic updates.
```

- [ ] **Step 6.4: Update the "Open security follow-ups (v1.1)" bullet about the in-memory store**

Find the bullet that begins:

```
- `InMemorySessionStore` is per-process, which forces `--workers 1` (see "Deployment constraint" above). Swapping to a Redis/DB-backed store would lift the constraint and also survive process restarts.
```

Remove it entirely (the constraint is gone). The remaining bullets in that section stay.

- [ ] **Step 6.5: Run the full suite one last time**

```
uv run pytest --ignore=tests/auth/integration
uv run basedpyright --level error
uv run basedpyright --level warning
uv run ruff check
```
Expected: all clean.

- [ ] **Step 6.6: Commit**

```
git add CLAUDE.md
git commit -m "docs: SQLite session store lifts --workers 1 + update_data contract

- Remove the 'Deployment constraint: single worker only' section; replace
  with 'Multi-worker deployment' explaining how WAL mode + shared
  SESSION_DB_PATH let workers scale.
- Add SESSION_DB_PATH to the env-var block.
- Rewrite 'Per-session server-side data' to document the explicit
  update_data contract; update the example route accordingly.
- Drop the obsolete v1.1 follow-up about replacing InMemorySessionStore."
```

---

## Self-review

**Spec coverage:**

- [x] Single `SessionStore` class in `src/iris/auth/sessions.py` — Task 1.
- [x] Schema: id PK, subject/username/display_name/groups_json/timestamps as INTEGER/data_json — Task 1, `_SCHEMA` constant.
- [x] WAL mode, `synchronous=NORMAL`, `check_same_thread=False`, `busy_timeout=5000`, `isolation_level=None` — Task 1, `_init_schema`.
- [x] `asyncio.to_thread` wraps every sync call — Task 1.
- [x] Sliding TTL refresh + absolute expiry + lazy expired-row deletion in `get_and_refresh` — Task 1.
- [x] `max_per_user` enforcement inside `BEGIN IMMEDIATE` transaction — Task 1, `_create_sync`.
- [x] `update_data` round-trip with JSON encoding — Task 1.
- [x] `JSON.dumps` validation outside the lock — Task 1, `update_data`.
- [x] `close` is idempotent — Task 1.
- [x] Multi-process verification — Task 2.
- [x] `SESSION_DB_PATH` env var with default `./iris-sessions.db` — Task 3.
- [x] `tests/conftest.py` defaults to `:memory:` — Task 3.
- [x] Test file uses tempfile DB paths — Task 1 (fixtures).
- [x] `Session.data` contract change with explicit `update_data` in `test_session_dep.py` — Task 4.
- [x] `_lifespan` closes the store — Task 5.
- [x] CLAUDE.md updates: deployment constraint removed, env var added, `update_data` documented — Task 6.

**Placeholder scan:** No "TBD"/"TODO"/"similar to Task N"/"add appropriate error handling" patterns. Every code block is complete.

**Type consistency:**
- `SessionStore.__init__(*, path: str, ttl_seconds: int, absolute_ttl_seconds: int, max_per_user: int = 10)` — used identically in tests, conftest, and routes.
- `SessionStore.update_data(session_id: str, data: dict[str, Any]) -> None` — same signature in `_lifespan`-unrelated callers.
- `app.state.auth_close_session_store` is the registration name; matches Task 3 (`routes.install`) and Task 5 (`_lifespan`).
- `app.state.auth_session_store` is the existing convention from `set_session_store`; routes use it via `request.app.state.auth_session_store.update_data(...)` (Task 4 example, CLAUDE.md docs).
