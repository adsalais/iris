from __future__ import annotations

import re

import pytest

from iris.shell.tabs import (
    MAX_TABS_PER_SESSION,
    TabCapExceeded,
    TabRecord,
    append_tab,
    find_tab,
    list_tabs,
    new_tab_id,
    remove_tab,
    replace_tab,
)


def test_new_tab_id_is_path_safe_alphanumeric():
    """Tab ids must be safe to embed as JS identifiers and Datastar signal
    paths — alphanumeric only, with a leading letter."""
    tid = new_tab_id()
    assert re.fullmatch(r"t[a-f0-9]{8}", tid), tid


def test_new_tab_id_is_random():
    seen = {new_tab_id() for _ in range(100)}
    assert len(seen) == 100


def test_list_tabs_empty_when_data_missing():
    assert list_tabs({}) == []


def test_list_tabs_reads_from_session_data():
    data = {"tabs": [{"id": "X", "feature": "auth", "intent": "my_access",
                       "params": {}, "title": "T"}]}
    tabs = list_tabs(data)
    assert len(tabs) == 1
    assert tabs[0].id == "X"
    assert tabs[0].feature == "auth"
    assert tabs[0].title == "T"


def test_find_tab_returns_record_or_none():
    data = {"tabs": [{"id": "X", "feature": "auth", "intent": "my_access",
                       "params": {}, "title": "T"}]}
    assert find_tab(data, "X") is not None
    assert find_tab(data, "Y") is None


def test_append_tab_initializes_tabs_list():
    data: dict[str, object] = {}
    rec = TabRecord(id="X", feature="auth", intent="my_access",
                    params={}, title="T")
    append_tab(data, rec)
    assert data["tabs"] == [{"id": "X", "feature": "auth",
                              "intent": "my_access", "params": {}, "title": "T",
                              "temporary": False}]


def test_append_tab_enforces_cap():
    data: dict[str, list[dict[str, object]]] = {"tabs": []}
    for i in range(MAX_TABS_PER_SESSION):
        append_tab(data, TabRecord(
            id=f"id{i:02}", feature="f", intent="i", params={}, title="t"))
    with pytest.raises(TabCapExceeded):
        append_tab(data, TabRecord(
            id="overflow", feature="f", intent="i", params={}, title="t"))


def test_remove_tab_drops_only_the_matching_id():
    data = {"tabs": [
        {"id": "A", "feature": "f", "intent": "i", "params": {}, "title": "a"},
        {"id": "B", "feature": "f", "intent": "i", "params": {}, "title": "b"},
    ]}
    removed = remove_tab(data, "A")
    assert removed is True
    assert [t["id"] for t in data["tabs"]] == ["B"]


def test_remove_tab_returns_false_when_missing():
    data = {"tabs": []}
    assert remove_tab(data, "X") is False


def test_replace_tab_updates_in_place():
    data = {"tabs": [
        {"id": "A", "feature": "auth", "intent": "manage",
         "params": {"database": "old"}, "title": "Manage old"},
    ]}
    replace_tab(data, "A", TabRecord(
        id="A", feature="auth", intent="manage",
        params={"database": "new"}, title="Manage new"))
    assert data["tabs"][0]["params"] == {"database": "new"}
    assert data["tabs"][0]["title"] == "Manage new"


def test_replace_tab_raises_when_missing():
    data = {"tabs": []}
    with pytest.raises(KeyError):
        replace_tab(data, "X", TabRecord(
            id="X", feature="f", intent="i", params={}, title="t"))
