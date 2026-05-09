from __future__ import annotations

import dataclasses

import pytest

from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
from iris.shell.contributions import (
    Contributions,
    NavEntry,
    NavGroup,
    NavRegistry,
    TabIntent,
)


def _caps(is_admin: bool = False, db_admin: frozenset[str] = frozenset()) -> Capabilities:
    return Capabilities(
        is_admin=is_admin,
        can_create_database=False,
        db_admin=db_admin,
        db_writer=frozenset(),
        db_reader=frozenset(),
    )


def test_default_contributions_has_empty_nav():
    c = Contributions()
    assert c.nav.groups == []


def test_nav_registry_add_appends():
    reg = NavRegistry()
    g1 = NavGroup(label="A", entries=(NavEntry("e1"),))
    g2 = NavGroup(label="B", entries=())
    reg.add(g1)
    reg.add(g2)
    assert reg.groups == [g1, g2]


def test_nav_entry_defaults_visible_true():
    e = NavEntry("Always visible")
    assert e.visible(EMPTY_CAPABILITIES) is True


def test_nav_entry_visible_predicate():
    e = NavEntry("Admin only", visible=lambda c: c.is_admin)
    assert e.visible(_caps(is_admin=False)) is False
    assert e.visible(_caps(is_admin=True)) is True


def test_nav_entry_badge_called_with_capabilities():
    e = NavEntry(
        "DBs",
        badge=lambda c: str(len(c.db_admin)) if c.db_admin else None,
    )
    assert e.badge is not None
    assert e.badge(_caps(db_admin=frozenset())) is None
    assert e.badge(_caps(db_admin=frozenset({"a", "b"}))) == "2"


def test_nav_entry_children_returns_dynamic_list():
    e = NavEntry(
        "DBs",
        children=lambda c: [NavEntry(d) for d in sorted(c.db_admin)],
    )
    assert e.children is not None
    children = list(e.children(_caps(db_admin=frozenset({"z", "a"}))))
    assert [c.label for c in children] == ["a", "z"]


def test_tab_intent_is_frozen():
    ti = TabIntent(feature="auth", intent="my_access")
    assert dataclasses.is_dataclass(ti)
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        ti.feature = "other"  # pyright: ignore[reportAttributeAccessIssue]


def test_tab_intent_params_default_empty_dict():
    ti = TabIntent(feature="auth", intent="my_access")
    assert ti.params == {}


def test_nav_group_visible_predicate():
    g = NavGroup(label="Admin", visible=lambda c: c.is_admin, entries=())
    assert g.visible(_caps(is_admin=False)) is False
    assert g.visible(_caps(is_admin=True)) is True
