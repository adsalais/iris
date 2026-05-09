from __future__ import annotations

from iris.auth.deps import (
    Session,
    SessionAdmin,
    SessionDatabaseAdmin,
    SessionDatabaseCreator,
    SessionOptional,
    SessionRead,
    SessionWrite,
)
from iris.auth.identity import User
from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
from iris.auth.routes import install
from iris.auth.views import (
    AdminSession,
    AuthSession,
    DatabaseAdminSession,
    DatabaseCreatorSession,
    DatabaseSession,
)

__all__ = [
    "AdminSession",
    "AuthSession",
    "Capabilities",
    "DatabaseAdminSession",
    "DatabaseCreatorSession",
    "DatabaseSession",
    "EMPTY_CAPABILITIES",
    "Session",
    "SessionAdmin",
    "SessionDatabaseAdmin",
    "SessionDatabaseCreator",
    "SessionOptional",
    "SessionRead",
    "SessionWrite",
    "User",
    "install",
]
