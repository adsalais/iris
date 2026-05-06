# SQLite session store — design

**Date:** 2026-05-06
**Status:** draft, pending review

## Problem

`InMemorySessionStore` keeps sessions in a per-process Python dict. Each uvicorn worker has its own copy, so deploying with `--workers >1` silently breaks sessions: a request's cookie may hit a worker that doesn't know the session, and the user is bounced back to `/login`. The constraint is documented in `CLAUDE.md` ("single worker only") and is the binding limit on horizontal scale within a single host.

The user has decided not to introduce Redis. We swap the in-memory store for a SQLite-backed store. SQLite is multi-process-safe (file locks via `fcntl`), stdlib-only, and has more than enough headroom for the ≤20-user deployment profile. As a side benefit, sessions survive process restarts.

## Non-goals

- A pluggable store backend / `SessionStore` protocol with multiple implementations. There's exactly one store.
- A scheduled cleanup loop for expired rows. At ≤20 users the table grows by a handful of rows per hour; lazy deletion on read is sufficient.
- Encryption at rest (would matter only if the DB file were on shared storage; it isn't).
- A migration helper to copy existing in-memory sessions on rollout. In-memory sessions never survived restarts; cutting over is acceptable.

## Architecture

A single `SessionStore` class in `src/iris/auth/sessions.py` replaces `InMemorySessionStore` entirely. The public interface gains one method (`update_data`) and otherwise matches the existing surface:

```python
class SessionStore:
    def __init__(
        self,
        *,
        path: str,
        ttl_seconds: int,
        absolute_ttl_seconds: int,
        max_per_user: int = 10,
    ) -> None: ...

    async def create(self, user: User) -> UserSession: ...
    async def get_and_refresh(self, session_id: str) -> UserSession | None: ...
    async def update_data(self, session_id: str, data: dict[str, Any]) -> None: ...
    async def delete(self, session_id: str) -> None: ...
    async def close(self) -> None: ...
```

`iris.auth.routes.install` constructs `SessionStore` directly. The store opens its `sqlite3.Connection` in `__init__`; `close()` is registered for lifespan shutdown alongside `auth_close_provider` and `clickhouse_close_http`.

### Schema

```sql
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
```

- Timestamps are Unix epoch `INTEGER` — sortable, comparable, smaller than ISO strings, and avoid timezone surprises across process restarts.
- `User.groups` (currently `tuple[str, ...]`) serializes as JSON array; deserializes back into a tuple via `tuple(json.loads(...))`.
- `data` is JSON text. Values must be JSON-encodable (strings, ints, floats, bools, `None`, lists, dicts). Anything else raises `TypeError` at write time — same constraint Redis would impose.
- The `idx_sessions_subject` index serves the per-user eviction query (`SELECT id FROM sessions WHERE subject = ? ORDER BY created_at_ts`).
- The `idx_sessions_expires` index isn't strictly needed today — there's no scheduled cleanup — but keeping it costs near-zero and unblocks a future sweeper.

### Connection model

One `sqlite3.Connection` per process, opened at `SessionStore.__init__`:

```python
conn = sqlite3.connect(
    path,
    check_same_thread=False,
    isolation_level=None,   # autocommit mode; we control transactions explicitly
)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")
conn.execute("PRAGMA foreign_keys=ON")
conn.row_factory = sqlite3.Row
```

- **WAL mode** (Write-Ahead Log) lets readers run concurrently with one writer. Critical for multi-worker — without it, a write in worker A blocks reads in worker B.
- **`synchronous=NORMAL`** gives one `fsync` per checkpoint instead of per-commit. The trade-off is losing the most recent commit on power loss, which for sessions is acceptable (a logged-in user re-authenticates).
- **`check_same_thread=False`** allows the single connection to be used from any thread. We rely on SQLite's own internal mutex plus our `asyncio.to_thread` wrapper for safety.
- **`isolation_level=None`** puts the connection in autocommit mode; we issue `BEGIN IMMEDIATE` / `COMMIT` explicitly when we need a transaction (e.g., the `create` + eviction pair).

All sync `sqlite3` calls are wrapped in `asyncio.to_thread(...)`, the same pattern the ClickHouse handle uses for `clickhouse-connect`. This keeps the FastAPI event loop unblocked during disk I/O.

### TTL handling

Sliding TTL stays a property of `get_and_refresh`:

```sql
-- inside get_and_refresh:
SELECT * FROM sessions WHERE id = ?;
-- if row.expires_at_ts <= now or row.absolute_expires_at_ts <= now:
DELETE FROM sessions WHERE id = ?;
-- else:
UPDATE sessions SET expires_at_ts = ? WHERE id = ?;
```

The DELETE-on-expired path lazily reaps expired rows. There's no background sweeper; rows nobody reads stay until process restart erases the in-memory cache (the file persists, of course). At ≤20 users this isn't a problem — typical session count is bounded by `max_per_user × users`.

### Per-user session cap

`max_per_user` enforcement runs inside `create` as a single transaction:

```python
async with self._lock:
    await asyncio.to_thread(self._create_sync, session)

def _create_sync(self, session: UserSession) -> None:
    self._conn.execute("BEGIN IMMEDIATE")
    try:
        self._conn.execute("INSERT INTO sessions (...) VALUES (...)", ...)
        # Find sessions for this subject ordered oldest-first
        rows = self._conn.execute(
            "SELECT id FROM sessions WHERE subject = ? "
            "ORDER BY created_at_ts ASC", (session.user.subject,),
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
```

`asyncio.Lock` on the store guards against interleaving inside one process; SQLite's `BEGIN IMMEDIATE` handles cross-process serialization (writer-exclusive lock).

### `session.data` contract change

`UserSession.data` becomes a per-request snapshot. Mutations to `session.data` no longer auto-persist — there's no shared dict object between requests, because each request deserializes a fresh dict from `data_json`.

Routes that need persistence call `update_data` explicitly:

```python
@app.post("/draft")
async def save_draft(
    request: Request, session: Session, body: dict
):
    session.data["draft"] = body
    await request.app.state.auth_session_store.update_data(
        session.id, session.data
    )
    return {"ok": True}
```

A small ergonomics helper keeps this readable: `session.save_data()` could be a method on the request-scoped `Session` view that internally calls the store. Whether to add this is a YAGNI call — for v1.1 we keep it explicit at the store level. If we get more than two routes mutating `data`, revisit.

The single test exercising auto-write-through (`tests/auth/test_session_dep.py`'s counter test) updates to call `update_data`. No production routes depend on the old contract.

### File path & defaults

One new env var:

```
SESSION_DB_PATH=./iris-sessions.db
```

Read by `AuthSettings.from_env()`. Default `./iris-sessions.db` so `uv run iris` works without a `.env` change. Production deployments override (e.g., `/var/lib/iris/sessions.db`). The directory must exist and be writable by the iris service user — fail-loud if it isn't (SQLite raises `OperationalError` on open).

`SESSION_DB_PATH=:memory:` is supported for tests — a per-process in-memory database. Single-process tests work; multi-process tests must use a real file.

## Configuration

Updated env-var block in `CLAUDE.md`:

```
SESSION_COOKIE_NAME=iris_session
SESSION_TTL_SECONDS=43200            # 12h, sliding TTL refreshed on each request
SESSION_ABSOLUTE_TTL_SECONDS=2592000 # 30d, hard cap on top of sliding TTL
SESSION_MAX_PER_USER=10              # cap concurrent sessions per User.subject
SESSION_DB_PATH=./iris-sessions.db   # SQLite database file; :memory: for tests
COOKIE_SECURE=true
AUTHZ_CONFIG_PATH=./authz.yaml
```

The "Deployment constraint: single worker only" section in `CLAUDE.md` is removed entirely. The new section documents that workers can scale freely as long as they share the same `SESSION_DB_PATH`.

## Error handling

| Failure | Behavior |
|---|---|
| `SESSION_DB_PATH` unwritable / directory missing at boot | `sqlite3.OperationalError` from `connect`; app refuses to start (fail-loud) |
| `data` value not JSON-encodable | `TypeError` from `json.dumps`; propagated to the route which 500s |
| Concurrent writer hits `SQLITE_BUSY` | SQLite retries internally up to the busy-timeout (set to 5000ms via PRAGMA); persistent contention surfaces as `OperationalError` |
| Corrupt DB file | `sqlite3.DatabaseError` at next read; operator action required (delete file or restore backup) |
| Disk full during write | `OperationalError`; route 500s. No graceful degradation — sessions can't be served without the DB. |

## Testing

Three layers, all stdlib:

### Unit (`tests/auth/test_session_store.py`)

The existing test file currently targets `InMemorySessionStore`. Retargeted to `SessionStore` with a tempfile path. Test cases preserved:

- `create` returns a session with the expected user, ttl, absolute_ttl.
- `get_and_refresh` returns `None` for unknown ids.
- `get_and_refresh` extends `expires_at` by the configured TTL.
- `get_and_refresh` returns `None` and deletes the row when `expires_at <= now`.
- `get_and_refresh` returns `None` and deletes the row when `absolute_expires_at <= now`.
- `delete` removes the row.
- `max_per_user` evicts the oldest session when the cap is exceeded.
- Concurrent `create` calls (via `asyncio.gather`) all succeed without race-driven duplicate IDs.

New cases:

- `update_data` round-trips: write a dict, read it back via `get_and_refresh`.
- `update_data` rejects non-JSON-encodable values with `TypeError`.
- `data` round-trip preserves nested dicts/lists, ints, floats, bools, None.
- Re-opening the store (closing and constructing a new `SessionStore` against the same path) sees existing sessions — proves persistence.

### Multi-process (`tests/auth/test_session_store_multiprocess.py`, NEW)

Forks two child processes via `multiprocessing`. A tempfile DB is created in the parent. Child A `create`s a session and prints the session ID to a queue. Child B (in a separate process) opens the same DB and `get_and_refresh`es the session ID — must succeed. Proves the store works across uvicorn workers.

The test:
1. Sets up `SESSION_DB_PATH` to a tempfile.
2. Forks process A that calls `SessionStore(path=tmp).create(user)`, returns the session ID.
3. Forks process B that opens its own `SessionStore(path=tmp)` and calls `get_and_refresh(sid)`, returns the user subject.
4. Parent asserts B saw the same user A wrote.

Skipped on platforms where `multiprocessing.set_start_method("fork")` isn't available (Windows). Linux-only is fine — production deploys are Linux.

### Existing tests untouched except for the data contract update

`tests/auth/test_session_dep.py` has one test (`test_session_data_persists_across_requests`) that exercises auto-write-through. It updates to call `await store.update_data(session.id, session.data)` explicitly before returning. The other tests on session views, role resolution, etc. are unaffected.

`tests/conftest.py` sets `os.environ.setdefault("SESSION_DB_PATH", ":memory:")` at module scope so all tests get a per-process in-memory DB without needing fixture choreography. The `authed_client` fixture's `asyncio.run(store.create(user))` keeps working — it talks to the same single connection.

The existing bridge tests (`tests/clickhouse/test_login_provisioning.py`) don't change. They build their own apps via `build_app(install_clickhouse=True)`, which constructs a fresh `SessionStore` against `:memory:` (inherited from the conftest env var).

## Public surface

After this work, `iris.auth.sessions` exports:

```python
from iris.auth.sessions import SessionStore
```

`InMemorySessionStore` is removed. `iris.auth.deps.set_session_store` keeps the same signature; the type annotation changes from `InMemorySessionStore` to `SessionStore`.

## Open follow-ups (not in this spec)

- A scheduled cleanup loop for expired rows. Useful only at higher request volumes; currently lazy deletion is enough.
- A `Session.save_data()` ergonomic helper that encapsulates the explicit `await store.update_data(...)` call. Add when there's a second route mutating `data`.
- Encryption at rest. Required only if the DB file lives on shared/untrusted storage.
- An "old session sweep" admin endpoint for operators to bulk-expire stale sessions (e.g., during a security incident).
