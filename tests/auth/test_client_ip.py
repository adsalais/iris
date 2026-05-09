"""Unit tests for iris.auth.client_ip.client_ip.

We construct fake Starlette Request objects directly via the ASGI scope dict
to avoid spinning up TestClient — these tests are about the helper's input
parsing, not HTTP round-trips.
"""
from __future__ import annotations

from starlette.requests import Request

from iris.auth.client_ip import client_ip


def _make_request(*, headers: dict[str, str] | None = None,
                  client: tuple[str, int] | None = ("10.0.0.1", 12345)) -> Request:
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": raw_headers,
        "client": client,
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_no_trust_no_header_returns_request_client():
    r = _make_request(client=("10.0.0.1", 12345))
    assert client_ip(r, trust_forwarded=False) == "10.0.0.1"


def test_no_trust_header_present_ignores_header():
    r = _make_request(headers={"x-forwarded-for": "1.2.3.4"}, client=("10.0.0.1", 12345))
    assert client_ip(r, trust_forwarded=False) == "10.0.0.1"


def test_trust_no_header_falls_back_to_request_client():
    r = _make_request(client=("10.0.0.1", 12345))
    assert client_ip(r, trust_forwarded=True) == "10.0.0.1"


def test_trust_single_xff_returns_that_ip():
    r = _make_request(headers={"x-forwarded-for": "1.2.3.4"})
    assert client_ip(r, trust_forwarded=True) == "1.2.3.4"


def test_trust_xff_list_returns_leftmost_ip():
    r = _make_request(headers={"x-forwarded-for": "1.2.3.4, 5.6.7.8, 9.10.11.12"})
    assert client_ip(r, trust_forwarded=True) == "1.2.3.4"


def test_trust_xff_leading_whitespace_is_stripped():
    r = _make_request(headers={"x-forwarded-for": "   1.2.3.4   , 5.6.7.8"})
    assert client_ip(r, trust_forwarded=True) == "1.2.3.4"


def test_trust_empty_xff_falls_back_to_request_client():
    r = _make_request(headers={"x-forwarded-for": ""}, client=("10.0.0.1", 12345))
    assert client_ip(r, trust_forwarded=True) == "10.0.0.1"


def test_trust_xff_with_only_whitespace_falls_back():
    r = _make_request(headers={"x-forwarded-for": "   "}, client=("10.0.0.1", 12345))
    assert client_ip(r, trust_forwarded=True) == "10.0.0.1"


def test_no_client_and_no_xff_returns_unknown():
    r = _make_request(client=None)
    assert client_ip(r, trust_forwarded=False) == "unknown"
    assert client_ip(r, trust_forwarded=True) == "unknown"


def test_trust_xff_list_with_empty_first_falls_back():
    """Defensive: '   , 5.6.7.8' — first slot is empty after strip."""
    r = _make_request(headers={"x-forwarded-for": "   , 5.6.7.8"}, client=("10.0.0.1", 12345))
    assert client_ip(r, trust_forwarded=True) == "10.0.0.1"
