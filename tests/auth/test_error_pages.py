from fastapi.testclient import TestClient

from iris.auth import SessionAdmin


def test_forbidden_html_renders_template(monkeypatch):
    # Bob is not bootstrapped as admin (only alice is in the conftest), and
    # tests run with install_clickhouse=False so derive_rights never runs —
    # bob's session has empty Rights. The SessionAdmin-gated route 403s.
    monkeypatch.setenv("MOCK_USERNAME", "bob")
    monkeypatch.setenv("MOCK_GROUPS", "users")
    from iris.app import build_app

    app = build_app(install_clickhouse=False)

    @app.get("/admin-only")
    async def admin_only(_: SessionAdmin):
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
