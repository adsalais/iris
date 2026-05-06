"""DatabaseAdminStore.for_session(...) returns a session-bound,
database-scoped mutator that re-checks admin authority before each
mutation. Defense-in-depth: even if a future admin route forgets the
require_clickhouse_database_admin gate, the mutator catches the call.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from iris.auth.exceptions import AuthForbidden
from iris.auth.identity import User
from iris.auth.session import SessionView
from iris.clickhouse.database_admins import DatabaseAdminStore


def _session(*, username: str = "alice", roles: frozenset[str] = frozenset()) -> SessionView:
    user = User(subject="x", username=username, display_name=username.title(), groups=())
    now = datetime.now(UTC)
    return SessionView(
        id="sid",
        user=user,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        data={},
        roles=roles,
    )


@pytest.fixture
def store(tmp_path: Path):
    s = DatabaseAdminStore(path=str(tmp_path / "auth.db"))
    s.bootstrap()
    yield s
    asyncio.run(s.close())


def test_for_session_blocks_non_admin(store):
    """User who is not in the per-DB admins table can't mutate."""
    session = _session(username="dave", roles=frozenset())
    mutator = store.for_session(session, database="orders")
    with pytest.raises(AuthForbidden):
        asyncio.run(mutator.add_admin_user("eve"))


def test_for_session_admits_listed_user(store):
    asyncio.run(store.add_admin_user(database="orders", username="alice"))
    session = _session(username="alice", roles=frozenset())
    mutator = store.for_session(session, database="orders")
    asyncio.run(mutator.add_admin_user("bob"))  # alice is admin → permitted
    assert "bob" in asyncio.run(store.list_admin_users(database="orders"))


def test_for_session_short_circuits_global_admin(store):
    """clickhouse_admin in roles → admin of every database without per-DB rows."""
    session = _session(username="anyone", roles=frozenset({"clickhouse_admin"}))
    mutator = store.for_session(session, database="any_db")
    asyncio.run(mutator.add_admin_user("eve"))
    assert "eve" in asyncio.run(store.list_admin_users(database="any_db"))


def test_for_session_isolates_per_database(store):
    """alice is admin of orders, NOT reports."""
    asyncio.run(store.add_admin_user(database="orders", username="alice"))
    session = _session(username="alice", roles=frozenset())
    mutator_other = store.for_session(session, database="reports")
    with pytest.raises(AuthForbidden):
        asyncio.run(mutator_other.add_admin_user("bob"))


def test_for_session_role_match_admits(store):
    """User with a role that's listed for the DB is admitted."""
    asyncio.run(store.add_admin_role(database="orders", role="ops"))
    session = _session(username="charlie", roles=frozenset({"ops"}))
    mutator = store.for_session(session, database="orders")
    asyncio.run(mutator.add_admin_user("eve"))
    assert "eve" in asyncio.run(store.list_admin_users(database="orders"))


def test_for_session_listing_methods_also_gated(store):
    """list_admin_* on the mutator also re-check (audit info isn't free)."""
    session = _session(username="dave", roles=frozenset())
    mutator = store.for_session(session, database="orders")
    with pytest.raises(AuthForbidden):
        asyncio.run(mutator.list_admin_users())
    with pytest.raises(AuthForbidden):
        asyncio.run(mutator.list_admin_roles())
