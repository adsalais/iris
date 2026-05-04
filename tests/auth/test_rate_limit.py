import pytest
from fastapi.testclient import TestClient

from iris.auth.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD


@pytest.fixture
def client():
    from iris.app import build_app

    return TestClient(build_app())


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
