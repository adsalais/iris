"""Unit tests for tab_render_url — pure URL builder, no FastAPI involvement."""
from __future__ import annotations

from iris.shell.url_builders import tab_render_url


def test_no_params():
    tab = {"feature": "auth", "id": "ABCD1234", "intent": "my_access", "params": {}}
    assert tab_render_url(tab) == "/feature/auth/ABCD1234/my_access"


def test_single_param():
    tab = {"feature": "auth", "id": "ABCD1234", "intent": "manage",
           "params": {"database": "marketing"}}
    assert tab_render_url(tab) == "/feature/auth/ABCD1234/manage?database=marketing"


def test_multiple_params():
    tab = {"feature": "auth", "id": "ABCD1234", "intent": "admin_console",
           "params": {"subtab": "users", "extra": "x"}}
    url = tab_render_url(tab)
    assert url.startswith("/feature/auth/ABCD1234/admin_console?")
    assert "subtab=users" in url
    assert "extra=x" in url


def test_special_chars_in_value():
    tab = {"feature": "auth", "id": "ABCD1234", "intent": "manage",
           "params": {"database": "needs encoding"}}
    assert tab_render_url(tab) == "/feature/auth/ABCD1234/manage?database=needs%20encoding"


def test_missing_params_key():
    tab = {"feature": "auth", "id": "ABCD1234", "intent": "my_access"}
    assert tab_render_url(tab) == "/feature/auth/ABCD1234/my_access"
