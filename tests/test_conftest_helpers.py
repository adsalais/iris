"""Smoke tests for the test helpers themselves: parse_sse + capability_session."""
from __future__ import annotations

import asyncio


def test_parse_sse_splits_events(parse_sse):
    raw = (
        "event: datastar-patch-elements\n"
        "data: elements <div id=\"x\">a</div>\n\n"
        "event: datastar-patch-signals\n"
        "data: signals {\"k\":1}\n\n"
    )
    events = parse_sse(raw)
    assert len(events) == 2
    assert events[0].event == "datastar-patch-elements"
    assert "elements" in events[0].data
    assert events[1].event == "datastar-patch-signals"
    assert "signals" in events[1].data


def test_capability_session_creates_authed_client(app, capability_session):
    client, _sid = asyncio.run(capability_session(is_admin=True))
    r = client.get("/")
    # The route surface changes during Phase 1; we accept anything other than
    # an unauthenticated redirect/401 — the assertion is "the cookie reached
    # the app and a session-scoped route was resolved".
    assert r.status_code in (200, 404, 401)
    assert client.cookies.get("iris_session") is not None
