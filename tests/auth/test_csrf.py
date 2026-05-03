from fastapi import Depends, FastAPI, Form
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from iris.auth.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_FORM_FIELD,
    issue_csrf_token,
    verify_csrf_form,
)


def _build_app() -> FastAPI:
    app = FastAPI()

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
