import pytest
from fastapi.testclient import TestClient

from iris.auth.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD


@pytest.fixture
def client():
    from iris.app import build_app

    return TestClient(build_app(install_clickhouse=False))


def test_login_rate_limit_kicks_in_on_burst(client):
    """After capacity-many rapid POSTs to /login, the next one returns 429."""
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    body = {CSRF_FORM_FIELD: csrf, "username": "alice", "password": "wrong", "next": "/"}
    last_status = None
    for _ in range(15):  # past the 10/burst capacity
        last_status = client.post("/login", data=body, follow_redirects=False).status_code
    assert last_status == 429


def test_login_rate_limit_response_has_retry_after(client):
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    body = {CSRF_FORM_FIELD: csrf, "username": "alice", "password": "wrong", "next": "/"}
    # Exhaust the bucket
    last = None
    for _ in range(15):
        last = client.post("/login", data=body, follow_redirects=False)
    assert last is not None
    assert last.status_code == 429
    retry_after = last.headers.get("Retry-After", "")
    assert retry_after.isdigit() and int(retry_after) >= 1


def test_token_bucket_allows_capacity_then_blocks():
    """Unit test the bucket directly: 10 immediate takes succeed, 11th waits."""
    from iris.auth.rate_limit import TokenBucket
    bucket = TokenBucket(capacity=10, refill_per_second=0.2)
    for _ in range(10):
        assert bucket.take("k") is None
    wait = bucket.take("k")
    assert wait is not None and wait > 0


def test_token_bucket_keys_are_isolated():
    """Different keys have separate buckets."""
    from iris.auth.rate_limit import TokenBucket
    bucket = TokenBucket(capacity=2, refill_per_second=0.1)
    assert bucket.take("a") is None
    assert bucket.take("a") is None
    assert bucket.take("a") is not None  # exhausted
    assert bucket.take("b") is None  # fresh bucket


def test_lru_evicts_oldest_when_capacity_exceeded():
    """Inserting _MAX_BUCKETS + 1 distinct keys evicts key #0."""
    from iris.auth.rate_limit import TokenBucket, _MAX_BUCKETS

    bucket = TokenBucket(capacity=10, refill_per_second=1.0)
    for i in range(_MAX_BUCKETS + 1):
        bucket.take(f"k{i}")

    assert "k0" not in bucket._buckets, "oldest key should have been evicted"
    assert f"k{_MAX_BUCKETS}" in bucket._buckets, "newest key should be present"
    assert len(bucket._buckets) == _MAX_BUCKETS, "size capped at _MAX_BUCKETS"


def test_returning_key_is_promoted_to_mru():
    """Re-taking a key bumps it to MRU; a subsequent overflow evicts the
    next-oldest, not the original key."""
    from iris.auth.rate_limit import TokenBucket, _MAX_BUCKETS

    bucket = TokenBucket(capacity=10, refill_per_second=1.0)
    for i in range(_MAX_BUCKETS):
        bucket.take(f"k{i}")
    # k0 is currently the LRU. Re-take it to promote.
    bucket.take("k0")
    # Now insert one more key, forcing one eviction.
    bucket.take(f"k{_MAX_BUCKETS}")

    assert "k0" in bucket._buckets, "k0 was promoted to MRU and must survive"
    assert "k1" not in bucket._buckets, "k1 was the new LRU and should be evicted"


def test_evicted_key_starts_with_full_bucket_on_re_insert():
    """An evicted key, on re-insert, gets a fresh full-capacity bucket."""
    from iris.auth.rate_limit import TokenBucket, _MAX_BUCKETS

    # refill_per_second is tiny but non-zero so the wait-time math doesn't
    # divide by zero; effectively no refill within the test's runtime.
    bucket = TokenBucket(capacity=10, refill_per_second=0.001)
    # Drain k0
    for _ in range(10):
        assert bucket.take("k0") is None
    assert bucket.take("k0") is not None  # exhausted

    # Spam other keys until k0 is evicted
    for i in range(1, _MAX_BUCKETS + 1):
        bucket.take(f"k{i}")
    assert "k0" not in bucket._buckets

    # Re-insert k0 — should get a fresh full bucket
    assert bucket.take("k0") is None  # capacity-many fresh tokens available
