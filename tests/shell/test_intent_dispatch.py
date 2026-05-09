from __future__ import annotations

import pytest

from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
from iris.shell.intent_dispatch import (
    IntentDispatcher,
    IntentForbidden,
    IntentNotFound,
    IntentSpec,
)


def _admin_caps() -> Capabilities:
    return Capabilities(
        is_admin=True,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )


def test_register_and_resolve_returns_spec():
    d = IntentDispatcher()
    spec = IntentSpec(
        feature="auth", intent="my_access",
        title=lambda _params: "My access",
        required=lambda _c: True,
    )
    d.register(spec)
    assert d.resolve("auth", "my_access") is spec


def test_resolve_unknown_raises_intent_not_found():
    d = IntentDispatcher()
    with pytest.raises(IntentNotFound):
        d.resolve("auth", "ghost")


def test_check_capability_passes_when_predicate_true():
    d = IntentDispatcher()
    d.register(IntentSpec(
        feature="auth", intent="admin",
        title=lambda _p: "Admin",
        required=lambda c: c.is_admin,
    ))
    d.check("auth", "admin", _admin_caps())


def test_check_capability_raises_when_predicate_false():
    d = IntentDispatcher()
    d.register(IntentSpec(
        feature="auth", intent="admin",
        title=lambda _p: "Admin",
        required=lambda c: c.is_admin,
    ))
    with pytest.raises(IntentForbidden):
        d.check("auth", "admin", EMPTY_CAPABILITIES)


def test_title_called_with_params():
    d = IntentDispatcher()
    d.register(IntentSpec(
        feature="auth", intent="manage",
        title=lambda p: f"Manage {p['database']}",
        required=lambda _c: True,
    ))
    spec = d.resolve("auth", "manage")
    assert spec.title({"database": "marketing"}) == "Manage marketing"


def test_register_duplicate_raises_value_error():
    d = IntentDispatcher()
    spec = IntentSpec(
        feature="auth", intent="my_access",
        title=lambda _p: "x", required=lambda _c: True,
    )
    d.register(spec)
    with pytest.raises(ValueError):
        d.register(spec)
