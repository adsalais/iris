import asyncio
from datetime import datetime, timedelta, UTC

import pytest

from iris.auth.identity import User
from iris.auth.sessions import InMemorySessionStore


@pytest.fixture
def user() -> User:
    return User(subject="alice", display_name="Alice", groups=("admins",))


def test_create_returns_session_with_id(user):
    store = InMemorySessionStore(ttl_seconds=60)
    session = asyncio.run(store.create(user))
    assert isinstance(session.id, str)
    assert len(session.id) >= 32
    assert session.user is user


def test_get_returns_session_when_present(user):
    store = InMemorySessionStore(ttl_seconds=60)
    session = asyncio.run(store.create(user))
    fetched = asyncio.run(store.get_and_refresh(session.id))
    assert fetched is not None
    assert fetched.id == session.id


def test_get_returns_none_when_absent():
    store = InMemorySessionStore(ttl_seconds=60)
    assert asyncio.run(store.get_and_refresh("not-a-real-id")) is None


def test_get_refreshes_expires_at(user):
    store = InMemorySessionStore(ttl_seconds=60)
    session = asyncio.run(store.create(user))
    original = session.expires_at
    asyncio.run(asyncio.sleep(0.01))  # ensure clock moves
    fetched = asyncio.run(store.get_and_refresh(session.id))
    assert fetched is not None
    assert fetched.expires_at > original


def test_get_evicts_expired_session(user):
    store = InMemorySessionStore(ttl_seconds=60)
    session = asyncio.run(store.create(user))
    # forcibly age the entry
    object.__setattr__(session, "expires_at", datetime.now(UTC) - timedelta(seconds=1))
    fetched = asyncio.run(store.get_and_refresh(session.id))
    assert fetched is None
    # second lookup confirms it was evicted
    fetched_again = asyncio.run(store.get_and_refresh(session.id))
    assert fetched_again is None


def test_delete_removes_session(user):
    store = InMemorySessionStore(ttl_seconds=60)
    session = asyncio.run(store.create(user))
    asyncio.run(store.delete(session.id))
    assert asyncio.run(store.get_and_refresh(session.id)) is None


def test_delete_unknown_id_is_noop():
    store = InMemorySessionStore(ttl_seconds=60)
    asyncio.run(store.delete("not-a-real-id"))  # must not raise


def test_session_ids_are_unique(user):
    store = InMemorySessionStore(ttl_seconds=60)
    s1 = asyncio.run(store.create(user))
    s2 = asyncio.run(store.create(user))
    assert s1.id != s2.id
