import asyncio
from datetime import datetime, timedelta, UTC

import pytest

from iris.auth.identity import User
from iris.auth.sessions import InMemorySessionStore


@pytest.fixture
def user() -> User:
    return User(subject="alice", display_name="Alice", groups=("admins",))


def test_create_returns_session_with_id(user):
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600)
    session = asyncio.run(store.create(user))
    assert isinstance(session.id, str)
    assert len(session.id) >= 32
    assert session.user is user


def test_get_returns_session_when_present(user):
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600)
    session = asyncio.run(store.create(user))
    fetched = asyncio.run(store.get_and_refresh(session.id))
    assert fetched is not None
    assert fetched.id == session.id


def test_get_returns_none_when_absent():
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600)
    assert asyncio.run(store.get_and_refresh("not-a-real-id")) is None


def test_get_refreshes_expires_at(user):
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600)
    session = asyncio.run(store.create(user))
    original = session.expires_at
    asyncio.run(asyncio.sleep(0.01))  # ensure clock moves
    fetched = asyncio.run(store.get_and_refresh(session.id))
    assert fetched is not None
    assert fetched.expires_at > original


def test_get_evicts_expired_session(user):
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600)
    session = asyncio.run(store.create(user))
    # forcibly age the entry
    object.__setattr__(session, "expires_at", datetime.now(UTC) - timedelta(seconds=1))
    fetched = asyncio.run(store.get_and_refresh(session.id))
    assert fetched is None
    # second lookup confirms it was evicted
    fetched_again = asyncio.run(store.get_and_refresh(session.id))
    assert fetched_again is None


def test_delete_removes_session(user):
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600)
    session = asyncio.run(store.create(user))
    asyncio.run(store.delete(session.id))
    assert asyncio.run(store.get_and_refresh(session.id)) is None


def test_delete_unknown_id_is_noop():
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600)
    asyncio.run(store.delete("not-a-real-id"))  # must not raise


def test_session_ids_are_unique(user):
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600)
    s1 = asyncio.run(store.create(user))
    s2 = asyncio.run(store.create(user))
    assert s1.id != s2.id


def test_absolute_expiry_overrides_sliding_refresh(user):
    """Even with sliding refresh, the absolute deadline kicks in."""
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=120)
    session = asyncio.run(store.create(user))
    # Forcibly age the absolute deadline into the past
    object.__setattr__(session, "absolute_expires_at", datetime.now(UTC) - timedelta(seconds=1))
    # The sliding expires_at is still in the future, but absolute_expires_at is past.
    assert asyncio.run(store.get_and_refresh(session.id)) is None
    # Second lookup confirms eviction
    assert asyncio.run(store.get_and_refresh(session.id)) is None


def test_absolute_expiry_preserves_session_until_deadline(user):
    """Sessions remain valid (and refreshable) until the absolute deadline."""
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=120)
    session = asyncio.run(store.create(user))
    fetched = asyncio.run(store.get_and_refresh(session.id))
    assert fetched is not None
    # absolute_expires_at should NOT be refreshed by get_and_refresh
    assert fetched.absolute_expires_at == session.absolute_expires_at


def test_create_evicts_oldest_when_cap_exceeded(user):
    """Creating more than max_per_user sessions for the same subject evicts oldest."""
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600, max_per_user=3)
    sessions = [asyncio.run(store.create(user)) for _ in range(5)]
    # Only the last 3 should still be present
    assert asyncio.run(store.get_and_refresh(sessions[0].id)) is None
    assert asyncio.run(store.get_and_refresh(sessions[1].id)) is None
    assert asyncio.run(store.get_and_refresh(sessions[2].id)) is not None
    assert asyncio.run(store.get_and_refresh(sessions[3].id)) is not None
    assert asyncio.run(store.get_and_refresh(sessions[4].id)) is not None


def test_cap_is_per_user_not_global(user):
    """Different subjects don't share the cap."""
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600, max_per_user=2)
    other = User(subject="bob", display_name="Bob", groups=())
    a1 = asyncio.run(store.create(user))
    a2 = asyncio.run(store.create(user))
    b1 = asyncio.run(store.create(other))
    b2 = asyncio.run(store.create(other))
    # All 4 still present (2 per subject)
    assert asyncio.run(store.get_and_refresh(a1.id)) is not None
    assert asyncio.run(store.get_and_refresh(a2.id)) is not None
    assert asyncio.run(store.get_and_refresh(b1.id)) is not None
    assert asyncio.run(store.get_and_refresh(b2.id)) is not None


def test_default_max_per_user_is_ten(user):
    """The default cap is 10 when not specified."""
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600)
    sessions = [asyncio.run(store.create(user)) for _ in range(11)]
    # First should have been evicted; last 10 present
    assert asyncio.run(store.get_and_refresh(sessions[0].id)) is None
    for s in sessions[1:]:
        assert asyncio.run(store.get_and_refresh(s.id)) is not None
