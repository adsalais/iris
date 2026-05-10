"""Server-side tab state lives in ``session.data['tabs']`` and is mutated
through these pure helpers. Routes call them and then call
``await session.persist_data()`` to flush.

A tab is one instance of a feature page. Multiple tabs can hold the same
feature with different params; ids make them unique. The cap bounds
session row size and protects against runaway tab spam from a buggy
client.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

MAX_TABS_PER_SESSION = 32


class TabCapExceeded(Exception):
    """Raised when ``append_tab`` would exceed ``MAX_TABS_PER_SESSION``."""


@dataclass(frozen=True, slots=True)
class TabRecord:
    """Wire-shape for one tab. Mirrors the dict stored in ``session.data['tabs']``.

    `temporary` is a preview-tab flag (italic title in the strip): only one
    temporary tab can exist at a time, and opening another temp tab replaces
    it. Promoted to permanent (False) on user interaction — see the open
    and promote routes in `shell/routes.py`.
    """
    id: str
    feature: str
    intent: str
    params: dict[str, Any]
    title: str
    temporary: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "feature": self.feature,
            "intent": self.intent,
            "params": self.params,
            "title": self.title,
            "temporary": self.temporary,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "TabRecord":
        return cls(
            id=d["id"],
            feature=d["feature"],
            intent=d["intent"],
            params=d.get("params", {}),
            title=d.get("title", ""),
            temporary=bool(d.get("temporary", False)),
        )


def new_tab_id() -> str:
    """8-char URL-safe random tab id."""
    return secrets.token_urlsafe(6)


def list_tabs(data: dict[str, Any]) -> list[TabRecord]:
    """Return all tabs from a session.data dict (empty if key missing)."""
    raw = data.get("tabs", [])
    return [TabRecord.from_json(d) for d in raw]


def find_tab(data: dict[str, Any], tab_id: str) -> TabRecord | None:
    for t in list_tabs(data):
        if t.id == tab_id:
            return t
    return None


def append_tab(data: dict[str, Any], rec: TabRecord) -> None:
    """Append a tab record. Raises TabCapExceeded if at the per-session cap."""
    tabs = data.setdefault("tabs", [])
    if len(tabs) >= MAX_TABS_PER_SESSION:
        msg = f"session has {len(tabs)} tabs; cap is {MAX_TABS_PER_SESSION}"
        raise TabCapExceeded(msg)
    tabs.append(rec.to_json())


def remove_tab(data: dict[str, Any], tab_id: str) -> bool:
    """Remove the tab with this id. Return True if removed, False if absent."""
    tabs = data.get("tabs", [])
    for i, t in enumerate(tabs):
        if t.get("id") == tab_id:
            del tabs[i]
            return True
    return False


def replace_tab(data: dict[str, Any], tab_id: str, rec: TabRecord) -> None:
    """Replace the tab with this id. Raises KeyError if absent."""
    tabs = data.get("tabs", [])
    for i, t in enumerate(tabs):
        if t.get("id") == tab_id:
            tabs[i] = rec.to_json()
            return
    raise KeyError(tab_id)


def find_temporary_tab(data: dict[str, Any]) -> TabRecord | None:
    """Return the (single) temporary tab in this session, or None."""
    for t in list_tabs(data):
        if t.temporary:
            return t
    return None
