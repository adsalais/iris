import pytest
from fastapi.testclient import TestClient

from iris.auth.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD


@pytest.fixture
def client():
    from iris.app import build_app

    return TestClient(build_app())


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


def test_post_login_with_oauth_method_returns_405(monkeypatch):
    """When AUTH_METHOD=oauth, POST /login is not a valid path (callback is)."""
    pytest.skip(
        "Building an oauth-mode app requires real OIDC discovery network call; "
        "covered indirectly by tests/auth/test_provider_oauth.py via _http_transport injection."
    )
