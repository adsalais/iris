import pytest
from fastapi.testclient import TestClient

from iris.auth.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD


@pytest.fixture
def authed_client():
    from iris.app import build_app

    client = TestClient(build_app())
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
    return client


def test_logout_deletes_session_and_clears_cookie(authed_client):
    csrf = authed_client.cookies[CSRF_COOKIE_NAME]
    r = authed_client.post(
        "/logout",
        data={CSRF_FORM_FIELD: csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    set_cookie = r.headers.get("set-cookie", "").lower()
    assert "iris_session=" in set_cookie
    assert "max-age=0" in set_cookie
    # whoami must now 401
    r = authed_client.get("/api/whoami", headers={"accept": "application/json"})
    assert r.status_code == 401


def test_logout_without_csrf_rejected(authed_client):
    r = authed_client.post("/logout", data={}, follow_redirects=False)
    assert r.status_code == 400


def test_logout_without_session_returns_401_or_400():
    from iris.app import build_app

    client = TestClient(build_app())
    r = client.post("/logout", data={}, headers={"accept": "application/json"})
    # No CSRF cookie either, so 400 (CSRF mismatch) is the most likely outcome
    assert r.status_code in (400, 401)
