"""End-to-end integration test helpers.

Two helpers:

- ``login_as``: drives Keycloak OAuth login through the iris HTTP layer
  and returns the iris_session sid.
- ``session_for``: reconstitutes a typed Session subclass from the
  stored ``StoredSession``, mirroring what ``iris.auth.deps`` does inside
  an HTTP request. Raises ``AuthForbidden`` from the same code path the
  real deps would raise from when a user lacks the required capabilities.
"""
from __future__ import annotations

from typing import Literal

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iris.auth.exceptions import AuthForbidden
from iris.auth.views import (
    AdminSession,
    AuthSession,
    DatabaseAdminSession,
    DatabaseCreatorSession,
    DatabaseSession,
)
from iris.clickhouse.capabilities import derive_capabilities
from tests.auth.integration._keycloak_helpers import simulate_login

SessionKind = Literal[
    "auth",
    "admin",
    "database_creator",
    "database_admin",
    "database_writer",
    "database_reader",
]


# Many-typed table covering every leaf type the marshaller supports.
TABLE_DDL = """
CREATE TABLE `{db}`.records (
    id          UInt64,
    region      String,
    tags        Array(String),
    score       Float64,
    active      Bool,
    created_at  DateTime,
    measured_at DateTime64(3),
    birthday    Date,
    note        Nullable(String),
    counts      Array(Nullable(Int32))
) ENGINE = MergeTree ORDER BY id
"""


def login_as(
    *,
    test_client: TestClient,
    keycloak_http: httpx.Client,
    username: str,
    password: str,
) -> str:
    """Drive the full Keycloak login flow for ``username``; return the iris_session sid.

    Clears Keycloak cookies first so a previous user's SSO session
    doesn't short-circuit the login. Otherwise Keycloak 302s straight
    to ``http://testserver/login/callback`` from outside iris's
    TestClient — unresolvable from the real httpx client.
    """
    keycloak_http.cookies.clear()
    response = simulate_login(
        test_client=test_client,
        http=keycloak_http,
        username=username,
        password=password,
    )
    sid = response.cookies.get("iris_session")
    assert sid is not None, f"login for {username} did not set iris_session"
    return sid


async def refresh_capabilities(app: FastAPI, sid: str) -> None:
    """Re-derive the session's CH-side capabilities and persist them to the store.

    Mirrors what the post-login hook does. Use after CH-side state changes
    (e.g. a new tier grant) to refresh a logged-in user's view without
    forcing a second Keycloak round-trip.
    """
    store = app.state.auth_session_store
    stored = await store.get_and_refresh(sid)
    assert stored is not None, f"session {sid!r} not in store"
    client = app.state.clickhouse_client
    capabilities = derive_capabilities(
        client,
        username=stored.user.username,
        groups=list(stored.user.groups),
    )
    await store.set_capabilities(sid, capabilities)


async def session_for(
    app: FastAPI,
    sid: str,
    *,
    kind: SessionKind,
    database: str | None = None,
) -> AuthSession:
    """Reconstitute a typed Session subclass from the stored StoredSession.

    Mirrors what iris.auth.deps does inside an HTTP request, but callable
    from test bodies. Raises AuthForbidden from the same code path the
    real deps would raise from when the user lacks the required capabilities.
    """
    store = app.state.auth_session_store
    stored = await store.get_and_refresh(sid)
    assert stored is not None, f"session {sid!r} not in store (logged out?)"

    client = getattr(app.state, "clickhouse_client", None)
    http_client = getattr(app.state, "clickhouse_http_client", None)
    settings = getattr(app.state, "clickhouse_settings", None)
    capabilities = stored.capabilities

    if kind == "auth":
        return AuthSession(
            id=stored.id, user=stored.user,
            created_at=stored.created_at, expires_at=stored.expires_at,
            data=stored.data, capabilities=capabilities,
            client=client, http_client=http_client,
            settings=settings, store=store,
        )
    if kind == "admin":
        if not capabilities.is_admin:
            raise AuthForbidden(needed=("admin",), have=())
        return AdminSession(
            id=stored.id, user=stored.user,
            created_at=stored.created_at, expires_at=stored.expires_at,
            data=stored.data, capabilities=capabilities,
            client=client, http_client=http_client,
            settings=settings, store=store,
        )
    if kind == "database_creator":
        if not (capabilities.is_admin or capabilities.can_create_database):
            raise AuthForbidden(
                needed=("admin", "database_creator"), have=()
            )
        return DatabaseCreatorSession(
            id=stored.id, user=stored.user,
            created_at=stored.created_at, expires_at=stored.expires_at,
            data=stored.data, capabilities=capabilities,
            client=client, http_client=http_client,
            settings=settings, store=store,
        )
    assert database is not None, f"kind={kind} requires database="
    if kind == "database_admin":
        if not capabilities.has_admin(database):
            raise AuthForbidden(
                needed=(f"database_admin[{database}]",), have=()
            )
        return DatabaseAdminSession(
            id=stored.id, user=stored.user,
            created_at=stored.created_at, expires_at=stored.expires_at,
            data=stored.data, capabilities=capabilities,
            client=client, http_client=http_client,
            settings=settings, store=store,
            database=database,
        )
    if kind == "database_writer":
        if not capabilities.has_write(database):
            raise AuthForbidden(
                needed=(f"database_writer[{database}]",), have=()
            )
        return DatabaseSession(
            id=stored.id, user=stored.user,
            created_at=stored.created_at, expires_at=stored.expires_at,
            data=stored.data, capabilities=capabilities,
            client=client, http_client=http_client,
            settings=settings, store=store,
            database=database,
        )
    if kind == "database_reader":
        if not capabilities.has_read(database):
            raise AuthForbidden(
                needed=(f"database_reader[{database}]",), have=()
            )
        return DatabaseSession(
            id=stored.id, user=stored.user,
            created_at=stored.created_at, expires_at=stored.expires_at,
            data=stored.data, capabilities=capabilities,
            client=client, http_client=http_client,
            settings=settings, store=store,
            database=database,
        )
    raise ValueError(f"unknown kind: {kind}")  # pyright: ignore[reportUnreachable]
