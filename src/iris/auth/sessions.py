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
        data_json                TEXT NOT NULL DEFAULT '{}',
        rights_json              TEXT NOT NULL DEFAULT '{}'
    );

Timestamps are Unix epoch INTEGER. Groups, data, and rights are JSON text.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, UTC
from typing import Any

from iris.auth.identity import User, UserSession
from iris.auth.session import EMPTY_RIGHTS, Rights, rights_from_dict, rights_to_dict

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
    data_json                TEXT NOT NULL DEFAULT '{}',
    rights_json              TEXT NOT NULL DEFAULT '{}'
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
    rights_raw = json.loads(row["rights_json"]) if row["rights_json"] else {}
    rights = rights_from_dict(rights_raw) if rights_raw else EMPTY_RIGHTS
    return UserSession(
        id=row["id"],
        user=user,
        created_at=_from_ts(row["created_at_ts"]),
        expires_at=_from_ts(row["expires_at_ts"]),
        absolute_expires_at=_from_ts(row["absolute_expires_at_ts"]),
        data=json.loads(row["data_json"]),
        rights=rights,
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
        """Open a SQLite-backed session store.

        Args:
            path: SQLite file path; ``":memory:"`` is supported for tests.
            ttl_seconds: sliding TTL refreshed on every ``get_and_refresh``.
            absolute_ttl_seconds: hard upper bound from ``created_at``;
                sessions past this expire even if recently refreshed.
            max_per_user: oldest sessions are pruned on ``create()`` once a
                subject exceeds this count.

        Concurrency: one ``sqlite3.Connection`` per process, serialized by
        ``self._lock`` (asyncio). Sync ``sqlite3`` calls run via
        ``asyncio.to_thread`` so the event loop stays unblocked. WAL mode
        plus ``synchronous=NORMAL`` make the file safe to share across
        multiple uvicorn workers.

        Lifecycle: ``close()`` is idempotent and required (registered into
        ``app.state.shutdown_hooks`` by ``iris.auth.routes.install``).
        """
        self._ttl = timedelta(seconds=ttl_seconds)
        self._absolute_ttl = timedelta(seconds=absolute_ttl_seconds)
        self._max_per_user = max_per_user
        self._lock = asyncio.Lock()
        self._closed = False
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
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
                """
                INSERT INTO sessions (
                    id, subject, username, display_name, groups_json,
                    created_at_ts, expires_at_ts, absolute_expires_at_ts,
                    data_json, rights_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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
                    "{}",
                ),
            )
            rows = self._conn.execute(
                "SELECT id FROM sessions WHERE subject = ? ORDER BY created_at_ts ASC",
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
        session = _row_to_session(row)
        return UserSession(
            id=session.id,
            user=session.user,
            created_at=session.created_at,
            expires_at=new_expires,
            absolute_expires_at=session.absolute_expires_at,
            data=session.data,
            rights=session.rights,
        )

    async def update_data(self, session_id: str, data: dict[str, Any]) -> None:
        data_json = json.dumps(data)
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "UPDATE sessions SET data_json = ? WHERE id = ?",
                (data_json, session_id),
            )

    async def set_rights(self, session_id: str, rights: Rights) -> None:
        """Persist the derived ``Rights`` view onto a session row.

        Called once per real login by the post-login hook chain after
        ``init_user_rights`` and ``derive_rights`` succeed.
        """
        rights_json = json.dumps(rights_to_dict(rights))
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "UPDATE sessions SET rights_json = ? WHERE id = ?",
                (rights_json, session_id),
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
