"""Typed contribution registry exposed to feature modules.

Features call ``app.state.contributions.nav.add(NavGroup(...))`` from their
``install(app)`` to extend the shell's left-panel navigation. Per the
discipline rule in the design spec (§4.2), only the ``nav`` extension point
is shipped at MVP; new registries are added one at a time when (and only
when) a real cross-feature integration motivates one.

Visibility predicates and dynamic-list derivers receive the session's
``Capabilities`` so the shell renders the same registry differently per
user. The shell evaluates these per-render server-side; nothing about
nav rendering happens in the browser.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from iris.auth.rights import Capabilities

CapPredicate = Callable[[Capabilities], bool]
CapDerived = Callable[[Capabilities], Any]


def _always_visible(_c: Capabilities) -> bool:
    return True


@dataclass(frozen=True, slots=True)
class TabIntent:
    """Open-a-tab descriptor: which feature, which intent, with what params."""
    feature: str
    intent: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NavEntry:
    label: str
    on_click: TabIntent | None = None
    icon: str | None = None
    visible: CapPredicate = _always_visible
    badge: CapDerived | None = None
    children: CapDerived | None = None


@dataclass(frozen=True, slots=True)
class NavGroup:
    label: str
    icon: str | None = None
    visible: CapPredicate = _always_visible
    entries: Sequence[NavEntry] = ()


@dataclass(slots=True)
class NavRegistry:
    groups: list[NavGroup] = field(default_factory=list)

    def add(self, group: NavGroup) -> None:
        self.groups.append(group)


@dataclass(slots=True)
class Contributions:
    nav: NavRegistry = field(default_factory=NavRegistry)
