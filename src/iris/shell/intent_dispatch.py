"""Intent registration and dispatch.

A feature registers one IntentSpec per intent it exposes. The dispatcher
maps ``(feature, intent)`` to its spec, providing the ``required``
predicate (intent gate, layer 2 of defense in depth) and the title
function (how to format the tab title from its params).

Note that IntentSpec doesn't include the *render* function. Rendering
is reached by HTTP routes mounted under the feature's APIRouter
(``/feature/<feature>/{tab_id}/render``); the route picks the right
render function from the feature's intents module by intent name.
The dispatcher's job is title + capability gate.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from iris.auth.rights import Capabilities


class IntentNotFound(Exception):
    """Raised when ``(feature, intent)`` is not registered."""


class IntentForbidden(Exception):
    """Raised when ``IntentSpec.required`` returns False for the session's caps."""


@dataclass(frozen=True, slots=True)
class IntentSpec:
    feature: str
    intent: str
    title: Callable[[dict[str, Any]], str]
    required: Callable[[Capabilities], bool]


class IntentDispatcher:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], IntentSpec] = {}

    def register(self, spec: IntentSpec) -> None:
        key = (spec.feature, spec.intent)
        if key in self._by_key:
            msg = f"intent already registered: {key}"
            raise ValueError(msg)
        self._by_key[key] = spec

    def resolve(self, feature: str, intent: str) -> IntentSpec:
        try:
            return self._by_key[(feature, intent)]
        except KeyError as e:
            msg = f"unknown intent: {(feature, intent)}"
            raise IntentNotFound(msg) from e

    def check(self, feature: str, intent: str, caps: Capabilities) -> IntentSpec:
        """Resolve + capability check. Returns the spec on success."""
        spec = self.resolve(feature, intent)
        if not spec.required(caps):
            msg = f"capability gate failed for {(feature, intent)}"
            raise IntentForbidden(msg)
        return spec
