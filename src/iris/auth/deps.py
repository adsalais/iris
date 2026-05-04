from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request

from iris.auth.authz.core import CurrentMapping, resolve_roles
from iris.auth.exceptions import AuthRequired
from iris.auth.identity import User, UserSession
from iris.auth.session import Session as _SessionT
from iris.auth.sessions import InMemorySessionStore


def set_session_store(app: FastAPI, store: InMemorySessionStore) -> None:
    app.state.auth_session_store = store


def set_settings(app: FastAPI, *, cookie_name: str, cookie_secure: bool = True) -> None:
    app.state.auth_cookie_name = cookie_name
    app.state.auth_cookie_secure = cookie_secure


def _get_store(request: Request) -> InMemorySessionStore:
    return request.app.state.auth_session_store


def _get_cookie_name(request: Request) -> str:
    return request.app.state.auth_cookie_name


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def _resolve_session(request: Request) -> UserSession | None:
    cookie_name = _get_cookie_name(request)
    sid = request.cookies.get(cookie_name) or _bearer(
        request.headers.get("authorization")
    )
    if not sid:
        return None
    store = _get_store(request)
    return await store.get_and_refresh(sid)


_ResolvedSession = Annotated[UserSession | None, Depends(_resolve_session)]


async def _required_session(session: _ResolvedSession) -> UserSession:
    if session is None:
        raise AuthRequired()
    return session


_RequiredSession = Annotated[UserSession, Depends(_required_session)]


# --- Old surface (will be removed in Task 11) ---------------------------------


async def _current_user(session: _RequiredSession) -> User:
    return session.user


async def _optional_current_user(session: _ResolvedSession) -> User | None:
    return session.user if session else None


async def _current_session(session: _RequiredSession) -> UserSession:
    return session


async def _session_data(session: _RequiredSession) -> dict[str, Any]:
    return session.data


CurrentUser = Annotated[User, Depends(_current_user)]
OptionalCurrentUser = Annotated[User | None, Depends(_optional_current_user)]
CurrentSession = Annotated[UserSession, Depends(_current_session)]
SessionData = Annotated[dict[str, Any], Depends(_session_data)]


# --- New surface --------------------------------------------------------------


async def _build_optional(
    stored: _ResolvedSession,
    mapping: CurrentMapping,
) -> _SessionT | None:
    if stored is None:
        return None
    return _SessionT(
        id=stored.id,
        user=stored.user,
        created_at=stored.created_at,
        expires_at=stored.expires_at,
        data=stored.data,
        roles=resolve_roles(stored.user, mapping),
    )


_BuiltOptional = Annotated[_SessionT | None, Depends(_build_optional)]


async def _build_required(view: _BuiltOptional) -> _SessionT:
    if view is None:
        raise AuthRequired()
    return view


Session = Annotated[_SessionT, Depends(_build_required)]
OptionalSession = Annotated[_SessionT | None, Depends(_build_optional)]
