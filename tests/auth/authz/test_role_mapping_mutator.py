"""RoleMappingStore.for_session(...) returns a session-scoped mutator that
re-checks role membership before each mutation.

Defense-in-depth: even if a future admin route forgets the
require_role(...) gate, the mutator catches the call.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from iris.auth.authz.store import RoleMappingStore
from iris.auth.exceptions import AuthForbidden
from iris.auth.identity import User
from iris.auth.session import Session


def _session(*, roles: frozenset[str]) -> Session:
    user = User(subject="x", username="alice", display_name="Alice", groups=())
    now = datetime.now(UTC)
    return Session(
        id="sid",
        user=user,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        data={},
        roles=roles,
    )


class _NoSeed:
    bootstrap_role = "admin"
    bootstrap_user = None


@pytest.fixture
def store(tmp_path: Path):
    s = RoleMappingStore(path=str(tmp_path / "auth.db"))
    s.bootstrap(_NoSeed())
    yield s
    asyncio.run(s.close())


def test_for_session_admin_can_add_role(store):
    session = _session(roles=frozenset({"admin"}))
    mutator = store.for_session(session)
    asyncio.run(mutator.add_role("reader"))
    mapping = asyncio.run(store.get_mapping())
    assert "reader" in mapping.roles


def test_for_session_non_admin_blocked(store):
    session = _session(roles=frozenset({"reader"}))  # not admin
    mutator = store.for_session(session)
    with pytest.raises(AuthForbidden):
        asyncio.run(mutator.add_role("any"))
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles == {}


def test_for_session_no_roles_blocked(store):
    session = _session(roles=frozenset())
    mutator = store.for_session(session)
    with pytest.raises(AuthForbidden):
        asyncio.run(mutator.add_role("any"))


def test_for_session_custom_required_role(store):
    """Operators with a non-default authz-admin role pass it explicitly."""
    session = _session(roles=frozenset({"superuser"}))
    mutator = store.for_session(session, required_role="superuser")
    asyncio.run(mutator.add_role("reader"))
    mapping = asyncio.run(store.get_mapping())
    assert "reader" in mapping.roles


def test_for_session_remove_role_blocked(store):
    asyncio.run(store.add_role("reader"))
    session = _session(roles=frozenset({"reader"}))  # not admin
    mutator = store.for_session(session)
    with pytest.raises(AuthForbidden):
        asyncio.run(mutator.remove_role("reader"))
    mapping = asyncio.run(store.get_mapping())
    assert "reader" in mapping.roles  # still present


def test_for_session_all_mutators_blocked_for_non_admin(store):
    asyncio.run(store.add_role("reader"))
    asyncio.run(store.add_role("writer"))
    session = _session(roles=frozenset())
    mutator = store.for_session(session)

    with pytest.raises(AuthForbidden):
        asyncio.run(mutator.add_role("x"))
    with pytest.raises(AuthForbidden):
        asyncio.run(mutator.remove_role("x"))
    with pytest.raises(AuthForbidden):
        asyncio.run(mutator.add_group_to_role("reader", "g"))
    with pytest.raises(AuthForbidden):
        asyncio.run(mutator.remove_group_from_role("reader", "g"))
    with pytest.raises(AuthForbidden):
        asyncio.run(mutator.add_user_to_role("reader", "alice"))
    with pytest.raises(AuthForbidden):
        asyncio.run(mutator.remove_user_from_role("reader", "alice"))
    with pytest.raises(AuthForbidden):
        asyncio.run(mutator.add_include("writer", "reader"))
    with pytest.raises(AuthForbidden):
        asyncio.run(mutator.remove_include("writer", "reader"))
