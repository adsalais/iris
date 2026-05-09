"""Static-files mount serves the vendored Datastar bundle (now under shell)."""
from fastapi.testclient import TestClient

from iris.app import build_app


def test_static_datastar_js_is_served():
    app = build_app(install_clickhouse=False)
    c = TestClient(app)
    r = c.get("/static/shell/datastar.js")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert ct.startswith(("application/javascript", "text/javascript")), (
        f"unexpected content-type: {ct!r}"
    )
    # Sanity-check the body: real bundle, not a stub or HTML 404 page.
    assert len(r.content) > 10_000, f"datastar.js body too small ({len(r.content)} bytes)"
    # The bundle is plain JS source, must decode as UTF-8 cleanly.
    r.content.decode("utf-8")  # raises UnicodeDecodeError on failure


def test_shell_static_mount_404s_for_missing_file():
    app = build_app(install_clickhouse=False)
    c = TestClient(app)
    r = c.get("/static/shell/does-not-exist.js")
    assert r.status_code == 404
