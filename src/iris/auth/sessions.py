from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, UTC

from iris.auth.identity import User, UserSession


class InMemorySessionStore:
    def __init__(
        self,
        ttl_seconds: int,
        absolute_ttl_seconds: int,
        max_per_user: int = 10,
    ) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._absolute_ttl = timedelta(seconds=absolute_ttl_seconds)
        self._max_per_user = max_per_user
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
                absolute_expires_at=now + self._absolute_ttl,
            )
            self._sessions[session.id] = session
            self._evict_excess_for_subject(user.subject)
            return session

    def _evict_excess_for_subject(self, subject: str) -> None:
        """Keep only the newest `max_per_user` sessions for the given subject."""
        user_sessions = [
            s for s in self._sessions.values() if s.user.subject == subject
        ]
        if len(user_sessions) <= self._max_per_user:
            return
        # Sort oldest first, drop the excess
        user_sessions.sort(key=lambda s: s.created_at)
        excess = len(user_sessions) - self._max_per_user
        for s in user_sessions[:excess]:
            del self._sessions[s.id]

    async def get_and_refresh(self, session_id: str) -> UserSession | None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            now = datetime.now(UTC)
            if session.expires_at <= now or session.absolute_expires_at <= now:
                del self._sessions[session_id]
                return None
            session.expires_at = now + self._ttl
            return session

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)
