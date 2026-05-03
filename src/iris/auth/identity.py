from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class User:
    subject: str
    display_name: str
    groups: tuple[str, ...]


@dataclass(slots=True)
class UserSession:
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
