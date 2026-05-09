"""DOM id helpers for tab-scoped fragments.

Every id inside a tab fragment is derived from the tab's id so multiple
tabs of the same feature don't collide. Server-side only — never compute
ids in the browser.
"""
from __future__ import annotations


def el(tab_id: str, *parts: str) -> str:
    """Compose a tab-scoped element id: ``el("AB12", "results")`` → ``"t-AB12-results"``."""
    return "t-" + tab_id + "-" + "-".join(parts)


def tab_button_id(tab_id: str) -> str:
    """Id of the tab-strip button for ``tab_id``."""
    return f"tab-button-{tab_id}"


def tab_panel_id(tab_id: str) -> str:
    """Id of the tab-content panel for ``tab_id``."""
    return f"tab-content-{tab_id}"
