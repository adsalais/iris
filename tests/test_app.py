import asyncio
import json

from iris.app import _clock_stream


def test_index_renders(authed_client):
    r = authed_client.get("/")
    assert r.status_code == 200
    assert "Iris" in r.text
    assert "datastar.js" in r.text
    assert "Alice" in r.text
    assert 'data-signals="{count: 0}"' in r.text
    assert 'data-on:click="@get(\'/api/greet\')"' in r.text
    assert "iris_csrf" in r.cookies  # guards the explicit attach_csrf_cookie call in app.py


def test_greet_default_returns_sse(authed_client):
    r = authed_client.get("/api/greet")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "Alice" in r.text
    assert 'id="greeting"' in r.text


DATASTAR_HEADERS = {"Datastar-Request": "true"}


def test_greet_uses_name_signal(authed_client):
    r = authed_client.get(
        "/api/greet",
        params={"datastar": json.dumps({"name": "Ada"})},
        headers=DATASTAR_HEADERS,
    )
    assert r.status_code == 200
    assert "Ada" in r.text


def test_greet_escapes_html_in_name(authed_client):
    r = authed_client.get(
        "/api/greet",
        params={"datastar": json.dumps({"name": "<script>alert(1)</script>"})},
        headers=DATASTAR_HEADERS,
    )
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_clock_stream_yields_signal_patch():
    async def first_tick():
        agen = _clock_stream()
        try:
            return await agen.__anext__()
        finally:
            await agen.aclose()

    event = asyncio.run(first_tick())
    assert event.startswith("event: datastar-patch-signals")
    assert '"now":' in event


def test_shutdown_hooks_run_in_lifo_order():
    """Hooks registered into app.state.shutdown_hooks fire in reverse-of-registration order."""
    from iris.app import build_app
    from fastapi.testclient import TestClient

    app = build_app(install_clickhouse=False)
    order: list[str] = []

    async def first():
        order.append("first")

    async def second():
        order.append("second")

    app.state.shutdown_hooks.append(first)
    app.state.shutdown_hooks.append(second)

    with TestClient(app):
        pass  # startup runs; exit triggers shutdown

    # Of the hooks we appended, second fires before first (LIFO).
    appended_order = [name for name in order if name in ("first", "second")]
    assert appended_order == ["second", "first"]


def test_build_app_initializes_shutdown_hooks_list():
    """build_app() exposes app.state.shutdown_hooks as a populated list."""
    from iris.app import build_app

    app = build_app(install_clickhouse=False)
    assert isinstance(app.state.shutdown_hooks, list)
    # auth.install registers at least the session-store closer.
    assert len(app.state.shutdown_hooks) >= 1
