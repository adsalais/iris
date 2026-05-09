"""Smoke tests for build_app and the shell wiring.

Detailed shell-route tests live in tests/shell/. This file just
verifies build_app composes correctly and shutdown hooks fire.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_build_app_initializes_shutdown_hooks_list():
    from iris.app import build_app

    app = build_app(install_clickhouse=False)
    assert isinstance(app.state.shutdown_hooks, list)
    # auth.install registers at least the session-store closer
    assert len(app.state.shutdown_hooks) >= 1


def test_shutdown_hooks_run_in_lifo_order():
    from iris.app import build_app

    app = build_app(install_clickhouse=False)
    order: list[str] = []

    async def first():
        order.append("first")

    async def second():
        order.append("second")

    app.state.shutdown_hooks.append(first)
    app.state.shutdown_hooks.append(second)

    with TestClient(app):
        pass

    appended = [n for n in order if n in ("first", "second")]
    assert appended == ["second", "first"]


def test_app_state_has_contributions_and_dispatcher():
    from iris.app import build_app
    from iris.shell.contributions import Contributions
    from iris.shell.intent_dispatch import IntentDispatcher

    app = build_app(install_clickhouse=False)
    assert isinstance(app.state.contributions, Contributions)
    assert isinstance(app.state.intent_dispatcher, IntentDispatcher)


def test_app_state_has_templates():
    from iris.app import build_app

    app = build_app(install_clickhouse=False)
    assert hasattr(app.state, "templates")
