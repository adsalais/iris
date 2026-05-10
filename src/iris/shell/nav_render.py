"""Render the left-panel navigation server-side from a Contributions
registry, filtered by the session's Capabilities.

HTML is built as plain strings (escape via html.escape) rather than a
Jinja template — easier to test, avoids loader gymnastics, and the
output is small enough that string concatenation is fine.
"""
from __future__ import annotations

import html
import json
from collections.abc import Sequence
from urllib.parse import quote

from iris.auth.rights import Capabilities
from iris.shell.contributions import (
    Contributions,
    NavEntry,
    NavGroup,
    TabIntent,
)

CHILDREN_POPOVER_THRESHOLD = 10


def render_nav(contribs: Contributions, caps: Capabilities) -> str:
    parts: list[str] = ['<nav class="iris-nav">']
    for group in contribs.nav.groups:
        if not group.visible(caps):
            continue
        parts.append(_render_group(group, caps))
    parts.append("</nav>")
    return "".join(parts)


def _render_group(group: NavGroup, caps: Capabilities) -> str:
    visible_entries = [e for e in group.entries if e.visible(caps)]
    if not visible_entries:
        return ""
    parts: list[str] = ['<div class="iris-nav-group">']
    parts.append(
        f'<h3 class="iris-nav-group-label">{html.escape(group.label)}</h3>'
    )
    parts.append("<ul>")
    for entry in visible_entries:
        parts.append(_render_entry(entry, caps))
    parts.append("</ul></div>")
    return "".join(parts)


def _render_entry(entry: NavEntry, caps: Capabilities) -> str:
    parts: list[str] = ['<li class="iris-nav-entry">']
    if entry.on_click is not None:
        action = _post_tab_action(entry.on_click)
        action_attr = html.escape(action, quote=True)
        label = html.escape(entry.label)
        parts.append(f'<button data-on:click="{action_attr}">{label}</button>')
    else:
        parts.append(
            f'<span class="iris-nav-entry-label">{html.escape(entry.label)}</span>'
        )
    if entry.badge is not None:
        b = entry.badge(caps)
        if b is not None:
            parts.append(f'<span class="iris-nav-badge">{html.escape(str(b))}</span>')
    if entry.children is not None:
        children = list(entry.children(caps))
        if children:
            parts.append(_render_children(children, caps))
    parts.append("</li>")
    return "".join(parts)


def _render_children(children: Sequence[NavEntry], caps: Capabilities) -> str:
    cls = (
        "iris-nav-popover"
        if len(children) > CHILDREN_POPOVER_THRESHOLD
        else "iris-nav-children"
    )
    parts = [f'<ul class="{cls}">']
    for child in children:
        if not child.visible(caps):
            continue
        parts.append(_render_entry(child, caps))
    parts.append("</ul>")
    return "".join(parts)


def _post_tab_action(intent: TabIntent) -> str:
    """Build the ``@post('/api/tabs?…')`` Datastar action expression.

    Nav clicks open *temporary* (preview) tabs — VS Code style. If the user
    already has a temp tab open, the open route replaces it. We deliberately
    do not pass `from_tab` here: nav lives outside any tab panel, so the
    click isn't "interaction with the current tab" and shouldn't promote it.
    """
    params_json = json.dumps(intent.params, sort_keys=True)
    return (
        f"@post('/api/tabs?feature={intent.feature}&intent={intent.intent}"
        f"&params={quote(params_json, safe='')}&temporary=true')"
    )
