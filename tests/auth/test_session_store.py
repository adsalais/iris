import asyncio
import time
from pathlib import Path

import pytest

from iris.auth.identity import User
from iris.auth.store import SessionStore


@pytest.fixture
def user() -> User:
    return User(subject="alice", username="alice", display_name="Alice", groups=("admins",))


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "sessions.db"


@pytest.fixture
def store(store_path):
    s = SessionStore(
        path=str(store_path),
        ttl_seconds=60,
        absolute_ttl_seconds=3600,
    )
    yield s
    asyncio.run(s.close())


def test_create_returns_session_with_id(store, user):
    session = asyncio.run(store.create(user))
    assert isinstance(session.id, str)
    assert len(session.id) >= 32
    assert session.user == user


def test_get_returns_session_when_present(store, user):
    session = asyncio.run(store.create(user))
    fetched = asyncio.run(store.get_and_refresh(session.id))
    assert fetched is not None
    assert fetched.id == session.id
    assert fetched.user == user


def test_get_returns_none_when_absent(store):
    assert asyncio.run(store.get_and_refresh("not-a-real-id")) is None


def test_get_refreshes_expires_at(store, user):
    session = asyncio.run(store.create(user))
    original = session.expires_at
    asyncio.run(asyncio.sleep(1.1))  # SECOND-resolution timestamps need >=1s gap
    fetched = asyncio.run(store.get_and_refresh(session.id))
    assert fetched is not None
    assert fetched.expires_at > original


def test_get_evicts_expired_session(store_path, user):
    s = SessionStore(path=str(store_path), ttl_seconds=1, absolute_ttl_seconds=3600)
    try:
        session = asyncio.run(s.create(user))
        asyncio.run(asyncio.sleep(2))  # let TTL elapse
        assert asyncio.run(s.get_and_refresh(session.id)) is None
        # Second lookup confirms eviction
        assert asyncio.run(s.get_and_refresh(session.id)) is None
    finally:
        asyncio.run(s.close())


def test_get_evicts_when_absolute_expired(store_path, user):
    s = SessionStore(path=str(store_path), ttl_seconds=3600, absolute_ttl_seconds=1)
    try:
        session = asyncio.run(s.create(user))
        asyncio.run(asyncio.sleep(2))  # absolute TTL elapses
        assert asyncio.run(s.get_and_refresh(session.id)) is None
    finally:
        asyncio.run(s.close())


def test_delete_removes_session(store, user):
    session = asyncio.run(store.create(user))
    asyncio.run(store.delete(session.id))
    assert asyncio.run(store.get_and_refresh(session.id)) is None


def test_delete_unknown_id_is_noop(store):
    asyncio.run(store.delete("not-a-real-id"))  # must not raise


def test_session_ids_are_unique(store, user):
    s1 = asyncio.run(store.create(user))
    s2 = asyncio.run(store.create(user))
    assert s1.id != s2.id


def test_user_groups_round_trip(store_path):
    s = SessionStore(path=str(store_path), ttl_seconds=60, absolute_ttl_seconds=3600)
    try:
        u = User(subject="x", username="x", display_name="X", groups=("a", "b", "c"))
        sess = asyncio.run(s.create(u))
        fetched = asyncio.run(s.get_and_refresh(sess.id))
        assert fetched is not None
        assert fetched.user.groups == ("a", "b", "c")
    finally:
        asyncio.run(s.close())


def test_create_evicts_oldest_when_cap_exceeded(store_path, user):
    s = SessionStore(
        path=str(store_path),
        ttl_seconds=60,
        absolute_ttl_seconds=3600,
        max_per_user=3,
    )
    try:
        sessions = []
        for _ in range(5):
            sessions.append(asyncio.run(s.create(user)))
            # SQLite stores INTEGER seconds — without a sleep, two sessions can
            # share created_at_ts and the eviction order becomes undefined.
            time.sleep(1.05)
        assert asyncio.run(s.get_and_refresh(sessions[0].id)) is None
        assert asyncio.run(s.get_and_refresh(sessions[1].id)) is None
        assert asyncio.run(s.get_and_refresh(sessions[2].id)) is not None
        assert asyncio.run(s.get_and_refresh(sessions[3].id)) is not None
        assert asyncio.run(s.get_and_refresh(sessions[4].id)) is not None
    finally:
        asyncio.run(s.close())


def test_cap_is_per_user_not_global(store_path):
    s = SessionStore(
        path=str(store_path),
        ttl_seconds=60,
        absolute_ttl_seconds=3600,
        max_per_user=2,
    )
    try:
        alice = User(subject="alice", username="alice", display_name="A", groups=())
        bob = User(subject="bob", username="bob", display_name="B", groups=())
        a1 = asyncio.run(s.create(alice))
        a2 = asyncio.run(s.create(alice))
        b1 = asyncio.run(s.create(bob))
        b2 = asyncio.run(s.create(bob))
        for sid in (a1.id, a2.id, b1.id, b2.id):
            assert asyncio.run(s.get_and_refresh(sid)) is not None
    finally:
        asyncio.run(s.close())


def test_update_data_round_trips(store, user):
    session = asyncio.run(store.create(user))
    asyncio.run(store.update_data(session.id, {"key": "value", "n": 42}))
    fetched = asyncio.run(store.get_and_refresh(session.id))
    assert fetched is not None
    assert fetched.data == {"key": "value", "n": 42}


def test_update_data_preserves_nested_types(store, user):
    session = asyncio.run(store.create(user))
    payload = {
        "list": [1, 2, 3],
        "dict": {"nested": True, "deep": {"x": None}},
        "float": 1.5,
        "bool": False,
        "null": None,
    }
    asyncio.run(store.update_data(session.id, payload))
    fetched = asyncio.run(store.get_and_refresh(session.id))
    assert fetched is not None
    assert fetched.data == payload


def test_update_data_rejects_non_json_encodable(store, user):
    session = asyncio.run(store.create(user))

    class Opaque:
        pass

    with pytest.raises(TypeError):
        asyncio.run(store.update_data(session.id, {"x": Opaque()}))


def test_update_data_unknown_id_is_noop(store):
    asyncio.run(store.update_data("not-a-real-id", {"x": 1}))  # must not raise


def test_persistence_across_reopen(store_path, user):
    """Closing and reopening the store sees existing sessions — the multi-worker payoff."""
    s1 = SessionStore(path=str(store_path), ttl_seconds=60, absolute_ttl_seconds=3600)
    try:
        session = asyncio.run(s1.create(user))
        asyncio.run(s1.update_data(session.id, {"k": "v"}))
        sid = session.id
    finally:
        asyncio.run(s1.close())

    s2 = SessionStore(path=str(store_path), ttl_seconds=60, absolute_ttl_seconds=3600)
    try:
        fetched = asyncio.run(s2.get_and_refresh(sid))
        assert fetched is not None
        assert fetched.data == {"k": "v"}
    finally:
        asyncio.run(s2.close())


def test_close_is_idempotent(store_path, user):
    s = SessionStore(path=str(store_path), ttl_seconds=60, absolute_ttl_seconds=3600)
    asyncio.run(s.close())
    asyncio.run(s.close())  # must not raise
