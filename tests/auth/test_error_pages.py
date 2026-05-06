from fastapi import Depends
from fastapi.testclient import TestClient

from iris.auth.authz.deps import require_role
from iris.auth.session import Session


def test_forbidden_html_renders_template(monkeypatch):
    # Use a username that's NOT the bootstrap admin user (conftest sets
    # AUTHZ_BOOTSTRAP_USER=alice). Bob has no role assignments, so the
    # admin-gated route should 403.
    monkeypatch.setenv("MOCK_USERNAME", "bob")
    monkeypatch.setenv("MOCK_GROUPS", "users")  # NOT admins
    from iris.app import build_app

    app = build_app()

    @app.get("/admin-only")
    async def admin_only(_: Session = Depends(require_role("admin"))):
        return {"ok": True}

    client = TestClient(app)
    from iris.auth.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD

    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "bob",
            "password": "secret",
            "next": "/",
        },
    )
    r = client.get("/admin-only", headers={"accept": "text/html"})
    assert r.status_code == 403
    assert "Forbidden" in r.text
    assert "admin" in r.text
