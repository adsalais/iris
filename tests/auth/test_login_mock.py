import pytest
from fastapi.testclient import TestClient

from iris.auth.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD


@pytest.fixture
def client():
    from iris.app import build_app

    return TestClient(build_app(install_clickhouse=False))


def test_get_login_returns_form_with_csrf(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "<form" in r.text
    assert CSRF_COOKIE_NAME in r.cookies


def test_post_login_with_valid_creds_creates_session(client):
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    r = client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "alice",
            "password": "secret",
            "next": "/",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    assert "iris_session" in r.cookies


def test_post_login_with_bad_creds_redirects_with_error(client):
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    r = client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "alice",
            "password": "wrong",
            "next": "/",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login?error=invalid_credentials")


def test_whoami_returns_user_after_login(client):
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "alice",
            "password": "secret",
            "next": "/",
        },
    )
    r = client.get("/api/whoami")
    assert r.status_code == 200
    assert r.json() == {
        "subject": "mock:alice",
        "display_name": "Alice",
        "groups": ["admins", "users"],
        "rights": {
            "is_admin": False,
            "can_create_database": False,
            "db_admin": [],
            "db_writer": [],
            "db_reader": [],
        },
    }


def test_whoami_without_session_returns_401(client):
    r = client.get("/api/whoami", headers={"accept": "application/json"})
    assert r.status_code == 401


def test_post_login_protocol_relative_next_falls_back_to_root(client):
    """Open-redirect protection: //evil.com is rejected, redirect lands at /."""
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    r = client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "alice",
            "password": "secret",
            "next": "//evil.com/phish",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/"


def test_post_login_absolute_url_next_falls_back_to_root(client):
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    r = client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "alice",
            "password": "secret",
            "next": "https://evil.com/phish",
        },
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


def test_post_login_backslash_next_falls_back_to_root(client):
    """Browsers can normalize \\ -> / before same-origin check; reject."""
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    r = client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "alice",
            "password": "secret",
            "next": "/\\evil.com/phish",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/"


def test_post_login_failure_redirect_url_encoded(client):
    """The failure-redirect URL must be properly url-encoded."""
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    r = client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "alice",
            "password": "wrong",
            "next": "/dashboard?tab=home",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    location = r.headers["location"]
    # Must be parseable; ?tab=home in next should be URL-encoded inside the query
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(location)
    assert parsed.path == "/login"
    qs = parse_qs(parsed.query)
    assert qs.get("error") == ["invalid_credentials"]
    assert qs.get("next") == ["/dashboard?tab=home"]


def test_csrf_token_rotates_after_login(client):
    """A CSRF token captured pre-login must be invalidated after successful auth."""
    # Capture the CSRF token from the login form render
    r = client.get("/login")
    pre_token = r.cookies[CSRF_COOKIE_NAME]
    assert pre_token

    # Successfully log in (which should rotate the CSRF cookie)
    client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: pre_token,
            "username": "alice",
            "password": "secret",
            "next": "/",
        },
    )

    # The pre-login token must no longer satisfy CSRF on a state-changing request.
    # We test against /logout (which is CSRF-protected and requires auth — both
    # conditions are met now). Use the captured pre_token as the form field.
    r = client.post(
        "/logout",
        data={CSRF_FORM_FIELD: pre_token},
        follow_redirects=False,
    )
    # Either 400 (csrf_mismatch — cookie has rotated) or some other rejection.
    # The key assertion: the OLD token does NOT successfully log the user out.
    assert r.status_code == 400, (
        f"Expected 400 csrf_mismatch after rotation; got {r.status_code}. "
        "If this is 303, the rotation isn't happening."
    )


def test_failed_login_logs_attempt(client, caplog):
    """A failed POST /login should emit an INFO log with username + remote addr."""
    import logging
    caplog.set_level(logging.INFO, logger="iris.auth")

    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "alice",
            "password": "wrong",
            "next": "/",
        },
    )

    matching = [
        rec for rec in caplog.records
        if "auth: login_failed" in rec.message
        and "alice" in rec.message
    ]
    assert matching, (
        "expected one INFO log line containing 'auth: login_failed' and 'alice'; "
        f"got: {[r.message for r in caplog.records]}"
    )


def test_successful_login_does_not_log_failed(client, caplog):
    """Sanity: a successful login does NOT emit the failure log line."""
    import logging
    caplog.set_level(logging.INFO, logger="iris.auth")

    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "alice",
            "password": "secret",
            "next": "/",
        },
    )

    failures = [r for r in caplog.records if "auth: login_failed" in r.message]
    assert not failures, f"successful login should not emit login_failed; got {failures}"


def test_login_rejects_oversized_username(client):
    """Usernames over 64 chars are rejected at the HTTP layer with 422."""
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    r = client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "a" * 65,
            "password": "secret",
            "next": "/",
        },
        follow_redirects=False,
    )
    assert r.status_code == 422


def test_login_rejects_oversized_password(client):
    """Passwords over 4096 chars are rejected at the HTTP layer with 422."""
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    r = client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "alice",
            "password": "a" * 4097,
            "next": "/",
        },
        follow_redirects=False,
    )
    assert r.status_code == 422


def test_login_rejects_oversized_next(client):
    """next param over 2048 chars is rejected at the HTTP layer with 422."""
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    r = client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "alice",
            "password": "secret",
            "next": "/" + "a" * 2048,
        },
        follow_redirects=False,
    )
    assert r.status_code == 422
