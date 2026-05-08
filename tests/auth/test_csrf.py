import re

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from iris.auth.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_FORM_FIELD,
    issue_csrf_token,
    mint_csrf_token,
    verify_csrf_form,
)
from iris.auth.deps import set_settings


def _build_app(*, cookie_secure: bool = False) -> FastAPI:
    app = FastAPI()
    # cookie_secure=False so httpx test client (http://testserver/...) sends the cookie back
    set_settings(app, cookie_name="iris_session", cookie_secure=cookie_secure)

    @app.get("/form", response_class=HTMLResponse)
    async def form(token: str = Depends(issue_csrf_token)):
        return f'<form><input name="{CSRF_FORM_FIELD}" value="{token}"></form>'

    @app.post("/submit")
    async def submit(_: None = Depends(verify_csrf_form)):
        return {"ok": True}

    return app


def test_get_form_sets_csrf_cookie():
    client = TestClient(_build_app())
    r = client.get("/form")
    assert CSRF_COOKIE_NAME in r.cookies
    assert r.cookies[CSRF_COOKIE_NAME] in r.text


def test_post_with_matching_token_succeeds():
    client = TestClient(_build_app())
    r = client.get("/form")
    token = r.cookies[CSRF_COOKIE_NAME]
    r = client.post("/submit", data={CSRF_FORM_FIELD: token})
    assert r.status_code == 200


def test_post_with_missing_form_field_rejected():
    client = TestClient(_build_app())
    client.get("/form")
    r = client.post("/submit", data={})
    assert r.status_code == 400


def test_post_with_mismatched_token_rejected():
    client = TestClient(_build_app())
    client.get("/form")
    r = client.post("/submit", data={CSRF_FORM_FIELD: "not-the-cookie"})
    assert r.status_code == 400


def test_post_with_no_cookie_rejected():
    client = TestClient(_build_app())
    r = client.post("/submit", data={CSRF_FORM_FIELD: "anything"})
    assert r.status_code == 400


def test_get_form_twice_reuses_same_token():
    """Multi-tab simulation: a refresh of the form keeps the same CSRF token."""
    client = TestClient(_build_app())
    r1 = client.get("/form")
    r2 = client.get("/form")
    assert r1.cookies[CSRF_COOKIE_NAME] == r2.cookies[CSRF_COOKIE_NAME]


def test_post_with_empty_form_field_rejected():
    client = TestClient(_build_app())
    client.get("/form")
    r = client.post("/submit", data={CSRF_FORM_FIELD: ""})
    assert r.status_code == 400


def test_cookie_attributes_lax_and_one_hour():
    r = TestClient(_build_app()).get("/form")
    sc = r.headers["set-cookie"].lower()
    assert "samesite=lax" in sc
    assert "max-age=3600" in sc
    assert "httponly" not in sc
    assert "path=/" in sc


def test_secure_attribute_follows_settings():
    # Insecure deployment (HTTP local dev): no Secure flag on the cookie
    r_insecure = TestClient(_build_app(cookie_secure=False)).get("/form")
    assert "secure" not in r_insecure.headers["set-cookie"].lower()
    # Secure deployment (HTTPS): Secure flag present
    r_secure = TestClient(_build_app(cookie_secure=True)).get("/form")
    assert "secure" in r_secure.headers["set-cookie"].lower()


def _echo_app() -> FastAPI:
    app = FastAPI()

    @app.get("/echo")
    async def echo(request: Request) -> dict[str, str]:
        return {"token": mint_csrf_token(request)}

    return app


def test_mint_csrf_token_replaces_malformed_cookie():
    """An attacker-controlled or garbage cookie value must not be reused;
    mint_csrf_token replaces it with a fresh secrets.token_urlsafe(32).
    """
    client = TestClient(_echo_app())
    client.cookies.set(CSRF_COOKIE_NAME, "../../../etc/passwd")
    response = client.get("/echo")
    token = response.json()["token"]
    assert token != "../../../etc/passwd"
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token), token


def test_mint_csrf_token_reuses_well_formed_cookie():
    """A well-formed urlsafe-base64 token of acceptable length is reused."""
    fixed = "A" * 32  # 32 chars, urlsafe-base64 charset
    client = TestClient(_echo_app())
    client.cookies.set(CSRF_COOKIE_NAME, fixed)
    response = client.get("/echo")
    assert response.json()["token"] == fixed
