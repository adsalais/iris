from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from iris.auth.exceptions import (
    AuthForbidden,
    AuthRequired,
    install_exception_handlers,
)


def _build_app() -> FastAPI:
    app = FastAPI()

    async def needs_auth():
        raise AuthRequired()

    async def needs_group():
        raise AuthForbidden(needed=("admins",), have=("users",))

    @app.get("/private")
    async def private(_: None = Depends(needs_auth)):  # noqa: B008
        return {"ok": True}

    @app.get("/admin")
    async def admin(_: None = Depends(needs_group)):  # noqa: B008
        return {"ok": True}

    install_exception_handlers(app, cookie_name="iris_session")
    return app


def test_html_request_redirects_to_login():
    client = TestClient(_build_app())
    r = client.get("/private", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login?next=/private")


def test_html_request_clears_cookie():
    client = TestClient(_build_app())
    client.cookies.set("iris_session", "stale")
    r = client.get("/private", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 302
    assert "iris_session=" in r.headers.get("set-cookie", "").lower()
    assert 'max-age=0' in r.headers["set-cookie"].lower()


def test_api_request_returns_401():
    client = TestClient(_build_app())
    r = client.get("/private", headers={"accept": "application/json"})
    assert r.status_code == 401
    assert r.text == ""


def test_html_forbidden_returns_403_html():
    client = TestClient(_build_app())
    r = client.get("/admin", headers={"accept": "text/html"})
    assert r.status_code == 403
    assert "admins" in r.text.lower()
    assert "users" in r.text.lower()


def test_api_forbidden_returns_403_no_body():
    client = TestClient(_build_app())
    r = client.get("/admin", headers={"accept": "application/json"})
    assert r.status_code == 403
    assert r.text == ""
