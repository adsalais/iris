import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from iris.app import _clock_stream, app


@pytest.fixture
def client():
    return TestClient(app)


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Iris" in r.text
    assert "datastar.js" in r.text
    assert 'data-signals="{count: 0}"' in r.text
    assert 'data-on:click="@get(\'/api/greet\')"' in r.text


def test_greet_default_returns_sse(client):
    r = client.get("/api/greet")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "stranger" in r.text
    assert 'id="greeting"' in r.text


DATASTAR_HEADERS = {"Datastar-Request": "true"}


def test_greet_uses_name_signal(client):
    r = client.get(
        "/api/greet",
        params={"datastar": json.dumps({"name": "Ada"})},
        headers=DATASTAR_HEADERS,
    )
    assert r.status_code == 200
    assert "Ada" in r.text


def test_greet_escapes_html_in_name(client):
    r = client.get(
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
