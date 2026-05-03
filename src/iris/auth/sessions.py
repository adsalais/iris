from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, UTC

from iris.auth.identity import User, UserSession


class InMemorySessionStore:
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._sessions: dict[str, UserSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, user: User) -> UserSession:
        async with self._lock:
            now = datetime.now(UTC)
            session = UserSession(
                id=secrets.token_urlsafe(32),
                user=user,
                created_at=now,
                expires_at=now + self._ttl,
            )
            self._sessions[session.id] = session
            return session

    async def get_and_refresh(self, session_id: str) -> UserSession | None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            now = datetime.now(UTC)
            if session.expires_at <= now:
                del self._sessions[session_id]
                return None
            session.expires_at = now + self._ttl
            return session

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)
