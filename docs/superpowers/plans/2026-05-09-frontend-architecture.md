# Frontend Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the iris frontend shell (two-panel collapsible nav + tabs) and the first feature module (Authorization, capability-adaptive) per `docs/superpowers/specs/2026-05-09-frontend-architecture-design.md`.

**Architecture:** Integrated app with feature modules under `src/iris/features/<name>/`, typed `Contributions` registry on `app.state` (only `nav` at MVP), server-persisted tabs in `session.data['tabs']` with per-tab signal namespacing. Defense in depth at three layers: nav filter, intent gate, per-route `Session*` guard. Datastar hypermedia-first: signals only for ephemeral UI state, all structural changes via SSE patches.

**Tech Stack:** Python 3.13, FastAPI, Datastar (datastar-py SDK), Jinja2, ClickHouse (existing), SQLite (sessions), pytest, basedpyright, ruff.

---

## Spec corrections discovered while planning

The spec's §2.3 "Open" flow uses `data-on:load` to lazy-fetch panel content. `load` is a real DOM event but only fires on `<body>`, `<img>`, `<iframe>`, `<script>`, `<link>` — not on `<div>`. Datastar's correct primitive for "fire once when element enters the DOM" is **`data-init="@get(...)"`**. The plan uses `data-init` throughout. (Behavioral intent unchanged.)

Two `datastar_py` SDK details verified at planning time:
- `SSE.patch_elements(elements, selector=None, mode=None, ...)` — modes are: `outer` (default, replace target by id), `inner`, `remove`, `replace`, `prepend`, `append`, `before`, `after`. With `mode="append"` you must supply `selector="#parent_id"`.
- `DatastarResponse(content)` accepts a single `DatastarEvent`, a list/iterable of events, or an async generator. The plan returns lists for tab-open (multiple events at once) and a single event everywhere else.

---

## File structure

### New files (shell)

| Path | Responsibility |
|---|---|
| `src/iris/shell/__init__.py` | Re-export `install` |
| `src/iris/shell/contributions.py` | Types: `Contributions`, `NavRegistry`, `NavGroup`, `NavEntry`, `TabIntent`, `CapPredicate`, `CapDerived` |
| `src/iris/shell/element_id.py` | `el(tab_id, *parts)` and `tab_panel_id(tab_id)`, `tab_button_id(tab_id)` |
| `src/iris/shell/tabs.py` | `new_tab_id()`; pure session.data helpers `list_tabs/find_tab/append_tab/remove_tab/replace_tab` |
| `src/iris/shell/intent_dispatch.py` | `IntentHandler` Protocol, per-feature `IntentRegistry`, top-level `IntentDispatcher` keyed by `(feature, intent)` |
| `src/iris/shell/nav_render.py` | `render_nav(contribs, capabilities) -> str` — pure HTML rendering |
| `src/iris/shell/install.py` | `install(app)` — wires `Contributions`, mounts shell static and routes, registers shell template dir |
| `src/iris/shell/routes.py` | `GET /`, `POST /api/tabs`, `DELETE /api/tabs/{tab_id}`, `PATCH /api/tabs/{tab_id}`, `GET /feature/{feature}/{tab_id}/render` |
| `src/iris/shell/templates/shell.html` | Base layout (replaces `templates/base.html`) |
| `src/iris/shell/templates/_nav.html` | Nav macro consumed by `nav_render.py` |
| `src/iris/shell/templates/_tab_strip.html` | Tab strip macro |
| `src/iris/shell/templates/_tab_panel.html` | Empty tab panel placeholder (carries `data-init`) |
| `src/iris/shell/templates/_account_popover.html` | Sign-out form + identity readout |
| `src/iris/shell/templates/_top_buttons.html` | Three top-left buttons |
| `src/iris/shell/static/shell.css` | Two-panel grid, nav collapse, popover, tab strip |

### New files (Authorization feature)

| Path | Responsibility |
|---|---|
| `src/iris/features/__init__.py` | Empty (namespace) |
| `src/iris/features/authorization/__init__.py` | Re-export `install` |
| `src/iris/features/authorization/install.py` | Nav contributions, intent registration, router mount, template dir registration |
| `src/iris/features/authorization/routes.py` | `APIRouter(prefix="/feature/auth")`; per-tab routes |
| `src/iris/features/authorization/intents.py` | One `render_*` function per intent |
| `src/iris/features/authorization/service.py` | Read-side helpers (capability listings, members listing, etc.) |
| `src/iris/features/authorization/templates/my_access.html` | Capability-adaptive home |
| `src/iris/features/authorization/templates/manage.html` | Per-database manage page |
| `src/iris/features/authorization/templates/_members_section.html` | Members section partial |
| `src/iris/features/authorization/templates/_row_policies.html` | Row policies partial |
| `src/iris/features/authorization/templates/_audit.html` | Audit partial |
| `src/iris/features/authorization/templates/_danger.html` | Delete-database partial |
| `src/iris/features/authorization/templates/create_database.html` | Create-database form |
| `src/iris/features/authorization/templates/admin_console.html` | Sub-tab framework |
| `src/iris/features/authorization/templates/_admin_users.html` | Users sub-tab |
| `src/iris/features/authorization/templates/_admin_databases.html` | Databases sub-tab |
| `src/iris/features/authorization/templates/_admin_policies.html` | Row policies sub-tab |
| `src/iris/features/authorization/templates/_admin_audit.html` | Audit sub-tab |

### Modified files

| Path | Why |
|---|---|
| `src/iris/templates.py` | Convert to registry pattern (`register_template_dir`, `init_templates`) |
| `src/iris/app.py` | Replace demo routes with shell wiring; install order auth → ch → shell → features |
| `src/iris/auth/routes.py` (`install`) | Call `register_template_dir(Path(__file__).parent / "templates")` |
| `src/iris/clickhouse/install.py` | (No change needed — does not own templates) |
| `tests/conftest.py` | Add `capability_session` fixture, `parse_sse` helper, `datastar_get` / `datastar_post` helpers |
| `tests/test_app.py` | Replace demo-route assertions with shell assertions |
| `CLAUDE.md` | Add Frontend section + conventions |

### Deleted files

| Path | Why |
|---|---|
| `src/iris/templates/index.html` | Demo |
| `src/iris/templates/base.html` | Replaced by `shell/templates/shell.html` |

### Moved files

| From | To |
|---|---|
| `src/iris/templates/auth/forbidden.html` | `src/iris/auth/templates/auth/forbidden.html` |
| `src/iris/templates/auth/ldap_form.html` | `src/iris/auth/templates/auth/ldap_form.html` |

### New files (docs)

| Path | Responsibility |
|---|---|
| `docs/frontend.md` | Shell module surface, tab system, Datastar conventions; peer to `docs/auth.md` |

### New test files

All under `tests/` (no `__init__.py` per CLAUDE.md). Subdirectories don't need `__init__.py`. Basenames must be unique across the whole `tests/` tree.

| Path | Covers |
|---|---|
| `tests/shell/test_contributions.py` | Registry types, predicates, derivers |
| `tests/shell/test_element_id.py` | `el()` helper |
| `tests/shell/test_tabs_helpers.py` | `new_tab_id()`, list/find/append/remove/replace |
| `tests/shell/test_intent_dispatch.py` | Register & dispatch by `(feature, intent)` |
| `tests/shell/test_nav_render.py` | Capability-filtered HTML output |
| `tests/shell/test_shell_routes.py` | `POST/DELETE/PATCH /api/tabs`, `GET /feature/.../render` |
| `tests/shell/test_shell_home.py` | `GET /` rendering nav + persisted tabs |
| `tests/shell/test_templates_loader.py` | `register_template_dir` + `init_templates` |
| `tests/features/test_authorization_install.py` | Nav contributions registered correctly |
| `tests/features/test_authorization_my_access.py` | Capability-adaptive my_access render |
| `tests/features/test_authorization_manage.py` | Members/policies/audit/danger; three authz layers |
| `tests/features/test_authorization_create_database.py` | Form, validation, success path |
| `tests/features/test_authorization_admin_console.py` | Sub-tabs; admin-only |

---

## Phase 1 — Shell scaffold (spec §7 step 1)

Builds the empty-but-functional shell and removes demo content. End state: `GET /` renders the two-panel layout with the nav (initially empty) and an empty tab strip; `POST /api/tabs` returns 400 because no intents are registered yet (intent gate); `data-init` lazy-load of any tab panel calls `GET /feature/.../render` which returns 404. Shell tests cover all of this.

### Task 1.1: Add `parse_sse` helper and `capability_session` fixture to `tests/conftest.py`

Both are needed by every subsequent test. Build them first.

**Files:**
- Modify: `tests/conftest.py`
- Test: `tests/test_conftest_helpers.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_conftest_helpers.py`:

```python
"""Smoke tests for the test helpers themselves: parse_sse + capability_session."""
from __future__ import annotations

import asyncio


def test_parse_sse_splits_events(parse_sse):
    raw = (
        "event: datastar-patch-elements\n"
        "data: elements <div id=\"x\">a</div>\n\n"
        "event: datastar-patch-signals\n"
        "data: signals {\"k\":1}\n\n"
    )
    events = parse_sse(raw)
    assert len(events) == 2
    assert events[0].event == "datastar-patch-elements"
    assert "elements" in events[0].data
    assert events[1].event == "datastar-patch-signals"
    assert "signals" in events[1].data


def test_capability_session_creates_authed_client(app, capability_session):
    client, _sid = asyncio.run(capability_session(is_admin=True))
    r = client.get("/")
    # Shell route doesn't exist yet at this Phase 1 point, so we accept any
    # response — the assertion is "session cookie is set, request reaches the app".
    assert r.status_code in (200, 404, 401)
    cookies = client.cookies.get("iris_session")
    assert cookies is not None
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_conftest_helpers.py -v
```
Expected: FAIL with `fixture 'parse_sse' not found` and `fixture 'capability_session' not found`.

- [ ] **Step 3: Implement helpers in `tests/conftest.py`**

Append to `tests/conftest.py` (after the existing `authed_client` fixture):

```python
# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SSEEvent:
    event: str
    data: str  # joined data lines, leading "data: " stripped


def _parse_sse_text(raw: str) -> list[SSEEvent]:
    """Split a text/event-stream body into SSEEvents. Tolerates trailing \\n."""
    events: list[SSEEvent] = []
    cur_event = ""
    cur_data: list[str] = []
    for line in raw.split("\n"):
        if line == "":
            if cur_event or cur_data:
                events.append(SSEEvent(event=cur_event, data="\n".join(cur_data)))
            cur_event = ""
            cur_data = []
            continue
        if line.startswith("event:"):
            cur_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            cur_data.append(line[len("data:"):].lstrip())
    if cur_event or cur_data:
        events.append(SSEEvent(event=cur_event, data="\n".join(cur_data)))
    return events


@pytest.fixture
def parse_sse():
    """Return a function that parses an SSE response body into [SSEEvent]."""
    return _parse_sse_text


# ---------------------------------------------------------------------------
# Capability-controlled session minting
# ---------------------------------------------------------------------------
from collections.abc import Iterable

from iris.auth.identity import User
from iris.auth.rights import Capabilities


@pytest.fixture
def capability_session(app):
    """Return an async function: build a session with given Capabilities,
    return (TestClient with cookie set, session_id)."""
    async def _make(
        *,
        is_admin: bool = False,
        can_create_database: bool = False,
        db_admin: Iterable[str] = (),
        db_writer: Iterable[str] = (),
        db_reader: Iterable[str] = (),
        username: str = "alice",
        display_name: str = "Alice",
        groups: tuple[str, ...] = ("users",),
        subject: str | None = None,
    ) -> tuple[TestClient, str]:
        store = app.state.auth_session_store
        user = User(
            subject=subject or f"mock:{username}",
            username=username,
            display_name=display_name,
            groups=groups,
        )
        session = await store.create(user)
        caps = Capabilities(
            is_admin=is_admin,
            can_create_database=can_create_database,
            db_admin=frozenset(db_admin),
            db_writer=frozenset(db_writer),
            db_reader=frozenset(db_reader),
        )
        await store.set_capabilities(session.id, caps)
        client = TestClient(app)
        client.cookies.set("iris_session", session.id)
        return client, session.id
    return _make
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/test_conftest_helpers.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/test_conftest_helpers.py
git commit -m "$(cat <<'EOF'
test: add parse_sse and capability_session helpers in conftest

parse_sse splits a text/event-stream body into SSEEvent dataclasses for
readable assertions on Datastar SSE responses. capability_session mints a
session with a given Capabilities and returns a cookie-bound TestClient,
so tests can exercise capability-adaptive UI without going through login.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.2: `shell/contributions.py` — typed registry

**Files:**
- Create: `src/iris/shell/__init__.py`
- Create: `src/iris/shell/contributions.py`
- Test: `tests/shell/test_contributions.py`

- [ ] **Step 1: Write the failing test**

Create `tests/shell/test_contributions.py`:

```python
from __future__ import annotations

from iris.auth.rights import Capabilities, EMPTY_CAPABILITIES
from iris.shell.contributions import (
    Contributions, NavEntry, NavGroup, NavRegistry, TabIntent,
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
    g1 = NavGroup(label="A", entries=[NavEntry("e1")])
    g2 = NavGroup(label="B", entries=[])
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
    import dataclasses
    assert dataclasses.is_dataclass(ti)
    # frozen + slots: cannot mutate fields
    import pytest
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        ti.feature = "other"  # type: ignore[misc]


def test_tab_intent_params_default_empty_dict():
    ti = TabIntent(feature="auth", intent="my_access")
    assert ti.params == {}


def test_nav_group_visible_predicate():
    g = NavGroup(label="Admin", visible=lambda c: c.is_admin, entries=[])
    assert g.visible(_caps(is_admin=False)) is False
    assert g.visible(_caps(is_admin=True)) is True
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/shell/test_contributions.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'iris.shell'`.

- [ ] **Step 3: Implement**

Create `src/iris/shell/__init__.py`:

```python
from iris.shell.install import install

__all__ = ["install"]
```

Create `src/iris/shell/contributions.py`:

```python
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
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/shell/test_contributions.py -v
```
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/iris/shell/__init__.py src/iris/shell/contributions.py tests/shell/test_contributions.py
git commit -m "$(cat <<'EOF'
feat(shell): typed Contributions registry — nav-only at MVP

Frozen dataclasses for NavEntry/NavGroup/TabIntent with capability
predicates (visible) and dynamic derivers (badge, children). NavRegistry
is mutable and per-app on app.state.contributions; features extend it
from their install(app). Per the spec's discipline rule, only `nav` is
shipped as an extension point; new registries are added one at a time
when a real cross-feature integration motivates one.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.3: `shell/element_id.py` — id helper

**Files:**
- Create: `src/iris/shell/element_id.py`
- Test: `tests/shell/test_element_id.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/shell/test_element_id.py
from iris.shell.element_id import el, tab_button_id, tab_panel_id


def test_el_combines_tab_id_and_parts():
    assert el("AB12CD34", "results") == "t-AB12CD34-results"


def test_el_handles_multiple_parts():
    assert el("AB12CD34", "row", "5", "edit") == "t-AB12CD34-row-5-edit"


def test_el_with_no_parts_returns_prefix_only():
    assert el("AB12CD34") == "t-AB12CD34-"


def test_tab_button_id_format():
    assert tab_button_id("AB12CD34") == "tab-button-AB12CD34"


def test_tab_panel_id_format():
    assert tab_panel_id("AB12CD34") == "tab-content-AB12CD34"
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/shell/test_element_id.py -v
```
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/iris/shell/element_id.py
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
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/shell/test_element_id.py -v
```
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/iris/shell/element_id.py tests/shell/test_element_id.py
git commit -m "$(cat <<'EOF'
feat(shell): el(), tab_button_id(), tab_panel_id() helpers

Server-side id derivation so multiple tabs of the same feature can coexist
without DOM id collisions. Exposed as Jinja globals in a later task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.4: `shell/tabs.py` — tab id + session.data helpers

**Files:**
- Create: `src/iris/shell/tabs.py`
- Test: `tests/shell/test_tabs_helpers.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/shell/test_tabs_helpers.py
from __future__ import annotations

import re

import pytest

from iris.shell.tabs import (
    MAX_TABS_PER_SESSION, TabRecord, TabCapExceeded,
    new_tab_id, list_tabs, find_tab,
    append_tab, remove_tab, replace_tab,
)


def test_new_tab_id_is_url_safe_8_chars():
    tid = new_tab_id()
    assert len(tid) == 8
    assert re.fullmatch(r"[A-Za-z0-9_-]{8}", tid)


def test_new_tab_id_is_random():
    seen = {new_tab_id() for _ in range(100)}
    assert len(seen) == 100  # collision-free at small N


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
    data: dict = {}
    rec = TabRecord(id="X", feature="auth", intent="my_access",
                    params={}, title="T")
    append_tab(data, rec)
    assert data["tabs"] == [{"id": "X", "feature": "auth",
                              "intent": "my_access", "params": {}, "title": "T"}]


def test_append_tab_enforces_cap():
    data: dict = {"tabs": []}
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
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/shell/test_tabs_helpers.py -v
```
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/iris/shell/tabs.py
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
    """Wire-shape for one tab. Mirrors the dict stored in ``session.data['tabs']``."""
    id: str
    feature: str
    intent: str
    params: dict[str, Any]
    title: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "feature": self.feature,
            "intent": self.intent,
            "params": self.params,
            "title": self.title,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "TabRecord":
        return cls(
            id=d["id"],
            feature=d["feature"],
            intent=d["intent"],
            params=d.get("params", {}),
            title=d.get("title", ""),
        )


def new_tab_id() -> str:
    """8-char URL-safe random tab id."""
    return secrets.token_urlsafe(6)  # 6 random bytes → 8 base64url chars


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
        raise TabCapExceeded(
            f"session has {len(tabs)} tabs; cap is {MAX_TABS_PER_SESSION}"
        )
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
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/shell/test_tabs_helpers.py -v
```
Expected: PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add src/iris/shell/tabs.py tests/shell/test_tabs_helpers.py
git commit -m "$(cat <<'EOF'
feat(shell): tab id generation and session.data helpers

Pure helpers over session.data['tabs']: new_tab_id, list_tabs, find_tab,
append_tab (with MAX_TABS_PER_SESSION cap), remove_tab, replace_tab.
Routes call these then await session.persist_data() to flush. The cap
bounds session row size and protects against runaway client behavior.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.5: `shell/intent_dispatch.py` — IntentHandler protocol + dispatcher

**Files:**
- Create: `src/iris/shell/intent_dispatch.py`
- Test: `tests/shell/test_intent_dispatch.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/shell/test_intent_dispatch.py
from __future__ import annotations

import pytest

from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
from iris.shell.intent_dispatch import (
    IntentDispatcher, IntentForbidden, IntentNotFound, IntentSpec,
)


def _admin_caps() -> Capabilities:
    return Capabilities(
        is_admin=True, can_create_database=False,
        db_admin=frozenset(), db_writer=frozenset(), db_reader=frozenset(),
    )


def test_register_and_resolve_returns_spec():
    d = IntentDispatcher()
    spec = IntentSpec(
        feature="auth", intent="my_access",
        title=lambda params: "My access",
        required=lambda c: True,
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
        title=lambda p: "Admin",
        required=lambda c: c.is_admin,
    ))
    d.check("auth", "admin", _admin_caps())  # no raise


def test_check_capability_raises_when_predicate_false():
    d = IntentDispatcher()
    d.register(IntentSpec(
        feature="auth", intent="admin",
        title=lambda p: "Admin",
        required=lambda c: c.is_admin,
    ))
    with pytest.raises(IntentForbidden):
        d.check("auth", "admin", EMPTY_CAPABILITIES)


def test_title_called_with_params():
    d = IntentDispatcher()
    d.register(IntentSpec(
        feature="auth", intent="manage",
        title=lambda p: f"Manage {p['database']}",
        required=lambda c: True,
    ))
    spec = d.resolve("auth", "manage")
    assert spec.title({"database": "marketing"}) == "Manage marketing"


def test_register_duplicate_raises_value_error():
    d = IntentDispatcher()
    spec = IntentSpec(
        feature="auth", intent="my_access",
        title=lambda p: "x", required=lambda c: True,
    )
    d.register(spec)
    with pytest.raises(ValueError):
        d.register(spec)
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/shell/test_intent_dispatch.py -v
```
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/iris/shell/intent_dispatch.py
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
    title: Callable[[dict[str, Any]], str]      # params → tab title
    required: Callable[[Capabilities], bool]    # capability predicate (intent gate)


class IntentDispatcher:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], IntentSpec] = {}

    def register(self, spec: IntentSpec) -> None:
        key = (spec.feature, spec.intent)
        if key in self._by_key:
            raise ValueError(f"intent already registered: {key}")
        self._by_key[key] = spec

    def resolve(self, feature: str, intent: str) -> IntentSpec:
        try:
            return self._by_key[(feature, intent)]
        except KeyError as e:
            raise IntentNotFound(f"unknown intent: {(feature, intent)}") from e

    def check(self, feature: str, intent: str, caps: Capabilities) -> IntentSpec:
        """Resolve + capability check. Returns the spec on success."""
        spec = self.resolve(feature, intent)
        if not spec.required(caps):
            raise IntentForbidden(f"capability gate failed for {(feature, intent)}")
        return spec
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/shell/test_intent_dispatch.py -v
```
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/iris/shell/intent_dispatch.py tests/shell/test_intent_dispatch.py
git commit -m "$(cat <<'EOF'
feat(shell): IntentDispatcher — register, resolve, check capability

Maps (feature, intent) to IntentSpec containing the title function and
the required-capability predicate. Implements layer 2 of the spec's
defense in depth (intent gate before tab open). Render functions live
on per-feature HTTP routes; this dispatcher's job is metadata + cap gate.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.6: `shell/nav_render.py` — capability-filtered HTML

**Files:**
- Create: `src/iris/shell/nav_render.py`
- Test: `tests/shell/test_nav_render.py`

The nav rendering is **string-building HTML**, not a Jinja template — predictable, fast to test, and avoids template-loader gymnastics in the test. The shell template (next task) embeds the rendered nav as a `{{ nav_html | safe }}` block.

- [ ] **Step 1: Write the failing test**

```python
# tests/shell/test_nav_render.py
from __future__ import annotations

from iris.auth.rights import Capabilities, EMPTY_CAPABILITIES
from iris.shell.contributions import (
    Contributions, NavEntry, NavGroup, TabIntent,
)
from iris.shell.nav_render import render_nav


def _caps(**kw) -> Capabilities:
    return Capabilities(
        is_admin=kw.get("is_admin", False),
        can_create_database=kw.get("can_create_database", False),
        db_admin=frozenset(kw.get("db_admin", ())),
        db_writer=frozenset(kw.get("db_writer", ())),
        db_reader=frozenset(kw.get("db_reader", ())),
    )


def test_render_empty_contributions_yields_empty_nav():
    html = render_nav(Contributions(), EMPTY_CAPABILITIES)
    assert '<nav class="iris-nav">' in html
    assert '</nav>' in html
    # No groups rendered
    assert 'iris-nav-group' not in html


def test_render_group_with_one_entry():
    c = Contributions()
    c.nav.add(NavGroup(label="Authorization", entries=[NavEntry("My access")]))
    html = render_nav(c, EMPTY_CAPABILITIES)
    assert "Authorization" in html
    assert "My access" in html


def test_invisible_group_is_omitted():
    c = Contributions()
    c.nav.add(NavGroup(
        label="Org admin",
        visible=lambda caps: caps.is_admin,
        entries=[NavEntry("All users")],
    ))
    html = render_nav(c, _caps(is_admin=False))
    assert "Org admin" not in html
    html2 = render_nav(c, _caps(is_admin=True))
    assert "Org admin" in html2


def test_invisible_entry_is_omitted():
    c = Contributions()
    c.nav.add(NavGroup(
        label="Auth",
        entries=[
            NavEntry("Always"),
            NavEntry("Admin only", visible=lambda caps: caps.is_admin),
        ],
    ))
    html = render_nav(c, _caps(is_admin=False))
    assert "Always" in html
    assert "Admin only" not in html


def test_entry_with_on_click_emits_post_to_api_tabs():
    c = Contributions()
    c.nav.add(NavGroup(label="Auth", entries=[
        NavEntry("My access", on_click=TabIntent("auth", "my_access")),
    ]))
    html = render_nav(c, EMPTY_CAPABILITIES)
    # The action posts to the tab-open endpoint with the intent encoded
    assert "@post" in html
    assert "/api/tabs" in html
    assert "auth" in html
    assert "my_access" in html


def test_entry_with_params_encodes_params_in_post_body():
    c = Contributions()
    c.nav.add(NavGroup(label="Auth", entries=[
        NavEntry("Manage marketing",
                 on_click=TabIntent("auth", "manage", {"database": "marketing"})),
    ]))
    html = render_nav(c, EMPTY_CAPABILITIES)
    assert "marketing" in html


def test_badge_renders_when_predicate_returns_string():
    c = Contributions()
    c.nav.add(NavGroup(label="Auth", entries=[
        NavEntry(
            "DBs I admin",
            badge=lambda caps: str(len(caps.db_admin)) if caps.db_admin else None,
        ),
    ]))
    html_no_badge = render_nav(c, _caps(db_admin=()))
    assert "iris-nav-badge" not in html_no_badge

    html_with_badge = render_nav(c, _caps(db_admin=("a", "b", "c")))
    assert "iris-nav-badge" in html_with_badge
    assert ">3<" in html_with_badge


def test_dynamic_children_render_inline_under_threshold():
    c = Contributions()
    c.nav.add(NavGroup(label="Auth", entries=[
        NavEntry(
            "DBs I admin",
            children=lambda caps: [NavEntry(d) for d in sorted(caps.db_admin)],
        ),
    ]))
    html = render_nav(c, _caps(db_admin=("z", "a")))
    # Both children appear inline (under 10-entry threshold)
    assert "<li" in html  # children render as list items
    # Sorted alphabetically
    a_pos = html.index(">a<")
    z_pos = html.index(">z<")
    assert a_pos < z_pos


def test_dynamic_children_collapse_to_popover_above_threshold():
    c = Contributions()
    c.nav.add(NavGroup(label="Auth", entries=[
        NavEntry(
            "DBs I admin",
            children=lambda caps: [NavEntry(d) for d in sorted(caps.db_admin)],
        ),
    ]))
    many = tuple(f"db{i:02}" for i in range(15))
    html = render_nav(c, _caps(db_admin=many))
    assert "iris-nav-popover" in html
    assert "db00" in html and "db14" in html  # all still present


def test_html_escapes_label_with_html_chars():
    c = Contributions()
    c.nav.add(NavGroup(label="<script>", entries=[
        NavEntry("ok"),
    ]))
    html = render_nav(c, EMPTY_CAPABILITIES)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/shell/test_nav_render.py -v
```
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/iris/shell/nav_render.py
"""Render the left-panel navigation server-side from a Contributions
registry, filtered by the session's Capabilities.

HTML is built as plain strings (escape via html.escape) rather than a
Jinja template — easier to test, avoids loader gymnastics, and the
output is small enough that string concatenation is fine.

Output structure:

    <nav class="iris-nav">
      <div class="iris-nav-group">
        <h3 class="iris-nav-group-label">Authorization</h3>
        <ul>
          <li class="iris-nav-entry">
            <button data-on:click="@post('/api/tabs?feature=auth&intent=my_access')">
              My access
            </button>
          </li>
          <li class="iris-nav-entry">
            <span class="iris-nav-entry-label">Databases I admin</span>
            <span class="iris-nav-badge">3</span>
            <ul class="iris-nav-children">  <!-- or .iris-nav-popover above threshold -->
              <li>...</li>
            </ul>
          </li>
        </ul>
      </div>
    </nav>
"""
from __future__ import annotations

import html
import json
from collections.abc import Sequence

from iris.auth.rights import Capabilities
from iris.shell.contributions import (
    Contributions, NavEntry, NavGroup, TabIntent,
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
        parts.append(
            f'<button data-on:click="{html.escape(action, quote=True)}">'
            f'{html.escape(entry.label)}</button>'
        )
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

    The feature/intent/params land in the query string; the route reads them
    from FastAPI Query parameters. params is JSON-encoded and url-safe-base64
    is not needed because Datastar URL-encodes the action string when emitting.
    """
    params_json = json.dumps(intent.params, sort_keys=True)
    # Use single quotes inside since the attribute is wrapped in double quotes
    # by html.escape later. Datastar parses the expression as JS.
    return (
        f"@post('/api/tabs?feature={intent.feature}&intent={intent.intent}"
        f"&params={_url_encode(params_json)}')"
    )


def _url_encode(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/shell/test_nav_render.py -v
```
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add src/iris/shell/nav_render.py tests/shell/test_nav_render.py
git commit -m "$(cat <<'EOF'
feat(shell): render_nav — capability-filtered HTML for the left panel

Plain string building with html.escape; consumes a Contributions registry
and a Capabilities snapshot, emits a <nav> tree where invisible groups/
entries are omitted, badges and dynamic children are evaluated lazily,
and overlong children lists collapse to a popover above 10 entries.
on_click intents become @post('/api/tabs?...') Datastar actions.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.7: Refactor `templates.py` to registry pattern (spec §7 step 2 partial — loader only; auth-template move comes in Task 1.8)

**Files:**
- Modify: `src/iris/templates.py`
- Test: `tests/shell/test_templates_loader.py`

Doing the loader refactor now (rather than in Phase 2) because the shell needs it: the shell template lives at `src/iris/shell/templates/shell.html`, not under the legacy `src/iris/templates/` directory.

- [ ] **Step 1: Write the failing test**

```python
# tests/shell/test_templates_loader.py
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def reset_templates_registry():
    """templates._dirs is module-state; reset between tests."""
    import iris.templates
    importlib.reload(iris.templates)
    yield
    importlib.reload(iris.templates)


def test_register_template_dir_appends_to_search_path(tmp_path: Path):
    import iris.templates as t
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    (d1 / "a.html").write_text("from d1")
    (d2 / "b.html").write_text("from d2")
    t.register_template_dir(d1)
    t.register_template_dir(d2)
    templates = t.init_templates()
    # Both templates resolvable
    assert templates.get_template("a.html").render() == "from d1"
    assert templates.get_template("b.html").render() == "from d2"


def test_init_templates_with_no_dirs_raises():
    import iris.templates as t
    with pytest.raises(RuntimeError, match="no template directories registered"):
        t.init_templates()


def test_register_template_dir_after_init_raises(tmp_path: Path):
    import iris.templates as t
    d = tmp_path / "d"
    d.mkdir()
    t.register_template_dir(d)
    t.init_templates()
    with pytest.raises(RuntimeError, match="already initialized"):
        t.register_template_dir(d)


def test_first_match_wins_when_paths_collide(tmp_path: Path):
    import iris.templates as t
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir(); d2.mkdir()
    (d1 / "x.html").write_text("d1 wins")
    (d2 / "x.html").write_text("d2 loses")
    t.register_template_dir(d1)
    t.register_template_dir(d2)
    templates = t.init_templates()
    assert templates.get_template("x.html").render() == "d1 wins"
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/shell/test_templates_loader.py -v
```
Expected: FAIL with `AttributeError` for `register_template_dir` / `init_templates`.

- [ ] **Step 3: Implement**

Replace `src/iris/templates.py` with:

```python
"""Process-wide Jinja loader registry.

Each subsystem / feature ``install(app)`` calls
``register_template_dir(Path(__file__).parent / "templates")`` early in its
body. ``build_app()`` then calls ``init_templates()`` once after all
``install``s have run, and stashes the result on ``app.state.templates``.

First-registered wins on path collisions (FileSystemLoader default).
Subsystems should namespace their templates by directory
(``shell/shell.html``, ``auth/forbidden.html``, …) to avoid collisions.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

_dirs: list[Path] = []
_initialized: bool = False


def register_template_dir(path: Path) -> None:
    """Append a template search dir. Must be called before ``init_templates``."""
    if _initialized:
        raise RuntimeError(
            "iris.templates already initialized; register_template_dir "
            + "must be called before init_templates"
        )
    _dirs.append(path)


def init_templates() -> Jinja2Templates:
    """Build the Jinja2Templates loader from the registered dirs.

    Idempotent: returns the same loader on subsequent calls (so tests and
    multiple ``build_app`` calls coexist). Reset by reloading the module.
    """
    global _initialized
    if not _dirs:
        raise RuntimeError("no template directories registered")
    _initialized = True
    return Jinja2Templates(directory=_dirs)
```

Note: the legacy `TEMPLATES = Jinja2Templates(...)` constant is removed. `iris.app` and `iris.auth.routes.install` need to be updated to use the new pattern. Those edits land in tasks 1.10 and 1.11 (shell wiring) and Task 2.1 (auth) — for now, we keep the existing callers working by adding a backward-shim:

After the `init_templates` definition, also add:

```python
# Backward shim: existing callers (iris.app, iris.auth.routes.install) still
# import TEMPLATES at module-import time. Until those callers migrate to the
# init_templates pattern (next tasks), expose a lazily-built TEMPLATES that
# pre-registers the legacy directory if no dirs have been registered yet.
def _legacy_default() -> Jinja2Templates:
    if not _dirs:
        register_template_dir(Path(__file__).parent / "templates")
    return init_templates()


class _LazyTemplates:
    """Stand-in that defers to init_templates on first attribute access."""
    _real: Jinja2Templates | None = None

    def _resolve(self) -> Jinja2Templates:
        if self._real is None:
            self._real = _legacy_default()
        return self._real

    def __getattr__(self, name: str):
        return getattr(self._resolve(), name)


TEMPLATES = _LazyTemplates()  # type: ignore[assignment]
```

This shim keeps the existing imports (`from iris.templates import TEMPLATES`) working until Task 1.10 migrates them, then Task 1.10 removes the shim.

- [ ] **Step 4: Run all existing tests + new test to verify nothing broke**

```bash
uv run pytest tests/test_app.py tests/shell/test_templates_loader.py -v
```
Expected: PASS (existing app tests + 4 new loader tests).

- [ ] **Step 5: Commit**

```bash
git add src/iris/templates.py tests/shell/test_templates_loader.py
git commit -m "$(cat <<'EOF'
feat(templates): register_template_dir / init_templates registry

Subsystems and feature modules call register_template_dir(...) from their
install(app); build_app calls init_templates() once after all installs.
First-registered wins on path collisions, so subsystems namespace their
templates by directory (auth/, shell/, etc.).

A _LazyTemplates shim preserves the legacy `from iris.templates import
TEMPLATES` imports during the migration; it pre-registers the legacy
templates dir on first attribute access. The shim is removed in a later
task once all callers move to the registry pattern.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.8: Shell base templates and CSS

**Files:**
- Create: `src/iris/shell/templates/shell.html`
- Create: `src/iris/shell/templates/_top_buttons.html`
- Create: `src/iris/shell/templates/_account_popover.html`
- Create: `src/iris/shell/templates/_tab_strip.html`
- Create: `src/iris/shell/templates/_tab_panel.html`
- Create: `src/iris/shell/static/shell.css`

No tests at this step — these are referenced by the shell route in Task 1.10, which is where rendering is asserted end-to-end.

- [ ] **Step 1: Create `shell.html`**

```html
{# src/iris/shell/templates/shell.html #}
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Iris{% endblock %}</title>
  <script type="module" src="/static/datastar.js"></script>
  <link rel="stylesheet" href="/static/shell/shell.css">
</head>
<body data-signals='{"active": {{ active_tab_id | tojson }}, "tabs": {{ tabs_signal | tojson }}, "nav_collapsed": false, "account_open": false}'
      data-attr:data-nav-collapsed="$nav_collapsed">

  <aside class="iris-left-panel">
    {% include "shell/_top_buttons.html" %}
    {{ nav_html | safe }}
  </aside>

  <main class="iris-right-panel">
    <div id="tab-strip" class="iris-tab-strip">
      {% for tab in tabs %}
      <button id="tab-button-{{ tab.id }}"
              data-on:click="$active = {{ tab.id | tojson }}"
              data-class="{active: $active === {{ tab.id | tojson }}}">
        {{ tab.title }}
        <span class="iris-tab-close"
              data-on:click.stop="@delete('/api/tabs/{{ tab.id }}')">&times;</span>
      </button>
      {% endfor %}
    </div>

    <div id="tab-content" class="iris-tab-content">
      {% for tab in tabs %}
      <div id="tab-content-{{ tab.id }}"
           class="iris-tab-panel"
           data-show="$active === {{ tab.id | tojson }}"
           data-init="@get('/feature/{{ tab.feature }}/{{ tab.id }}/render')">
      </div>
      {% endfor %}
    </div>
  </main>

  {% include "shell/_account_popover.html" %}
</body>
</html>
```

- [ ] **Step 2: Create `_top_buttons.html`**

```html
{# src/iris/shell/templates/_top_buttons.html #}
<div class="iris-top-buttons">
  <button class="iris-top-btn"
          aria-label="Toggle nav"
          data-on:click="$nav_collapsed = !$nav_collapsed">
    <span data-show="!$nav_collapsed">&#9664;</span>
    <span data-show="$nav_collapsed">&#9654;</span>
  </button>
  <button class="iris-top-btn"
          aria-label="Settings"
          data-on:click="@post('/api/tabs?feature=settings&amp;intent=home&amp;params=%7B%7D')"
          disabled
          title="Settings (not yet available)">&#9881;</button>
  <button class="iris-top-btn"
          aria-label="Account"
          data-on:click="$account_open = !$account_open">&#128100;</button>
</div>
```

(Settings is disabled for now — the Settings feature doesn't exist yet. The button is present so the layout is final.)

- [ ] **Step 3: Create `_account_popover.html`**

```html
{# src/iris/shell/templates/_account_popover.html #}
<div class="iris-account-popover" data-show="$account_open">
  <div class="iris-account-info">
    <strong>{{ user.display_name }}</strong>
    <div class="iris-account-groups">{{ user.groups | join(", ") }}</div>
  </div>
  <form method="post" action="/logout">
    <input type="hidden" name="_csrf_token" value="{{ csrf_token }}">
    <button type="submit">Sign out</button>
  </form>
</div>
```

- [ ] **Step 4: Create `_tab_strip.html` and `_tab_panel.html`**

These are tiny partials used by SSE responses to inject one tab button / one panel:

```html
{# src/iris/shell/templates/_tab_strip.html — single tab button to append #}
<button id="tab-button-{{ tab.id }}"
        data-on:click="$active = {{ tab.id | tojson }}"
        data-class="{active: $active === {{ tab.id | tojson }}}">
  {{ tab.title }}
  <span class="iris-tab-close"
        data-on:click.stop="@delete('/api/tabs/{{ tab.id }}')">&times;</span>
</button>
```

```html
{# src/iris/shell/templates/_tab_panel.html — single empty panel to append #}
<div id="tab-content-{{ tab.id }}"
     class="iris-tab-panel"
     data-show="$active === {{ tab.id | tojson }}"
     data-init="@get('/feature/{{ tab.feature }}/{{ tab.id }}/render')">
</div>
```

- [ ] **Step 5: Create `shell/static/shell.css`**

```css
/* src/iris/shell/static/shell.css */
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  font-family: system-ui, -apple-system, sans-serif;
  display: grid;
  grid-template-columns: 240px 1fr;
  color: #1a1a1a;
}
body[data-nav-collapsed="true"] {
  grid-template-columns: 56px 1fr;
}

/* Left panel */
.iris-left-panel {
  border-right: 1px solid #ddd;
  padding: 0.5rem;
  overflow-y: auto;
}
.iris-top-buttons {
  display: flex;
  gap: 0.25rem;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid #eee;
}
.iris-top-btn {
  background: none;
  border: 1px solid #ddd;
  border-radius: 4px;
  cursor: pointer;
  padding: 0.25rem 0.5rem;
}
.iris-nav { padding-top: 0.5rem; }
.iris-nav-group { margin-bottom: 1rem; }
.iris-nav-group-label {
  font-size: 0.75rem;
  text-transform: uppercase;
  color: #888;
  margin: 0.5rem 0 0.25rem;
}
.iris-nav ul { list-style: none; padding: 0; margin: 0; }
.iris-nav-entry { padding: 0.15rem 0; }
.iris-nav-entry button,
.iris-nav-entry-label {
  background: none; border: none; cursor: pointer;
  text-align: left; padding: 0.2rem 0.4rem; width: 100%;
  border-radius: 4px;
}
.iris-nav-entry button:hover { background: #f4f4f4; }
.iris-nav-badge {
  display: inline-block;
  background: #eee; border-radius: 999px;
  padding: 0 0.4rem; font-size: 0.75rem;
}
.iris-nav-popover {
  max-height: 12rem; overflow-y: auto;
  border: 1px solid #eee; border-radius: 4px;
  padding: 0.25rem;
}

/* Collapsed nav: hide labels, show only icons (icons aren't implemented yet) */
body[data-nav-collapsed="true"] .iris-nav-group-label,
body[data-nav-collapsed="true"] .iris-nav-entry button,
body[data-nav-collapsed="true"] .iris-nav-entry-label,
body[data-nav-collapsed="true"] .iris-nav-badge {
  display: none;
}

/* Right panel */
.iris-right-panel {
  display: grid;
  grid-template-rows: auto 1fr;
  height: 100vh;
}
.iris-tab-strip {
  display: flex;
  gap: 0.25rem;
  padding: 0.5rem;
  border-bottom: 1px solid #ddd;
  overflow-x: auto;
}
.iris-tab-strip button {
  border: 1px solid #ddd; border-radius: 4px 4px 0 0;
  background: #fafafa; cursor: pointer;
  padding: 0.4rem 0.75rem;
}
.iris-tab-strip button.active { background: #fff; font-weight: 600; }
.iris-tab-close { margin-left: 0.5rem; opacity: 0.5; }
.iris-tab-close:hover { opacity: 1; }
.iris-tab-content { position: relative; overflow-y: auto; padding: 1rem; }
.iris-tab-panel { /* visibility controlled by data-show */ }

/* Account popover */
.iris-account-popover {
  position: fixed; top: 1rem; left: 4rem;
  background: #fff; border: 1px solid #ddd; border-radius: 4px;
  padding: 0.75rem; box-shadow: 0 4px 12px rgba(0,0,0,0.1);
  z-index: 100;
}
.iris-account-groups { color: #888; font-size: 0.85rem; margin: 0.25rem 0 0.5rem; }
```

- [ ] **Step 6: Commit**

No tests yet (these are pure template/CSS files; coverage comes via the route tests in Task 1.10).

```bash
git add src/iris/shell/templates/ src/iris/shell/static/
git commit -m "$(cat <<'EOF'
feat(shell): base templates and CSS for the two-panel shell

shell.html is the new base: two-panel grid (240px nav | content), tab
strip, lazy-loaded tab panels (data-init="@get(/feature/.../render)").
Tab list is read from the server-side session.data['tabs'] at render
time and re-emitted as a Datastar signal so client state can flip
$active without re-fetching. _tab_strip.html and _tab_panel.html are
single-tab partials used by SSE responses on tab open. _top_buttons.html
holds the three shell buttons (collapse, settings (disabled), account).
_account_popover.html holds the sign-out form.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.9: `shell/install.py` — wire Contributions, IntentDispatcher, static, template dir

**Files:**
- Create: `src/iris/shell/install.py`

This task is not test-covered standalone; it's smoke-tested by Task 1.10 (routes).

- [ ] **Step 1: Implement**

```python
# src/iris/shell/install.py
"""Wire the shell into a FastAPI app.

Order matters: ``iris.shell.install`` must be called BEFORE any feature's
install (features assume ``app.state.contributions`` and
``app.state.intent_dispatcher`` exist). ``build_app()`` enforces:
auth → clickhouse → shell → features.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from iris.shell.contributions import Contributions
from iris.shell.intent_dispatch import IntentDispatcher
from iris.templates import register_template_dir


def install(app: FastAPI) -> None:
    app.state.contributions = Contributions()
    app.state.intent_dispatcher = IntentDispatcher()

    register_template_dir(Path(__file__).parent / "templates")

    app.mount(
        "/static/shell",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="shell-static",
    )

    from iris.shell.routes import install_routes
    install_routes(app)
```

- [ ] **Step 2: Commit (no test on its own; Task 1.10 covers smoke)**

```bash
git add src/iris/shell/install.py
git commit -m "$(cat <<'EOF'
feat(shell): install(app) — Contributions, dispatcher, static, routes

Initializes app.state.contributions and app.state.intent_dispatcher,
registers the shell templates dir, mounts /static/shell, and calls into
shell.routes.install_routes (which registers the GET /, POST /api/tabs,
DELETE /api/tabs/{id}, PATCH /api/tabs/{id}, and GET /feature/.../render
endpoints).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.10: `shell/routes.py` — GET / + the four /api/tabs routes + render proxy

**Files:**
- Create: `src/iris/shell/routes.py`
- Test: `tests/shell/test_shell_home.py`
- Test: `tests/shell/test_shell_routes.py`

This is the biggest single task. Implementing it as one unit (vs splitting into per-route micro-tasks) keeps the route module coherent — splitting would force premature design of internal helpers.

- [ ] **Step 1: Write the failing tests — home page**

```python
# tests/shell/test_shell_home.py
from __future__ import annotations

import asyncio


def test_get_home_renders_shell_layout(authed_client):
    r = authed_client.get("/")
    assert r.status_code == 200
    body = r.text
    assert '<aside class="iris-left-panel">' in body
    assert '<main class="iris-right-panel">' in body
    assert 'id="tab-strip"' in body
    assert 'id="tab-content"' in body


def test_home_seeds_tabs_signal_from_session_data(app, capability_session):
    """If session.data['tabs'] has entries, they appear in the rendered tab strip."""
    client, sid = asyncio.run(capability_session())
    store = app.state.auth_session_store
    asyncio.run(store.update_data(sid, {"tabs": [
        {"id": "AB12CD34", "feature": "auth", "intent": "my_access",
         "params": {}, "title": "My access"},
    ]}))
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="tab-button-AB12CD34"' in r.text
    assert 'id="tab-content-AB12CD34"' in r.text
    assert 'My access' in r.text


def test_home_includes_account_popover(authed_client):
    r = authed_client.get("/")
    assert "iris-account-popover" in r.text
    assert "Sign out" in r.text
```

- [ ] **Step 2: Write the failing tests — `/api/tabs` routes**

```python
# tests/shell/test_shell_routes.py
from __future__ import annotations

import asyncio
import json
import urllib.parse


def _datastar_headers(client) -> dict[str, str]:
    return {
        "Datastar-Request": "true",
        "X-CSRF-Token": client.cookies.get("iris_csrf") or "",
    }


def _bootstrap_csrf(client):
    """GET / sets the CSRF cookie."""
    client.get("/")


def test_post_tabs_unknown_intent_returns_400(authed_client):
    _bootstrap_csrf(authed_client)
    r = authed_client.post(
        "/api/tabs",
        params={"feature": "ghost", "intent": "x", "params": "{}"},
        headers=_datastar_headers(authed_client),
    )
    assert r.status_code == 400


def test_post_tabs_without_csrf_returns_400(authed_client):
    _bootstrap_csrf(authed_client)
    r = authed_client.post(
        "/api/tabs",
        params={"feature": "auth", "intent": "my_access", "params": "{}"},
        headers={"Datastar-Request": "true"},  # no X-CSRF-Token
    )
    assert r.status_code == 400


def test_delete_tab_returns_204_when_absent(authed_client):
    _bootstrap_csrf(authed_client)
    r = authed_client.delete(
        "/api/tabs/UNKNOWN1",
        headers=_datastar_headers(authed_client),
    )
    # No-op deletes return 204 (idempotent)
    assert r.status_code == 204


def test_delete_tab_removes_from_session_data(app, capability_session, parse_sse):
    client, sid = asyncio.run(capability_session())
    store = app.state.auth_session_store
    asyncio.run(store.update_data(sid, {"tabs": [
        {"id": "AB12CD34", "feature": "auth", "intent": "my_access",
         "params": {}, "title": "My access"},
    ]}))
    _bootstrap_csrf(client)
    r = client.delete(
        "/api/tabs/AB12CD34",
        headers={"Datastar-Request": "true",
                 "X-CSRF-Token": client.cookies.get("iris_csrf") or ""},
    )
    assert r.status_code == 200
    # SSE removes the button + panel
    events = parse_sse(r.text)
    targets = " ".join(e.data for e in events)
    assert "tab-button-AB12CD34" in targets
    assert "tab-content-AB12CD34" in targets
    # Session.data['tabs'] is now empty
    refreshed = asyncio.run(store.get_and_refresh(sid))
    assert refreshed is not None
    assert refreshed.data.get("tabs", []) == []


def test_render_route_returns_404_for_unknown_tab(authed_client):
    r = authed_client.get("/feature/auth/UNKNOWN1/render")
    assert r.status_code == 404


def test_render_route_returns_404_for_unknown_feature(app, capability_session):
    client, sid = asyncio.run(capability_session())
    store = app.state.auth_session_store
    asyncio.run(store.update_data(sid, {"tabs": [
        {"id": "AB12CD34", "feature": "ghost", "intent": "x",
         "params": {}, "title": "T"},
    ]}))
    r = client.get("/feature/ghost/AB12CD34/render")
    assert r.status_code == 404
```

- [ ] **Step 3: Run to verify they fail**

```bash
uv run pytest tests/shell/test_shell_home.py tests/shell/test_shell_routes.py -v
```
Expected: All FAIL — routes don't exist yet.

- [ ] **Step 4: Implement `shell/routes.py`**

```python
# src/iris/shell/routes.py
"""Shell routes: home page, tab lifecycle, feature-render proxy."""
from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import (
    Depends, FastAPI, HTTPException, Query, Request, Response,
)
from fastapi.responses import HTMLResponse

from datastar_py.fastapi import DatastarResponse
from datastar_py.fastapi import ServerSentEventGenerator as SSE

from iris.auth.csrf import (
    attach_csrf_cookie, mint_csrf_token, verify_csrf_header,
)
from iris.auth.deps import Session
from iris.shell.contributions import Contributions
from iris.shell.intent_dispatch import (
    IntentDispatcher, IntentForbidden, IntentNotFound,
)
from iris.shell.nav_render import render_nav
from iris.shell.tabs import (
    TabRecord, TabCapExceeded,
    append_tab, find_tab, list_tabs, new_tab_id, remove_tab, replace_tab,
)

logger = logging.getLogger("iris.shell")


def install_routes(app: FastAPI) -> None:

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request, session: Session) -> Response:
        contribs: Contributions = request.app.state.contributions
        templates = request.app.state.templates

        nav_html = render_nav(contribs, session.capabilities)
        tabs = list_tabs(session.data)
        active_tab_id = tabs[0].id if tabs else ""
        tabs_signal = {t.id: {} for t in tabs}

        csrf = mint_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "shell/shell.html",
            {
                "user": session.user,
                "nav_html": nav_html,
                "tabs": [t.to_json() for t in tabs],
                "tabs_signal": tabs_signal,
                "active_tab_id": active_tab_id,
                "csrf_token": csrf,
            },
        )
        attach_csrf_cookie(request, response, csrf)
        return response

    @app.post("/api/tabs")
    async def open_tab(
        request: Request,
        session: Session,
        feature: Annotated[str, Query(max_length=64)],
        intent: Annotated[str, Query(max_length=64)],
        params: Annotated[str, Query(max_length=4096)] = "{}",
        _: None = Depends(verify_csrf_header),
    ) -> Response:
        dispatcher: IntentDispatcher = request.app.state.intent_dispatcher
        templates = request.app.state.templates
        try:
            params_dict = json.loads(params)
            if not isinstance(params_dict, dict):
                raise ValueError("params must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"invalid params: {e}")

        try:
            spec = dispatcher.check(feature, intent, session.capabilities)
        except IntentNotFound:
            raise HTTPException(status_code=400, detail="unknown intent")
        except IntentForbidden:
            raise HTTPException(status_code=403, detail="intent forbidden")

        tab_id = new_tab_id()
        rec = TabRecord(
            id=tab_id, feature=feature, intent=intent,
            params=params_dict, title=spec.title(params_dict),
        )
        try:
            append_tab(session.data, rec)
        except TabCapExceeded as e:
            raise HTTPException(status_code=409, detail=str(e))
        await session.persist_data()

        button_html = templates.get_template("shell/_tab_strip.html").render(
            tab=rec.to_json()
        )
        panel_html = templates.get_template("shell/_tab_panel.html").render(
            tab=rec.to_json()
        )
        return DatastarResponse([
            SSE.patch_elements(button_html, selector="#tab-strip", mode="append"),
            SSE.patch_elements(panel_html, selector="#tab-content", mode="append"),
            SSE.patch_signals({
                "tabs": {tab_id: {}},
                "active": tab_id,
            }),
        ])

    @app.delete("/api/tabs/{tab_id}")
    async def close_tab(
        request: Request,
        session: Session,
        tab_id: str,
        _: None = Depends(verify_csrf_header),
    ) -> Response:
        if find_tab(session.data, tab_id) is None:
            return Response(status_code=204)
        remove_tab(session.data, tab_id)
        await session.persist_data()
        return DatastarResponse([
            SSE.patch_elements(selector=f"#tab-button-{tab_id}", mode="remove"),
            SSE.patch_elements(selector=f"#tab-content-{tab_id}", mode="remove"),
        ])

    @app.patch("/api/tabs/{tab_id}")
    async def retarget_tab(
        request: Request,
        session: Session,
        tab_id: str,
        intent: Annotated[str, Query(max_length=64)],
        params: Annotated[str, Query(max_length=4096)] = "{}",
        _: None = Depends(verify_csrf_header),
    ) -> Response:
        existing = find_tab(session.data, tab_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="tab not found")
        dispatcher: IntentDispatcher = request.app.state.intent_dispatcher
        templates = request.app.state.templates
        try:
            params_dict = json.loads(params)
            if not isinstance(params_dict, dict):
                raise ValueError("params must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"invalid params: {e}")

        try:
            spec = dispatcher.check(existing.feature, intent, session.capabilities)
        except IntentNotFound:
            raise HTTPException(status_code=400, detail="unknown intent")
        except IntentForbidden:
            raise HTTPException(status_code=403, detail="intent forbidden")

        new_rec = TabRecord(
            id=tab_id, feature=existing.feature, intent=intent,
            params=params_dict, title=spec.title(params_dict),
        )
        replace_tab(session.data, tab_id, new_rec)
        await session.persist_data()

        # Re-render the panel via @get to /feature/<f>/<id>/render and update title
        return DatastarResponse([
            SSE.patch_signals({"active": tab_id}),
            SSE.patch_elements(
                templates.get_template("shell/_tab_strip.html").render(tab=new_rec.to_json()),
                selector=f"#tab-button-{tab_id}",
                mode="outer",
            ),
            SSE.patch_elements(
                templates.get_template("shell/_tab_panel.html").render(tab=new_rec.to_json()),
                selector=f"#tab-content-{tab_id}",
                mode="outer",
            ),
        ])

    @app.get("/feature/{feature}/{tab_id}/render")
    async def render_tab(
        request: Request,
        session: Session,
        feature: str,
        tab_id: str,
    ) -> Response:
        rec = find_tab(session.data, tab_id)
        if rec is None or rec.feature != feature:
            raise HTTPException(status_code=404, detail="tab not found")
        # Per-feature render endpoints live at
        # /feature/<feature>/{tab_id}/render and dispatch on rec.intent.
        # If no feature has registered such a route at this prefix,
        # FastAPI returns 404 below this handler. Features take over by
        # mounting their own router at /feature/<feature>; this stub
        # exists to make Phase 1 testable in isolation.
        raise HTTPException(status_code=404, detail="no feature handler")
```

- [ ] **Step 5: Wire into `iris.app`**

Modify `src/iris/app.py`. Replace the demo body (`@app.get("/")`, `@app.get("/api/greet")`, `@app.get("/api/clock")`, `_clock_stream`) with the shell installation:

```python
# src/iris/app.py
from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from iris.middleware import SecurityHeadersMiddleware
from iris.templates import init_templates


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    yield
    for hook in reversed(app.state.shutdown_hooks):
        await hook()


def build_app(*, install_clickhouse: bool = True) -> FastAPI:
    app = FastAPI(title="Iris", lifespan=_lifespan)
    shutdown_hooks: list[Callable[[], Awaitable[None]]] = []
    app.state.shutdown_hooks = shutdown_hooks

    from iris.auth.routes import install as install_auth
    install_auth(app)

    if install_clickhouse:
        from iris.clickhouse.install import install as install_clickhouse_fn
        install_clickhouse_fn(app)

    from iris.shell.install import install as install_shell
    install_shell(app)

    # Build the templates loader once all subsystems have registered their dirs
    app.state.templates = init_templates()

    app.add_middleware(SecurityHeadersMiddleware)
    return app
```

The legacy `app.mount("/static", ...)` for the global `datastar.js` is still needed. Add it back:

```python
    # ... after add_middleware:
    from pathlib import Path
    from fastapi.staticfiles import StaticFiles
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )
    return app
```

(Order: shell mounts `/static/shell`, then app mounts `/static`. FastAPI tries longest-prefix first.)

- [ ] **Step 6: Update `iris.auth.routes.install` to register its template dir**

In `src/iris/auth/routes.py`, find `def install(app: FastAPI)` and replace the line:

```python
    from iris.templates import TEMPLATES
    app.state.templates = TEMPLATES
```

with:

```python
    from pathlib import Path
    from iris.templates import register_template_dir
    register_template_dir(Path(__file__).parent.parent / "templates")
```

(Templates are still in the legacy `src/iris/templates/` directory at this point — they move in Phase 2. The `app.state.templates` assignment is now done by `build_app` after all installs.)

Also remove the `app.state.templates = TEMPLATES` line — `build_app` sets it now.

- [ ] **Step 7: Delete the demo route assertions in `tests/test_app.py`**

Replace the entire file with a smaller smoke test:

```python
# tests/test_app.py
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

    app = build_app(install_clickhouse=False)
    from iris.shell.contributions import Contributions
    from iris.shell.intent_dispatch import IntentDispatcher

    assert isinstance(app.state.contributions, Contributions)
    assert isinstance(app.state.intent_dispatcher, IntentDispatcher)


def test_app_state_has_templates():
    from iris.app import build_app

    app = build_app(install_clickhouse=False)
    assert hasattr(app.state, "templates")
```

- [ ] **Step 8: Delete demo template files**

```bash
rm src/iris/templates/index.html
rm src/iris/templates/base.html
```

(Keep `src/iris/templates/auth/forbidden.html` and `ldap_form.html` until Phase 2 moves them.)

- [ ] **Step 9: Remove the `_LazyTemplates` shim in `iris.templates`**

Now that all callers (`app.py`, `auth.routes.install`) use the registry pattern, delete the `_LazyTemplates` class and the `TEMPLATES = _LazyTemplates()` line at the bottom of `src/iris/templates.py`. Final state of the file:

```python
# src/iris/templates.py
"""Process-wide Jinja loader registry.

Each subsystem / feature install(app) calls
register_template_dir(Path(__file__).parent / "templates") early in its body.
build_app() then calls init_templates() once after all installs and stashes
the result on app.state.templates.

First-registered wins on path collisions (FileSystemLoader default).
Subsystems should namespace their templates by directory (shell/shell.html,
auth/forbidden.html, ...) to avoid collisions.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

_dirs: list[Path] = []
_initialized: bool = False


def register_template_dir(path: Path) -> None:
    if _initialized:
        raise RuntimeError(
            "iris.templates already initialized; register_template_dir "
            + "must be called before init_templates"
        )
    _dirs.append(path)


def init_templates() -> Jinja2Templates:
    global _initialized
    if not _dirs:
        raise RuntimeError("no template directories registered")
    _initialized = True
    return Jinja2Templates(directory=_dirs)
```

- [ ] **Step 10: Run all tests to verify everything passes**

```bash
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -v
```
Expected: PASS for all unit tests. Any failures in `tests/auth/test_error_pages.py` or other auth tests that load templates indicate a templates-loader-init ordering issue — the auth tests build the app and expect templates already initialized; verify `build_app` calls `init_templates()` after `install_auth`.

- [ ] **Step 11: Run gates**

```bash
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: zero issues across all three.

- [ ] **Step 12: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(shell): wire shell into build_app — home, /api/tabs, render proxy

Replaces the demo /, /api/greet, /api/clock routes with the shell:

- GET / renders shell.html with capability-filtered nav and
  session.data['tabs']-seeded tab strip + lazy panels.
- POST /api/tabs gates on intent_dispatcher.check (intent gate, layer 2
  of defense in depth), generates a tab_id, persists to session.data,
  emits SSE patches for the new button, panel, and signals.
- DELETE /api/tabs/{id} is idempotent (204 if absent), removes from
  session.data, emits SSE removes for the button + panel.
- PATCH /api/tabs/{id} re-targets an existing tab to a new intent/params,
  re-renders the title and panel.
- GET /feature/{feature}/{tab_id}/render is a 404 stub at this Phase 1
  point — feature routers will register handlers at /feature/<feature>
  starting in Phase 3.

Templates loader is now driven by register_template_dir/init_templates;
the _LazyTemplates shim is removed. build_app order is now
auth → clickhouse → shell → init_templates(). Demo files index.html,
base.html, /api/greet, /api/clock are deleted.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.11: Write `docs/frontend.md`

**Files:**
- Create: `docs/frontend.md`

- [ ] **Step 1: Write the doc**

```markdown
# Frontend module surface

Sister doc to `docs/auth.md` and `docs/clickhouse.md`. Covers the iris
shell module (`src/iris/shell/`) and the conventions feature modules
follow under `src/iris/features/<name>/`.

## Module layout

```
src/iris/shell/
├── install.py            # install(app); call BEFORE feature installs
├── contributions.py      # Contributions, NavRegistry, NavGroup, NavEntry, TabIntent
├── element_id.py         # el(tab_id, *parts), tab_button_id, tab_panel_id
├── tabs.py               # new_tab_id, list/find/append/remove/replace_tab,
│                         # MAX_TABS_PER_SESSION, TabCapExceeded
├── intent_dispatch.py    # IntentDispatcher, IntentSpec, IntentNotFound, IntentForbidden
├── nav_render.py         # render_nav(contribs, capabilities) -> str
├── routes.py             # GET /, POST/DELETE/PATCH /api/tabs, GET /feature/.../render
├── templates/
│   ├── shell.html        # base layout (replaces the legacy templates/base.html)
│   ├── _nav.html
│   ├── _tab_strip.html
│   ├── _tab_panel.html
│   ├── _account_popover.html
│   └── _top_buttons.html
└── static/
    └── shell.css
```

## install order

`build_app` calls in order:

1. `iris.auth.routes.install(app)`
2. `iris.clickhouse.install.install(app)` (skipped when `install_clickhouse=False`)
3. `iris.shell.install.install(app)` — sets `app.state.contributions` and `app.state.intent_dispatcher`
4. Each `iris.features.<name>.install(app)`
5. `iris.templates.init_templates()` — builds the Jinja loader from all registered dirs; result stashed on `app.state.templates`

Features depend on `app.state.contributions` and `app.state.intent_dispatcher` existing, so they install AFTER the shell.

## Tab system

A tab is one instance of a feature page. State lives server-side in
`session.data['tabs']` (a list of `{id, feature, intent, params, title}` dicts).
Refresh restores tabs from this list — no localStorage needed.

Conventions:

- **Tab id**: 8-char URL-safe random (`secrets.token_urlsafe(6)`); generated by
  the server in `POST /api/tabs`. Lives in URL path: `/feature/<feature>/{tab_id}/...`.
- **DOM ids inside a tab fragment**: derive from `tab_id` via the `el()` helper
  (`src/iris/shell/element_id.py`). Server-side only — never compute ids in JS.
- **Per-tab signals**: live under `$tabs.<tab_id>.*`. Initialized by
  `patch_signals({tabs: {<tab_id>: {...}}})` in the tab-open SSE response.
- **Visibility**: each panel uses `data-show="$active === '<tab_id>'"`. Tab-switching
  is purely client-side (a signal flip), no server roundtrip.
- **Cap**: `MAX_TABS_PER_SESSION = 32`, enforced server-side in `append_tab`.
  Over the cap returns 409.

## Datastar conventions

- Server is the source of truth for state. Open tabs, capabilities, persisted
  data — server-side. Signals carry only ephemeral UI state (`$active`,
  `$nav_collapsed`, form input bindings).
- All structural changes are SSE patches: `SSE.patch_elements(...)` for HTML,
  `SSE.patch_signals(...)` for signals. Returned via `DatastarResponse([events])`.
- All state-changing actions are CSRF-protected. Form POSTs use
  `verify_csrf_form`; Datastar `@post`/`@delete`/`@patch` use `verify_csrf_header`
  (token transmitted via the `X-CSRF-Token` header read from the JS-readable
  `iris_csrf` cookie).
- No JS in templates. All interactivity is via Datastar attributes
  (`data-on:*`, `data-bind`, `data-show`, `data-signals`, `data-init`).
- Lazy element initialization: `data-init="@get(...)"` fires once when the
  element enters the DOM. Used to fetch tab content lazily after the panel
  shell appears.

## Contribution registry

`app.state.contributions` is a `Contributions` instance with one nav registry
at MVP:

```python
from iris.shell.contributions import (
    Contributions, NavGroup, NavEntry, TabIntent,
)

contribs = app.state.contributions
contribs.nav.add(NavGroup(
    label="Authorization",
    visible=lambda c: True,                        # capability predicate
    entries=[
        NavEntry("My access", on_click=TabIntent("auth", "my_access")),
        NavEntry(
            "Databases I admin",
            visible=lambda c: bool(c.db_admin),
            badge=lambda c: str(len(c.db_admin)),
            children=lambda c: [                   # dynamic children list
                NavEntry(db, on_click=TabIntent("auth", "manage", {"database": db}))
                for db in sorted(c.db_admin)
            ],
        ),
    ],
))
```

Capability-aware fields (`visible`, `badge`, `children`) are evaluated
per-render against the session's `Capabilities`. Children lists with more
than 10 entries collapse into a scrollable popover.

**Discipline rule:** Do not add a new registry to `Contributions` until at
least one feature has a concrete need to contribute and at least one feature
has a concrete need to consume. Every registry is permanent API surface.

## Defense in depth

Authz is enforced at three layers per the design spec:

1. **Nav rendering** (presentation): `render_nav` skips entries whose
   `visible` predicate fails for the session's capabilities.
2. **Intent gate** (gateway): `POST /api/tabs` calls
   `dispatcher.check(feature, intent, capabilities)` which evaluates the
   intent's `required` predicate. Failure → 403.
3. **Per-route guard** (authoritative): every route inside a feature uses
   an `Annotated` `Session*` dep alias from `iris.auth.deps`. This is the
   only level that *enforces*; (1) and (2) are UX.

## Adding a feature

1. Create `src/iris/features/<name>/{__init__.py, install.py, routes.py,
   intents.py, service.py, templates/, static/}`.
2. In `install.py`: register a templates dir, register intent specs on
   `app.state.intent_dispatcher`, add nav contributions to
   `app.state.contributions.nav`, and mount your `APIRouter(prefix="/feature/<name>")`.
3. The `APIRouter` should expose `GET /feature/<name>/{tab_id}/render`
   that dispatches on `tab.intent` to the right render function from
   `intents.py`.
4. Tests live under `tests/features/test_<name>_<intent>.py`. Use the
   `capability_session` fixture from `tests/conftest.py` to mint a session
   with arbitrary capabilities and assert at all three authz layers
   (nav HTML present/absent, `POST /api/tabs` 200/403, per-route 200/403).
```

- [ ] **Step 2: Commit**

```bash
git add docs/frontend.md
git commit -m "$(cat <<'EOF'
docs(frontend): module surface, tab system, Datastar conventions

Sister doc to docs/auth.md and docs/clickhouse.md. Covers shell module
layout, install order, tab system conventions (server-side state,
dynamic ids, per-tab signals), Datastar discipline (server-rendered,
no JS in templates, CSRF on every state-changer), the Contributions
registry shape with the discipline rule, defense-in-depth layers, and
the recipe for adding a new feature.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

End of Phase 1. The shell is live, tabs work, and `GET /` renders the empty two-panel layout. No features yet → nav is empty → `POST /api/tabs` returns 400 (unknown intent). Phase 3 onward populates the nav.

---

## Phase 2 — Auth template move (spec §7 step 2 finish)

The templates loader refactor was folded into Phase 1 (Task 1.7) since the shell needed it. This phase finishes the spec's step 2 by moving the existing auth templates out of the legacy global `src/iris/templates/` directory into `src/iris/auth/templates/` so each subsystem owns its own templates.

### Task 2.1: Move `forbidden.html` and `ldap_form.html` under `src/iris/auth/templates/`

**Files:**
- Move: `src/iris/templates/auth/forbidden.html` → `src/iris/auth/templates/auth/forbidden.html`
- Move: `src/iris/templates/auth/ldap_form.html` → `src/iris/auth/templates/auth/ldap_form.html`
- Modify: `src/iris/auth/routes.py` (`install`)
- Delete: `src/iris/templates/auth/` (now empty)
- Delete: `src/iris/templates/` (now empty if no other files remain)

- [ ] **Step 1: Verify the auth tests that exercise these templates pass before the move**

```bash
uv run pytest tests/auth/test_error_pages.py tests/auth/test_provider_ldap.py -v
```
Expected: PASS. (If they don't pass before the move, fix that first.)

- [ ] **Step 2: Move the files**

```bash
mkdir -p src/iris/auth/templates/auth
git mv src/iris/templates/auth/forbidden.html src/iris/auth/templates/auth/forbidden.html
git mv src/iris/templates/auth/ldap_form.html src/iris/auth/templates/auth/ldap_form.html
rmdir src/iris/templates/auth
# src/iris/templates/ is now empty (Phase 1 deleted index.html and base.html)
rmdir src/iris/templates
```

- [ ] **Step 3: Update `iris.auth.routes.install` to register the new path**

In `src/iris/auth/routes.py`, the `install` function currently has:

```python
    from pathlib import Path
    from iris.templates import register_template_dir
    register_template_dir(Path(__file__).parent.parent / "templates")
```

(That line was added in Phase 1 Task 1.10.) Update it to point at the auth-local templates directory:

```python
    from pathlib import Path
    from iris.templates import register_template_dir
    register_template_dir(Path(__file__).parent / "templates")
```

(`Path(__file__).parent` is `src/iris/auth/`, so the templates dir is `src/iris/auth/templates/`.)

- [ ] **Step 4: Run all tests**

```bash
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -v
```
Expected: PASS. Auth template references (`templates.TemplateResponse(request, "auth/forbidden.html", ...)` in `iris/auth/exceptions.py`, `"auth/ldap_form.html"` in `iris/auth/providers/ldap.py` or `_form.py`) continue to work because the path inside `templates/` is preserved (`auth/forbidden.html`).

- [ ] **Step 5: Run gates**

```bash
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: zero issues.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(auth): move auth templates under src/iris/auth/templates/

Each subsystem owns its templates. Combined with the templates registry
(introduced in Phase 1), this means iris/templates/ no longer holds
subsystem-specific files. The auth/ prefix inside the templates path is
preserved (auth/forbidden.html, auth/ldap_form.html) so all callers
continue to work unchanged.

Removes the now-empty src/iris/templates/ directory.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

End of Phase 2. Each subsystem owns its templates; the legacy global `src/iris/templates/` directory is gone.

---

## Phase 3 — Authorization feature scaffold + `my_access` intent (spec §7 step 3)

Builds the first feature module top-to-bottom: directory structure, install hook, intent registration, nav contributions, the my_access render function, the my_access template, and a per-feature router with the `/feature/auth/{tab_id}/render` endpoint that dispatches on intent.

End state: a logged-in user can click "My access" in the nav, see a tab open with capability-adapted content. The Authorization NavGroup appears with three entries (My access, Databases I admin (cond.), Create database (cond.)). The Org admin NavGroup appears only for `is_admin`.

### Task 3.1: Feature directory skeleton + nav contributions + install wired into `build_app`

**Files:**
- Create: `src/iris/features/__init__.py`
- Create: `src/iris/features/authorization/__init__.py`
- Create: `src/iris/features/authorization/install.py`
- Modify: `src/iris/app.py` (call `install_authorization`)
- Test: `tests/features/test_authorization_install.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/features/test_authorization_install.py
"""The Authorization feature's install hook contributes nav and registers intents."""
from __future__ import annotations


def test_install_adds_authorization_nav_group(app):
    contribs = app.state.contributions
    labels = [g.label for g in contribs.nav.groups]
    assert "Authorization" in labels


def test_install_adds_org_admin_nav_group(app):
    contribs = app.state.contributions
    labels = [g.label for g in contribs.nav.groups]
    assert "Org admin" in labels


def test_org_admin_only_visible_to_admin(app):
    from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
    contribs = app.state.contributions
    org_admin_groups = [g for g in contribs.nav.groups if g.label == "Org admin"]
    assert len(org_admin_groups) == 1
    g = org_admin_groups[0]
    assert g.visible(EMPTY_CAPABILITIES) is False
    assert g.visible(Capabilities(
        is_admin=True, can_create_database=False,
        db_admin=frozenset(), db_writer=frozenset(), db_reader=frozenset(),
    )) is True


def test_install_registers_my_access_intent(app):
    dispatcher = app.state.intent_dispatcher
    spec = dispatcher.resolve("auth", "my_access")
    assert spec.feature == "auth"
    assert spec.intent == "my_access"
    assert spec.title({}) == "My access"
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/features/test_authorization_install.py -v
```
Expected: FAIL — `Authorization` not in nav groups; `IntentNotFound` for `("auth", "my_access")`.

- [ ] **Step 3: Implement `features/__init__.py` and `features/authorization/__init__.py`**

```python
# src/iris/features/__init__.py
# (empty — namespace marker)
```

```python
# src/iris/features/authorization/__init__.py
from iris.features.authorization.install import install

__all__ = ["install"]
```

- [ ] **Step 4: Implement `features/authorization/install.py`**

```python
# src/iris/features/authorization/install.py
"""Install the Authorization feature into a FastAPI app.

Registers nav contributions (Authorization + Org admin groups), intent
specs (my_access at this Phase 3 point; manage / create_database /
admin_console land in subsequent phases), the per-feature templates dir,
and mounts the feature's APIRouter at /feature/auth.

Depends on app.state.contributions and app.state.intent_dispatcher
existing — call AFTER iris.shell.install.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from iris.shell.contributions import (
    Contributions, NavEntry, NavGroup, TabIntent,
)
from iris.shell.intent_dispatch import IntentDispatcher, IntentSpec
from iris.templates import register_template_dir


def install(app: FastAPI) -> None:
    contribs: Contributions = app.state.contributions
    dispatcher: IntentDispatcher = app.state.intent_dispatcher

    register_template_dir(Path(__file__).parent / "templates")

    _register_intents(dispatcher)
    _register_nav(contribs)

    from iris.features.authorization.routes import router
    app.include_router(router)


def _register_intents(dispatcher: IntentDispatcher) -> None:
    dispatcher.register(IntentSpec(
        feature="auth",
        intent="my_access",
        title=lambda _params: "My access",
        required=lambda _c: True,
    ))


def _register_nav(contribs: Contributions) -> None:
    contribs.nav.add(NavGroup(
        label="Authorization",
        entries=[
            NavEntry("My access", on_click=TabIntent("auth", "my_access")),
            # Databases I admin / Create database land in Phase 4 / Phase 5
            # alongside the manage / create_database intents.
        ],
    ))
    contribs.nav.add(NavGroup(
        label="Org admin",
        visible=lambda c: c.is_admin,
        entries=[
            # Org admin sub-entries land in Phase 6 alongside admin_console.
        ],
    ))
```

- [ ] **Step 5: Stub `features/authorization/routes.py` so the import in `install` works**

```python
# src/iris/features/authorization/routes.py
"""APIRouter for the Authorization feature.

Mounted at /feature/auth by install. Each phase fills in more routes:
Phase 3 only handles the render-by-intent dispatch for my_access; Phase 4
adds /manage routes; Phase 5 adds /create_database; Phase 6 adds
/admin_console sub-routes.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from iris.auth.deps import Session
from iris.shell.tabs import find_tab

router = APIRouter(prefix="/feature/auth")


@router.get("/{tab_id}/render")
async def render(
    request: Request,
    session: Session,
    tab_id: str,
) -> Response:
    rec = find_tab(session.data, tab_id)
    if rec is None or rec.feature != "auth":
        raise HTTPException(status_code=404, detail="tab not found")

    from iris.features.authorization.intents import RENDER_BY_INTENT
    handler = RENDER_BY_INTENT.get(rec.intent)
    if handler is None:
        raise HTTPException(status_code=404, detail="unknown intent")
    return await handler(request, session, rec)
```

- [ ] **Step 6: Stub `features/authorization/intents.py` with an empty handler map**

```python
# src/iris/features/authorization/intents.py
"""Intent render functions for the Authorization feature.

The shell's per-feature route /feature/auth/{tab_id}/render dispatches
on tab.intent into RENDER_BY_INTENT. Phase 3 adds my_access; Phase 4 adds
manage; Phase 5 adds create_database; Phase 6 adds admin_console (and
its four sub-tab handlers).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import Request, Response

if TYPE_CHECKING:
    from iris.auth.views import AuthSession
    from iris.shell.tabs import TabRecord

IntentHandler = Callable[[Request, "AuthSession", "TabRecord"], Awaitable[Response]]

RENDER_BY_INTENT: dict[str, IntentHandler] = {}
```

- [ ] **Step 7: Wire the install into `iris.app.build_app`**

In `src/iris/app.py`, add the feature install AFTER the shell install:

```python
    from iris.shell.install import install as install_shell
    install_shell(app)

    from iris.features.authorization.install import install as install_authorization
    install_authorization(app)

    # Build the templates loader once all subsystems have registered their dirs
    app.state.templates = init_templates()
```

- [ ] **Step 8: Run to verify the test passes**

```bash
uv run pytest tests/features/test_authorization_install.py -v
```
Expected: PASS (4 tests).

- [ ] **Step 9: Run gates**

```bash
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: zero issues.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(features/authorization): scaffold install + nav + intent dispatch

Adds the feature module skeleton: features/authorization/{install,routes,
intents}.py and an empty templates/ dir. install(app) registers the
Authorization NavGroup (always visible) and the Org admin NavGroup
(visible only when capabilities.is_admin), registers the my_access
intent spec on the dispatcher, registers its templates dir, and mounts
the APIRouter at /feature/auth.

routes.py exposes GET /feature/auth/{tab_id}/render which dispatches on
tab.intent into RENDER_BY_INTENT (currently empty; my_access lands in
the next task).

build_app order is now auth → ch → shell → authorization → init_templates.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.2: `my_access` template + render function + capability-adaptive listings

**Files:**
- Create: `src/iris/features/authorization/templates/my_access.html`
- Create: `src/iris/features/authorization/service.py`
- Modify: `src/iris/features/authorization/intents.py`
- Test: `tests/features/test_authorization_my_access.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/features/test_authorization_my_access.py
"""my_access render adapts to capabilities."""
from __future__ import annotations

import asyncio


def _seed_my_access_tab(app, sid: str, tab_id: str = "AB12CD34") -> None:
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": tab_id, "feature": "auth", "intent": "my_access",
         "params": {}, "title": "My access"},
    ]}))


def test_my_access_shows_identity(app, capability_session):
    client, sid = asyncio.run(capability_session(
        username="alice", display_name="Alice",
        groups=("data-team", "dev"),
    ))
    _seed_my_access_tab(app, sid)
    r = client.get("/feature/auth/AB12CD34/render")
    assert r.status_code == 200
    assert "alice" in r.text or "Alice" in r.text
    assert "data-team" in r.text


def test_my_access_omits_reader_section_when_empty(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed_my_access_tab(app, sid)
    r = client.get("/feature/auth/AB12CD34/render")
    assert r.status_code == 200
    assert "Databases you can read" not in r.text


def test_my_access_lists_reader_databases(app, capability_session):
    client, sid = asyncio.run(capability_session(db_reader={"marketing", "analytics"}))
    _seed_my_access_tab(app, sid)
    r = client.get("/feature/auth/AB12CD34/render")
    assert r.status_code == 200
    assert "Databases you can read" in r.text
    assert "marketing" in r.text
    assert "analytics" in r.text


def test_my_access_lists_writer_and_admin_databases(app, capability_session):
    client, sid = asyncio.run(capability_session(
        db_writer={"events"}, db_admin={"sales"},
    ))
    _seed_my_access_tab(app, sid)
    r = client.get("/feature/auth/AB12CD34/render")
    assert "Databases you can write to" in r.text and "events" in r.text
    assert "Databases you administer" in r.text and "sales" in r.text


def test_my_access_shows_create_when_can_create_database(app, capability_session):
    client_no, sid_no = asyncio.run(capability_session())
    _seed_my_access_tab(app, sid_no)
    r = client_no.get("/feature/auth/AB12CD34/render")
    assert "Create new database" not in r.text

    client_yes, sid_yes = asyncio.run(capability_session(can_create_database=True))
    _seed_my_access_tab(app, sid_yes)
    r2 = client_yes.get("/feature/auth/AB12CD34/render")
    assert "Create new database" in r2.text


def test_my_access_shows_admin_console_when_is_admin(app, capability_session):
    client_no, sid_no = asyncio.run(capability_session())
    _seed_my_access_tab(app, sid_no)
    r = client_no.get("/feature/auth/AB12CD34/render")
    assert "Open admin console" not in r.text

    client_yes, sid_yes = asyncio.run(capability_session(is_admin=True))
    _seed_my_access_tab(app, sid_yes)
    r2 = client_yes.get("/feature/auth/AB12CD34/render")
    assert "Open admin console" in r2.text


def test_my_access_render_route_returns_404_for_wrong_feature(app, capability_session):
    client, sid = asyncio.run(capability_session())
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": "AB12CD34", "feature": "ghost", "intent": "x",
         "params": {}, "title": "T"},
    ]}))
    r = client.get("/feature/auth/AB12CD34/render")
    assert r.status_code == 404


def test_my_access_render_route_returns_404_for_unknown_intent(app, capability_session):
    client, sid = asyncio.run(capability_session())
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": "AB12CD34", "feature": "auth", "intent": "nope",
         "params": {}, "title": "T"},
    ]}))
    r = client.get("/feature/auth/AB12CD34/render")
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/features/test_authorization_my_access.py -v
```
Expected: FAIL with 404 (RENDER_BY_INTENT is empty).

- [ ] **Step 3: Create `my_access.html`**

```html
{# src/iris/features/authorization/templates/my_access.html #}
<div class="iris-feature-page" id="{{ panel_id }}">
  <h2>My access</h2>

  <section class="iris-identity-card">
    <strong>{{ user.username }}</strong>
    {% if user.display_name and user.display_name != user.username %}
      <span class="iris-display-name">({{ user.display_name }})</span>
    {% endif %}
    <div class="iris-account-groups">Groups: {{ user.groups | join(", ") }}</div>
  </section>

  {% if reader_dbs %}
  <section>
    <h3>Databases you can read</h3>
    <ul class="iris-db-list">
      {% for db in reader_dbs %}
      <li>
        <span class="iris-db-name">{{ db }}</span>
        <button class="iris-action"
                data-on:click="@post('/api/tabs?feature=workbench&amp;intent=open&amp;params=' + encodeURIComponent(JSON.stringify({database: {{ db | tojson }}})))">
          open in workbench
        </button>
      </li>
      {% endfor %}
    </ul>
  </section>
  {% endif %}

  {% if writer_dbs %}
  <section>
    <h3>Databases you can write to</h3>
    <ul class="iris-db-list">
      {% for db in writer_dbs %}
      <li>
        <span class="iris-db-name">{{ db }}</span>
        <button class="iris-action"
                data-on:click="@post('/api/tabs?feature=workbench&amp;intent=open&amp;params=' + encodeURIComponent(JSON.stringify({database: {{ db | tojson }}})))">
          open in workbench
        </button>
      </li>
      {% endfor %}
    </ul>
  </section>
  {% endif %}

  {% if admin_dbs %}
  <section>
    <h3>Databases you administer</h3>
    <ul class="iris-db-list">
      {% for db in admin_dbs %}
      <li>
        <span class="iris-db-name">{{ db }}</span>
        <button class="iris-action"
                data-on:click="@post('/api/tabs?feature=auth&amp;intent=manage&amp;params=' + encodeURIComponent(JSON.stringify({database: {{ db | tojson }}})))">
          manage
        </button>
      </li>
      {% endfor %}
    </ul>
  </section>
  {% endif %}

  {% if can_create_database %}
  <section class="iris-actions-row">
    <button class="iris-action-primary"
            data-on:click="@post('/api/tabs?feature=auth&amp;intent=create_database&amp;params=%7B%7D')">
      + Create new database
    </button>
  </section>
  {% endif %}

  {% if is_admin %}
  <section class="iris-actions-row">
    <h3>Org administration</h3>
    <button class="iris-action-primary"
            data-on:click="@post('/api/tabs?feature=auth&amp;intent=admin_console&amp;params=%7B%7D')">
      Open admin console
    </button>
  </section>
  {% endif %}
</div>
```

- [ ] **Step 4: Create `service.py`** (currently just my_access listings; manage / admin add helpers later)

```python
# src/iris/features/authorization/service.py
"""Read-side helpers for the Authorization feature.

Pure functions that take a Capabilities (or other inputs) and return data
suitable for templates. No FastAPI imports here — keeps testing easy and
makes the layering explicit (routes → service → CH).
"""
from __future__ import annotations

from iris.auth.rights import Capabilities


def my_access_view(caps: Capabilities) -> dict[str, object]:
    """Build the template context for the my_access render."""
    return {
        "reader_dbs": sorted(caps.db_reader),
        "writer_dbs": sorted(caps.db_writer),
        "admin_dbs": sorted(caps.db_admin),
        "can_create_database": caps.can_create_database,
        "is_admin": caps.is_admin,
    }
```

- [ ] **Step 5: Implement the my_access render in `intents.py`**

```python
# src/iris/features/authorization/intents.py
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import Request, Response

if TYPE_CHECKING:
    from iris.auth.views import AuthSession
    from iris.shell.tabs import TabRecord

IntentHandler = Callable[[Request, "AuthSession", "TabRecord"], Awaitable[Response]]


async def render_my_access(
    request: Request,
    session: "AuthSession",
    rec: "TabRecord",
) -> Response:
    from iris.features.authorization.service import my_access_view
    from iris.shell.element_id import tab_panel_id

    templates = request.app.state.templates
    ctx = my_access_view(session.capabilities)
    return templates.TemplateResponse(
        request,
        "my_access.html",
        {
            "user": session.user,
            "panel_id": tab_panel_id(rec.id),
            **ctx,
        },
    )


RENDER_BY_INTENT: dict[str, IntentHandler] = {
    "my_access": render_my_access,
}
```

- [ ] **Step 6: Run to verify the tests pass**

```bash
uv run pytest tests/features/test_authorization_my_access.py -v
```
Expected: PASS (8 tests).

- [ ] **Step 7: Run all tests + gates**

```bash
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: zero failures, zero issues.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(features/authorization): my_access render — capability-adaptive

The my_access intent handler renders an identity card plus three optional
sections (Databases you can read / write to / administer) plus two
optional CTAs (Create new database, Open admin console). Each section is
omitted when its capability set is empty or the boolean predicate fails.

service.my_access_view is a pure function over Capabilities — easy to
unit-test, no FastAPI / template coupling.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.3: Smoke-test the end-to-end flow — open my_access tab, panel renders

**Files:**
- Test: `tests/features/test_authorization_smoke.py`

This is a defense-in-depth integration test: nav → tab open → tab render. Catches regressions if any of the layers are wired wrong.

- [ ] **Step 1: Write the test**

```python
# tests/features/test_authorization_smoke.py
"""End-to-end: nav has the entry, tab opens, panel renders."""
from __future__ import annotations

import asyncio


def test_home_includes_authorization_my_access_in_nav(authed_client):
    r = authed_client.get("/")
    assert r.status_code == 200
    assert "Authorization" in r.text
    assert "My access" in r.text


def test_open_my_access_tab_then_render(authed_client, parse_sse, app):
    # Bootstrap CSRF cookie
    home = authed_client.get("/")
    assert home.status_code == 200
    csrf = authed_client.cookies.get("iris_csrf")
    assert csrf

    # Open the tab
    open_r = authed_client.post(
        "/api/tabs",
        params={"feature": "auth", "intent": "my_access", "params": "{}"},
        headers={"Datastar-Request": "true", "X-CSRF-Token": csrf},
    )
    assert open_r.status_code == 200
    events = parse_sse(open_r.text)
    # Three events: button append, panel append, signals
    event_names = [e.event for e in events]
    assert event_names.count("datastar-patch-elements") == 2
    assert "datastar-patch-signals" in event_names

    # Extract tab_id from the signals event
    import json, re
    sig_event = next(e for e in events if e.event == "datastar-patch-signals")
    sig_payload = sig_event.data[len("signals "):] if sig_event.data.startswith("signals ") else sig_event.data
    sig = json.loads(sig_payload)
    assert "tabs" in sig and len(sig["tabs"]) == 1
    tab_id = next(iter(sig["tabs"]))
    assert sig["active"] == tab_id

    # Hit the render endpoint
    render_r = authed_client.get(f"/feature/auth/{tab_id}/render")
    assert render_r.status_code == 200
    assert "My access" in render_r.text


def test_my_access_intent_rejected_when_unknown_user_caps(app, capability_session):
    """my_access has required=lambda c: True, so any logged-in user passes.
    Verify the dispatch + render pipeline works for a minimum-cap session."""
    client, sid = asyncio.run(capability_session())  # all caps empty
    home = client.get("/")
    assert home.status_code == 200
    csrf = client.cookies.get("iris_csrf")
    open_r = client.post(
        "/api/tabs",
        params={"feature": "auth", "intent": "my_access", "params": "{}"},
        headers={"Datastar-Request": "true", "X-CSRF-Token": csrf},
    )
    assert open_r.status_code == 200
```

- [ ] **Step 2: Run to verify it passes**

```bash
uv run pytest tests/features/test_authorization_smoke.py -v
```
Expected: PASS (3 tests).

- [ ] **Step 3: Commit**

```bash
git add tests/features/test_authorization_smoke.py
git commit -m "$(cat <<'EOF'
test(features/authorization): end-to-end my_access tab open + render

Covers the full nav → POST /api/tabs → GET /feature/auth/{id}/render
flow. Catches regressions in any of: nav rendering, intent dispatcher,
SSE response shape, signal seeding, tab persistence, render dispatch.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

End of Phase 3. A logged-in user can open and view the my_access tab with capability-adaptive content. The shell, contribution registry, intent dispatcher, and per-feature render pipeline are now exercised end-to-end. Subsequent phases just add more intents.

---

## Phase 4 — `manage` intent (spec §7 step 4)

The per-database management page. Loaded when a user clicks `[manage →]` on the my_access page or "Manage \<db\>" under "Databases I admin" in the nav. Gated on `SessionDatabaseAdmin` (existing dep alias).

End state: an admin of database `marketing` can view and modify members (grant/revoke read/write/admin to users and groups), view + add + remove row policies, view audit, and delete the database (with confirmation).

### Task 4.1: `manage` intent registration + skeleton template + nav contribution

**Files:**
- Modify: `src/iris/features/authorization/install.py` (register intent + add nav entry "Databases I admin")
- Modify: `src/iris/features/authorization/intents.py` (add `render_manage`)
- Modify: `src/iris/features/authorization/service.py` (add helpers)
- Create: `src/iris/features/authorization/templates/manage.html`
- Create: `src/iris/features/authorization/templates/_members_section.html`
- Test: `tests/features/test_authorization_manage.py` (skeleton tests)

- [ ] **Step 1: Write the failing tests**

```python
# tests/features/test_authorization_manage.py
"""Manage intent: nav contribution + capability-aware sections."""
from __future__ import annotations

import asyncio


def _seed_manage_tab(app, sid: str, database: str = "marketing",
                     tab_id: str = "MG12CD34") -> None:
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": tab_id, "feature": "auth", "intent": "manage",
         "params": {"database": database}, "title": f"Manage {database}"},
    ]}))


def test_manage_intent_registered(app):
    spec = app.state.intent_dispatcher.resolve("auth", "manage")
    assert spec.title({"database": "marketing"}) == "Manage marketing"


def test_manage_required_predicate_checks_db_admin(app):
    from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
    spec = app.state.intent_dispatcher.resolve("auth", "manage")
    # The dispatcher's `required` checks logged-in caps but not the database
    # parameter — that's enforced at the route via SessionDatabaseAdmin.
    # The dispatcher predicate just gates the intent itself; for manage, any
    # user with at least one db_admin entry can attempt it.
    assert spec.required(EMPTY_CAPABILITIES) is False
    assert spec.required(Capabilities(
        is_admin=False, can_create_database=False,
        db_admin=frozenset({"marketing"}), db_writer=frozenset(),
        db_reader=frozenset(),
    )) is True
    assert spec.required(Capabilities(
        is_admin=True, can_create_database=False,
        db_admin=frozenset(), db_writer=frozenset(), db_reader=frozenset(),
    )) is True


def test_databases_i_admin_nav_entry_visible_when_db_admin_nonempty(app):
    from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
    contribs = app.state.contributions
    auth_group = next(g for g in contribs.nav.groups if g.label == "Authorization")
    db_admin_entry = next(
        (e for e in auth_group.entries if e.label == "Databases I admin"),
        None,
    )
    assert db_admin_entry is not None
    assert db_admin_entry.visible(EMPTY_CAPABILITIES) is False
    caps = Capabilities(
        is_admin=False, can_create_database=False,
        db_admin=frozenset({"x"}), db_writer=frozenset(), db_reader=frozenset(),
    )
    assert db_admin_entry.visible(caps) is True


def test_manage_render_renders_database_name(app, capability_session):
    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed_manage_tab(app, sid)
    r = client.get("/feature/auth/MG12CD34/render")
    assert r.status_code == 200
    assert "Manage marketing" in r.text


def test_manage_render_returns_403_when_not_db_admin(app, capability_session):
    client, sid = asyncio.run(capability_session())  # no caps
    _seed_manage_tab(app, sid)
    r = client.get("/feature/auth/MG12CD34/render")
    assert r.status_code == 403
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/features/test_authorization_manage.py -v
```
Expected: FAIL (intent unknown, nav entry missing, route 404).

- [ ] **Step 3: Register the manage intent and add the nav entry**

Edit `src/iris/features/authorization/install.py`:

```python
def _register_intents(dispatcher: IntentDispatcher) -> None:
    dispatcher.register(IntentSpec(
        feature="auth",
        intent="my_access",
        title=lambda _params: "My access",
        required=lambda _c: True,
    ))
    dispatcher.register(IntentSpec(
        feature="auth",
        intent="manage",
        title=lambda params: f"Manage {params.get('database', '')}",
        # Gate: caller has at least one admin db OR is global admin.
        # Per-database authorization is enforced at the per-route layer
        # (SessionDatabaseAdmin) — defense in depth.
        required=lambda c: c.is_admin or bool(c.db_admin),
    ))


def _register_nav(contribs: Contributions) -> None:
    contribs.nav.add(NavGroup(
        label="Authorization",
        entries=[
            NavEntry("My access", on_click=TabIntent("auth", "my_access")),
            NavEntry(
                "Databases I admin",
                visible=lambda c: bool(c.db_admin),
                badge=lambda c: str(len(c.db_admin)) if c.db_admin else None,
                children=lambda c: [
                    NavEntry(
                        db,
                        on_click=TabIntent("auth", "manage", {"database": db}),
                    )
                    for db in sorted(c.db_admin)
                ],
            ),
        ],
    ))
    contribs.nav.add(NavGroup(
        label="Org admin",
        visible=lambda c: c.is_admin,
        entries=[],
    ))
```

- [ ] **Step 4: Add the manage render function in `intents.py`**

```python
# Add to src/iris/features/authorization/intents.py

async def render_manage(
    request: Request,
    session: "AuthSession",
    rec: "TabRecord",
) -> Response:
    from fastapi import HTTPException
    from iris.auth.views import DatabaseAdminSession
    from iris.shell.element_id import tab_panel_id
    from iris.features.authorization.service import manage_view

    database = rec.params.get("database", "")
    if not database:
        raise HTTPException(status_code=400, detail="database param required")

    # Enforce per-database admin (route-layer authz, defense in depth).
    if not session.capabilities.has_admin(database):
        raise HTTPException(status_code=403, detail="not a database admin")

    # Promote the AuthSession to a DatabaseAdminSession to get the
    # bound CH client + per-DB methods. This mirrors what the dep alias
    # SessionDatabaseAdmin would inject in a path-parametrized route.
    db_session = DatabaseAdminSession(
        id=session.id, user=session.user,
        created_at=session.created_at, expires_at=session.expires_at,
        data=session.data, capabilities=session.capabilities,
        client=session.client, http_client=session.http_client,
        settings=session.settings, store=session.store,
        database=database,
    )

    templates = request.app.state.templates
    ctx = await manage_view(db_session)
    return templates.TemplateResponse(
        request,
        "manage.html",
        {
            "panel_id": tab_panel_id(rec.id),
            "tab_id": rec.id,
            "database": database,
            **ctx,
        },
    )


RENDER_BY_INTENT["manage"] = render_manage
```

- [ ] **Step 5: Add `manage_view` in `service.py`**

```python
# Add to src/iris/features/authorization/service.py
from typing import Any
from iris.auth.views import DatabaseAdminSession


async def manage_view(session: "DatabaseAdminSession") -> dict[str, Any]:
    """Build the manage-page context. Async because list_admin_members is async."""
    members = await session.list_admin_members()  # admin tier
    # In subsequent tasks we'll add reader/writer member listing helpers.
    return {
        "members": {
            "admin": members,
            "reader": [],   # filled in Task 4.2
            "writer": [],   # filled in Task 4.2
        },
        "row_policies": [],  # filled in Task 4.3
        "audit": [],         # filled in Task 4.4
    }
```

- [ ] **Step 6: Create skeleton templates**

```html
{# src/iris/features/authorization/templates/manage.html #}
<div class="iris-feature-page" id="{{ panel_id }}"
     data-signals='{"tabs": {{ {"" + tab_id: {}} | tojson }}}'>
  <header class="iris-manage-header">
    <button class="iris-back"
            data-on:click="@patch('/api/tabs/{{ tab_id }}?intent=my_access&amp;params=%7B%7D')">
      &larr;
    </button>
    <h2>Manage {{ database }}</h2>
  </header>

  {% include "_members_section.html" %}

  {# Phase 4 task 4.3 adds row policies section here #}
  {# Phase 4 task 4.4 adds audit section here #}
  {# Phase 4 task 4.5 adds danger zone here #}
</div>
```

```html
{# src/iris/features/authorization/templates/_members_section.html #}
<section id="{{ panel_id }}-members" class="iris-members-section">
  <h3>Members</h3>
  {# Phase 4 task 4.2 fills in tiers (Readers / Writers / Admins) with grant/revoke UI #}
  <div class="iris-members-tier">
    <h4>Admins</h4>
    <ul>
      {% for m in members.admin %}
      <li>{{ "group: " if m.kind == "role" else "" }}{{ m.name }}</li>
      {% endfor %}
    </ul>
  </div>
</section>
```

- [ ] **Step 7: Run to verify the tests pass**

```bash
uv run pytest tests/features/test_authorization_manage.py -v
```
Expected: PASS (5 tests). The render test against a real CH may need to be adjusted to handle the `DatabaseAdminSession.list_admin_members` call requiring CH; if `install_clickhouse=False` is in effect, the call will raise. In that case, mock the list to `[]` for the tests that don't exercise CH — or run only the gating tests in this task and add the CH-backed test in Task 4.2 once we have a real backing.

If the render test fails because of CH dependency: split into two tests — one that asserts the route enters the render function (mock the service), and one in `tests/clickhouse/integration/` that exercises the real CH path. For Phase 4 task 4.1, prefer the mocked route-test approach:

```python
def test_manage_render_renders_database_name(app, capability_session, monkeypatch):
    async def fake_view(session):
        return {"members": {"admin": [], "reader": [], "writer": []},
                "row_policies": [], "audit": []}
    monkeypatch.setattr(
        "iris.features.authorization.service.manage_view", fake_view,
    )
    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed_manage_tab(app, sid)
    r = client.get("/feature/auth/MG12CD34/render")
    assert r.status_code == 200
    assert "Manage marketing" in r.text
```

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(features/authorization): manage intent — skeleton + admin gate

Registers the 'manage' intent (gated on is_admin or db_admin nonempty
at intent layer; per-database admin enforced at route via
DatabaseAdminSession). Adds the 'Databases I admin' nav entry with
dynamic children listing each database the user admins. Renders a
skeleton manage page with header + Members section (admin tier only
at this task; reader/writer + grant/revoke land in 4.2; row policies
in 4.3; audit in 4.4; danger zone in 4.5).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4.2: Members section — list reader/writer + grant/revoke for users & groups

**Files:**
- Modify: `src/iris/features/authorization/service.py` (add `list_members`)
- Modify: `src/iris/features/authorization/templates/_members_section.html`
- Modify: `src/iris/features/authorization/routes.py` (add 12 routes: 6 grant + 6 revoke)
- Test: `tests/features/test_authorization_members.py`

The grant/revoke surface is 12 routes total: { reader, writer, admin } × { user, group } × { grant, revoke }. Implementing all twelve in one task because they share structure and are tedious to checkpoint individually.

- [ ] **Step 1: Write the failing tests** (gates and SSE shape — leave the underlying CH side-effects to integration tests)

```python
# tests/features/test_authorization_members.py
"""Members tier grants/revokes for users + groups."""
from __future__ import annotations

import asyncio


def _seed(app, sid: str, database: str = "marketing", tab_id: str = "MG12CD34"):
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": tab_id, "feature": "auth", "intent": "manage",
         "params": {"database": database}, "title": f"Manage {database}"},
    ]}))


def _csrf_headers(client):
    client.get("/")
    return {"Datastar-Request": "true",
            "X-CSRF-Token": client.cookies.get("iris_csrf") or ""}


def test_grant_reader_user_returns_403_when_not_db_admin(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed(app, sid)
    headers = _csrf_headers(client)
    r = client.post(
        "/feature/auth/MG12CD34/members/reader/user",
        params={"username": "bob"},
        headers=headers,
    )
    assert r.status_code == 403


def test_grant_reader_user_returns_400_on_bad_username(app, capability_session, monkeypatch):
    async def noop(self, username): pass
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.grant_reader", noop,
    )
    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    headers = _csrf_headers(client)
    # Empty username
    r = client.post(
        "/feature/auth/MG12CD34/members/reader/user",
        params={"username": ""},
        headers=headers,
    )
    assert r.status_code == 422  # FastAPI Query validation


def test_grant_reader_user_calls_db_session_method(app, capability_session, monkeypatch):
    calls = []
    async def fake_grant(self, username):
        calls.append(("grant_reader", username))
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.grant_reader", fake_grant,
    )
    # Members list helper also called — make it a no-op
    async def fake_list(self):
        return [{"kind": "user", "name": "bob"}]
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.list_admin_members", fake_list,
    )

    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    headers = _csrf_headers(client)
    r = client.post(
        "/feature/auth/MG12CD34/members/reader/user",
        params={"username": "bob"},
        headers=headers,
    )
    assert r.status_code == 200
    assert calls == [("grant_reader", "bob")]
    # SSE response should patch the members section
    assert "datastar-patch-elements" in r.text
    assert "MG12CD34-members" in r.text


def test_revoke_admin_group_calls_remove_admin_group(
    app, capability_session, monkeypatch
):
    calls = []
    async def fake_revoke(self, group):
        calls.append(("remove_admin_group", group))
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.remove_admin_group", fake_revoke,
    )
    async def fake_list(self):
        return []
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.list_admin_members", fake_list,
    )

    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    headers = _csrf_headers(client)
    r = client.delete(
        "/feature/auth/MG12CD34/members/admin/group",
        params={"group": "data-team"},
        headers=headers,
    )
    assert r.status_code == 200
    assert calls == [("remove_admin_group", "data-team")]


def test_grant_routes_csrf_required(app, capability_session):
    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    client.get("/")  # cookie set
    # Omit X-CSRF-Token
    r = client.post(
        "/feature/auth/MG12CD34/members/reader/user",
        params={"username": "bob"},
        headers={"Datastar-Request": "true"},
    )
    assert r.status_code == 400
```

- [ ] **Step 2: Run to verify they fail**

```bash
uv run pytest tests/features/test_authorization_members.py -v
```
Expected: FAIL — routes don't exist.

- [ ] **Step 3: Add `list_members` to service.py**

Replace `manage_view` with the fuller version:

```python
async def manage_view(session: "DatabaseAdminSession") -> dict[str, Any]:
    members = await list_members(session)
    return {
        "members": members,
        "row_policies": [],   # task 4.3
        "audit": [],          # task 4.4
    }


async def list_members(session: "DatabaseAdminSession") -> dict[str, list[dict]]:
    """Return {tier: [{kind, name}]} across reader/writer/admin tiers.

    Admins are read via the existing list_admin_members method on
    DatabaseAdminSession. Reader/writer tiers don't yet have a typed list
    method on the session — query system.role_grants for the tier role
    name (tier_role_name(database, tier)) directly. That helper lives in
    iris.clickhouse.grants.
    """
    from iris.clickhouse.grants import (
        TIER_DBADMIN, TIER_DBREADER, TIER_DBWRITER, tier_role_name,
    )
    import asyncio

    client = session._ch()[0]
    db = session.database
    members: dict[str, list[dict]] = {"admin": [], "reader": [], "writer": []}

    # Admin tier uses the existing typed method
    members["admin"] = await session.list_admin_members()

    # Reader / Writer: query system.role_grants for grantees of the tier role
    for tier_const, tier_key in (
        (TIER_DBREADER, "reader"),
        (TIER_DBWRITER, "writer"),
    ):
        role = tier_role_name(db, tier_const)
        def _q(role=role):
            rows = client.query(
                """
                SELECT user_name, role_name FROM system.role_grants
                WHERE granted_role_name = {r:String}
                """,
                {"r": role},
            )
            out: list[dict] = []
            for row in rows.named_results():
                u = row.get("user_name")
                r2 = row.get("role_name")
                if u:
                    out.append({"kind": "user", "name": u})
                elif r2:
                    out.append({"kind": "role", "name": r2})
            return out
        members[tier_key] = await asyncio.to_thread(_q)
    return members
```

- [ ] **Step 4: Replace `_members_section.html` with the full UI**

```html
{# src/iris/features/authorization/templates/_members_section.html #}
<section id="{{ panel_id }}-members" class="iris-members-section">
  <h3>Members</h3>
  {% for tier_label, tier_key in [("Readers", "reader"), ("Writers", "writer"), ("Admins", "admin")] %}
  <div class="iris-members-tier">
    <h4>{{ tier_label }}</h4>
    <form data-on:submit="@post('/feature/auth/{{ tab_id }}/members/{{ tier_key }}/user?username=' + encodeURIComponent($tabs.{{ tab_id }}.{{ tier_key }}_user_input || ''))"
          class="iris-grant-form">
      <input type="text" placeholder="Add user…"
             data-bind="tabs.{{ tab_id }}.{{ tier_key }}_user_input">
      <button type="submit">+ add user</button>
    </form>
    <form data-on:submit="@post('/feature/auth/{{ tab_id }}/members/{{ tier_key }}/group?group=' + encodeURIComponent($tabs.{{ tab_id }}.{{ tier_key }}_group_input || ''))"
          class="iris-grant-form">
      <input type="text" placeholder="Add group…"
             data-bind="tabs.{{ tab_id }}.{{ tier_key }}_group_input">
      <button type="submit">+ add group</button>
    </form>
    <ul>
      {% for m in members[tier_key] %}
      <li>
        {% if m.kind == "role" %}group: {% endif %}{{ m.name }}
        <button class="iris-revoke"
                data-on:click="@delete('/feature/auth/{{ tab_id }}/members/{{ tier_key }}/{{ 'group' if m.kind == 'role' else 'user' }}?{{ 'group' if m.kind == 'role' else 'username' }}=' + encodeURIComponent({{ m.name | tojson }}))">
          revoke
        </button>
      </li>
      {% endfor %}
    </ul>
  </div>
  {% endfor %}
</section>
```

- [ ] **Step 5: Add the 12 grant/revoke routes in `routes.py`**

```python
# Append to src/iris/features/authorization/routes.py

from typing import Annotated, Callable, Awaitable

from fastapi import Depends, Query
from datastar_py.fastapi import DatastarResponse, ServerSentEventGenerator as SSE
from iris.auth.csrf import verify_csrf_header
from iris.auth.deps import Session
from iris.auth.views import DatabaseAdminSession


def _promote_to_db_admin(session, database: str) -> DatabaseAdminSession:
    from fastapi import HTTPException
    if not session.capabilities.has_admin(database):
        raise HTTPException(status_code=403, detail="not a database admin")
    return DatabaseAdminSession(
        id=session.id, user=session.user,
        created_at=session.created_at, expires_at=session.expires_at,
        data=session.data, capabilities=session.capabilities,
        client=session.client, http_client=session.http_client,
        settings=session.settings, store=session.store,
        database=database,
    )


async def _re_render_members(request, db_session, panel_id: str, tab_id: str):
    from iris.features.authorization.service import list_members
    members = await list_members(db_session)
    templates = request.app.state.templates
    html = templates.get_template("_members_section.html").render(
        panel_id=panel_id, tab_id=tab_id, members=members,
    )
    return DatastarResponse(
        SSE.patch_elements(html, selector=f"#{panel_id}-members", mode="outer")
    )


async def _members_route_common(
    request, session, tab_id: str,
) -> tuple[DatabaseAdminSession, str]:
    """Resolve tab → database → DatabaseAdminSession + panel_id."""
    from fastapi import HTTPException
    from iris.shell.tabs import find_tab
    from iris.shell.element_id import tab_panel_id

    rec = find_tab(session.data, tab_id)
    if rec is None or rec.feature != "auth" or rec.intent != "manage":
        raise HTTPException(status_code=404, detail="tab not found")
    database = rec.params.get("database", "")
    if not database:
        raise HTTPException(status_code=400, detail="database missing")
    db_session = _promote_to_db_admin(session, database)
    return db_session, tab_panel_id(rec.id)


# 12 routes: {reader, writer, admin} × {user, group} × {POST grant, DELETE revoke}

@router.post("/{tab_id}/members/reader/user")
async def grant_reader_user(
    request: Request, session: Session, tab_id: str,
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
):
    db, panel_id = await _members_route_common(request, session, tab_id)
    await db.grant_reader(username)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.delete("/{tab_id}/members/reader/user")
async def revoke_reader_user(
    request: Request, session: Session, tab_id: str,
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
):
    db, panel_id = await _members_route_common(request, session, tab_id)
    await db.revoke_reader(username)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.post("/{tab_id}/members/reader/group")
async def grant_reader_group(
    request: Request, session: Session, tab_id: str,
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
):
    db, panel_id = await _members_route_common(request, session, tab_id)
    await db.grant_reader_to_group(group)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.delete("/{tab_id}/members/reader/group")
async def revoke_reader_group(
    request: Request, session: Session, tab_id: str,
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
):
    db, panel_id = await _members_route_common(request, session, tab_id)
    await db.revoke_reader_from_group(group)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.post("/{tab_id}/members/writer/user")
async def grant_writer_user(
    request: Request, session: Session, tab_id: str,
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
):
    db, panel_id = await _members_route_common(request, session, tab_id)
    await db.grant_writer(username)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.delete("/{tab_id}/members/writer/user")
async def revoke_writer_user(
    request: Request, session: Session, tab_id: str,
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
):
    db, panel_id = await _members_route_common(request, session, tab_id)
    await db.revoke_writer(username)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.post("/{tab_id}/members/writer/group")
async def grant_writer_group(
    request: Request, session: Session, tab_id: str,
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
):
    db, panel_id = await _members_route_common(request, session, tab_id)
    await db.grant_writer_to_group(group)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.delete("/{tab_id}/members/writer/group")
async def revoke_writer_group(
    request: Request, session: Session, tab_id: str,
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
):
    db, panel_id = await _members_route_common(request, session, tab_id)
    await db.revoke_writer_from_group(group)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.post("/{tab_id}/members/admin/user")
async def grant_admin_user(
    request: Request, session: Session, tab_id: str,
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
):
    db, panel_id = await _members_route_common(request, session, tab_id)
    await db.add_admin_user(username)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.delete("/{tab_id}/members/admin/user")
async def revoke_admin_user(
    request: Request, session: Session, tab_id: str,
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
):
    db, panel_id = await _members_route_common(request, session, tab_id)
    await db.remove_admin_user(username)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.post("/{tab_id}/members/admin/group")
async def grant_admin_group(
    request: Request, session: Session, tab_id: str,
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
):
    db, panel_id = await _members_route_common(request, session, tab_id)
    await db.add_admin_group(group)
    return await _re_render_members(request, db, panel_id, tab_id)


@router.delete("/{tab_id}/members/admin/group")
async def revoke_admin_group(
    request: Request, session: Session, tab_id: str,
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
):
    db, panel_id = await _members_route_common(request, session, tab_id)
    await db.remove_admin_group(group)
    return await _re_render_members(request, db, panel_id, tab_id)
```

- [ ] **Step 6: Run to verify the tests pass**

```bash
uv run pytest tests/features/test_authorization_members.py -v
```
Expected: PASS (5 tests).

- [ ] **Step 7: Run gates**

```bash
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: zero issues.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(features/authorization): manage members — 12 grant/revoke routes

Members section UI: per-tier (reader/writer/admin) form to grant a
user/group + revoke buttons next to each existing member. 12 routes
total ({reader,writer,admin} × {user,group} × {grant POST, revoke
DELETE}), each gated by per-database admin check via DatabaseAdminSession
promotion. After every grant/revoke, the route re-renders the members
section as an SSE patch_elements with mode=outer on the section id.

list_members service helper queries system.role_grants for reader/writer
tier roles and falls back to the existing list_admin_members for the
admin tier. Each route runs CSRF verification (verify_csrf_header).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4.3: Row policies section — list, add, remove

**Files:**
- Modify: `src/iris/features/authorization/service.py` (add `list_row_policies_view`)
- Create: `src/iris/features/authorization/templates/_row_policies.html`
- Modify: `src/iris/features/authorization/templates/manage.html` (include partial)
- Modify: `src/iris/features/authorization/routes.py` (add 2 routes)
- Test: `tests/features/test_authorization_row_policies.py`

The DatabaseAdminSession only provides a *list* helper (`list_row_policies`) at present. Add/remove for row policies live on `AdminSession` (`add_row_policy`, `revoke_row_policy`). Per-database admins should be able to manage policies on their own database. Allow this by performing the operation through a fresh `AdminSession`-style call gated server-side on `has_admin(database)` — but only via the existing typed methods. The cleanest path: extend `DatabaseAdminSession` to expose `add_row_policy` and `revoke_row_policy` scoped to `self.database`, delegating to `iris.clickhouse.policies` directly (same pattern as `list_row_policies`).

That extension is scoped to `iris.auth.views.DatabaseAdminSession` and minimal:

- [ ] **Step 1: Extend `DatabaseAdminSession` with `add_row_policy` / `revoke_row_policy`**

In `src/iris/auth/views.py`, inside `class DatabaseAdminSession(DatabaseSession):`, append:

```python
    async def add_row_policy(
        self, *, table: str, column: str, role: str, value: str
    ) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            policies.add_row_policy, client,
            database=self.database, table=table, column=column,
            role=role, value=value,
        )

    async def revoke_row_policy(
        self, *, table: str, role: str, value: str
    ) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            policies.revoke_row_policy, client,
            database=self.database, table=table, role=role, value=value,
        )
```

(`policies` is already imported at the top of the file.)

Add a small unit test in `tests/auth/test_session_dep.py` or a new `tests/auth/test_database_admin_row_policies.py` to verify the methods exist and call through:

```python
# tests/auth/test_database_admin_row_policies.py
from __future__ import annotations

import asyncio


def test_add_row_policy_calls_policies_helper(monkeypatch):
    captured = {}
    def fake_add(client, *, database, table, column, role, value):
        captured["args"] = (database, table, column, role, value)
    monkeypatch.setattr(
        "iris.auth.views.policies.add_row_policy", fake_add,
    )
    from unittest.mock import MagicMock
    from iris.auth.views import DatabaseAdminSession
    from iris.auth.identity import User
    from iris.auth.rights import EMPTY_CAPABILITIES
    from datetime import datetime, UTC

    s = DatabaseAdminSession(
        id="x", user=User("s", "u", "U", ()),
        created_at=datetime.now(UTC), expires_at=datetime.now(UTC),
        data={}, capabilities=EMPTY_CAPABILITIES,
        client=MagicMock(), http_client=MagicMock(), settings=MagicMock(),
        store=MagicMock(), database="marketing",
    )
    asyncio.run(s.add_row_policy(table="events", column="user_id",
                                  role="r1", value="alice"))
    assert captured["args"] == ("marketing", "events", "user_id", "r1", "alice")
```

- [ ] **Step 2: Write the failing route tests**

```python
# tests/features/test_authorization_row_policies.py
from __future__ import annotations

import asyncio


def _seed(app, sid: str, database="marketing", tab_id="MG12CD34"):
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": tab_id, "feature": "auth", "intent": "manage",
         "params": {"database": database}, "title": f"Manage {database}"},
    ]}))


def _csrf(client):
    client.get("/")
    return {"Datastar-Request": "true",
            "X-CSRF-Token": client.cookies.get("iris_csrf") or ""}


def test_add_policy_403_when_not_db_admin(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed(app, sid)
    r = client.post(
        "/feature/auth/MG12CD34/policies",
        params={"table": "events", "column": "user_id",
                "role": "r1", "value": "alice"},
        headers=_csrf(client),
    )
    assert r.status_code == 403


def test_add_policy_calls_db_session_method(app, capability_session, monkeypatch):
    calls = []
    async def fake_add(self, *, table, column, role, value):
        calls.append(("add", table, column, role, value))
    async def fake_list(self):
        return []
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.add_row_policy", fake_add,
    )
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.list_row_policies", fake_list,
    )
    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    r = client.post(
        "/feature/auth/MG12CD34/policies",
        params={"table": "events", "column": "user_id",
                "role": "r1", "value": "alice"},
        headers=_csrf(client),
    )
    assert r.status_code == 200
    assert calls == [("add", "events", "user_id", "r1", "alice")]
    assert "MG12CD34-policies" in r.text


def test_revoke_policy_calls_db_session_method(app, capability_session, monkeypatch):
    calls = []
    async def fake_rev(self, *, table, role, value):
        calls.append(("rev", table, role, value))
    async def fake_list(self):
        return []
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.revoke_row_policy", fake_rev,
    )
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.list_row_policies", fake_list,
    )
    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    r = client.delete(
        "/feature/auth/MG12CD34/policies",
        params={"table": "events", "role": "r1", "value": "alice"},
        headers=_csrf(client),
    )
    assert r.status_code == 200
    assert calls == [("rev", "events", "r1", "alice")]
```

- [ ] **Step 3: Add `list_row_policies_view` and update `manage_view`**

In `service.py`, replace `manage_view` body with:

```python
async def manage_view(session: "DatabaseAdminSession") -> dict[str, Any]:
    members = await list_members(session)
    row_policies = await session.list_row_policies()
    return {
        "members": members,
        "row_policies": row_policies,
        "audit": [],          # task 4.4
    }
```

- [ ] **Step 4: Create `_row_policies.html`**

```html
{# src/iris/features/authorization/templates/_row_policies.html #}
<section id="{{ panel_id }}-policies" class="iris-policies-section">
  <h3>Row policies</h3>
  <ul>
    {% for p in row_policies %}
    <li>
      {{ p.table | default("?") }} ON role {{ p.short_name | default(p.name) | default("?") }}: {{ p.select_filter | default("?") }}
      <button data-on:click="@delete('/feature/auth/{{ tab_id }}/policies?table=' + encodeURIComponent({{ (p.table or "") | tojson }}) + '&amp;role=' + encodeURIComponent({{ (p.short_name or p.name or "") | tojson }}) + '&amp;value=' + encodeURIComponent({{ (p.select_filter or "") | tojson }}))">
        &times;
      </button>
    </li>
    {% endfor %}
  </ul>
  <form data-on:submit="@post('/feature/auth/{{ tab_id }}/policies?table=' + encodeURIComponent($tabs.{{ tab_id }}.policy_table || '') + '&amp;column=' + encodeURIComponent($tabs.{{ tab_id }}.policy_column || '') + '&amp;role=' + encodeURIComponent($tabs.{{ tab_id }}.policy_role || '') + '&amp;value=' + encodeURIComponent($tabs.{{ tab_id }}.policy_value || ''))"
        class="iris-add-policy">
    <input placeholder="table" data-bind="tabs.{{ tab_id }}.policy_table">
    <input placeholder="column" data-bind="tabs.{{ tab_id }}.policy_column">
    <input placeholder="role" data-bind="tabs.{{ tab_id }}.policy_role">
    <input placeholder="value" data-bind="tabs.{{ tab_id }}.policy_value">
    <button type="submit">+ add row policy</button>
  </form>
</section>
```

(The exact dict shape returned by `list_row_policies` from `system.row_policies` is upstream-defined; the template uses defensive `| default` to render whichever fields are present. Operators can iterate on the shape later.)

- [ ] **Step 5: Include the partial in `manage.html`**

Edit `manage.html` to insert `{% include "_row_policies.html" %}` after the members-section include.

- [ ] **Step 6: Add the two policy routes in `routes.py`**

```python
@router.post("/{tab_id}/policies")
async def add_policy(
    request: Request, session: Session, tab_id: str,
    table: Annotated[str, Query(min_length=1, max_length=64)],
    column: Annotated[str, Query(min_length=1, max_length=64)],
    role: Annotated[str, Query(min_length=1, max_length=64)],
    value: Annotated[str, Query(min_length=0, max_length=4096)],
    _: None = Depends(verify_csrf_header),
):
    db, panel_id = await _members_route_common(request, session, tab_id)
    await db.add_row_policy(table=table, column=column, role=role, value=value)
    return await _re_render_policies(request, db, panel_id, tab_id)


@router.delete("/{tab_id}/policies")
async def revoke_policy(
    request: Request, session: Session, tab_id: str,
    table: Annotated[str, Query(min_length=1, max_length=64)],
    role: Annotated[str, Query(min_length=1, max_length=64)],
    value: Annotated[str, Query(min_length=0, max_length=4096)],
    _: None = Depends(verify_csrf_header),
):
    db, panel_id = await _members_route_common(request, session, tab_id)
    await db.revoke_row_policy(table=table, role=role, value=value)
    return await _re_render_policies(request, db, panel_id, tab_id)


async def _re_render_policies(request, db_session, panel_id: str, tab_id: str):
    row_policies = await db_session.list_row_policies()
    templates = request.app.state.templates
    html = templates.get_template("_row_policies.html").render(
        panel_id=panel_id, tab_id=tab_id, row_policies=row_policies,
    )
    return DatastarResponse(
        SSE.patch_elements(html, selector=f"#{panel_id}-policies", mode="outer")
    )
```

- [ ] **Step 7: Run tests + gates**

```bash
uv run pytest tests/auth/test_database_admin_row_policies.py tests/features/test_authorization_row_policies.py -v
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: zero failures, zero issues.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(features/authorization): manage row policies — list, add, remove

Adds add_row_policy / revoke_row_policy methods to DatabaseAdminSession
(both delegate to iris.clickhouse.policies, scoped to self.database).
Adds POST /feature/auth/{tab_id}/policies and DELETE …/policies routes,
each gated by per-database admin check; both re-render the policies
section via SSE on success.

list_row_policies (already on DatabaseAdminSession) feeds the section.
The template is defensive about column shape from system.row_policies.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4.4: Audit section — recent grants for the database

**Files:**
- Modify: `src/iris/features/authorization/service.py` (add `list_audit_view`)
- Create: `src/iris/features/authorization/templates/_audit.html`
- Modify: `src/iris/features/authorization/templates/manage.html`
- Test: `tests/features/test_authorization_audit.py`

The audit section is read-only — no routes beyond what `list_grants` (already on `DatabaseAdminSession`) provides.

- [ ] **Step 1: Write the failing test**

```python
# tests/features/test_authorization_audit.py
from __future__ import annotations

import asyncio


def test_audit_section_renders_grants_list(app, capability_session, monkeypatch):
    async def fake_list_members(self): return {"admin": [], "reader": [], "writer": []}
    async def fake_list_policies(self): return []
    async def fake_list_grants(self):
        return [
            {"user_name": "alice", "role_name": None, "access_type": "SELECT",
             "database": "marketing", "table": None, "column": None,
             "is_partial_revoke": 0, "grant_option": 0},
        ]
    monkeypatch.setattr("iris.auth.views.DatabaseAdminSession.list_admin_members",
                        lambda self: fake_list_members(self))
    monkeypatch.setattr("iris.features.authorization.service.list_members",
                        lambda s: fake_list_members(s))
    monkeypatch.setattr("iris.auth.views.DatabaseAdminSession.list_row_policies",
                        lambda self: fake_list_policies(self))
    monkeypatch.setattr("iris.auth.views.DatabaseAdminSession.list_grants",
                        lambda self: fake_list_grants(self))

    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": "AU12CD34", "feature": "auth", "intent": "manage",
         "params": {"database": "marketing"}, "title": "Manage marketing"},
    ]}))
    r = client.get("/feature/auth/AU12CD34/render")
    assert r.status_code == 200
    assert "Audit" in r.text
    assert "alice" in r.text
    assert "SELECT" in r.text
```

- [ ] **Step 2: Update `service.manage_view` to fetch grants**

```python
async def manage_view(session: "DatabaseAdminSession") -> dict[str, Any]:
    members = await list_members(session)
    row_policies = await session.list_row_policies()
    audit = await session.list_grants()
    return {
        "members": members,
        "row_policies": row_policies,
        "audit": audit,
    }
```

- [ ] **Step 3: Create `_audit.html`**

```html
{# src/iris/features/authorization/templates/_audit.html #}
<section id="{{ panel_id }}-audit" class="iris-audit-section">
  <h3>Audit</h3>
  <table>
    <thead>
      <tr>
        <th>Grantee</th><th>Access</th><th>Database</th>
        <th>Table</th><th>Column</th>
      </tr>
    </thead>
    <tbody>
      {% for row in audit %}
      <tr>
        <td>{{ row.user_name or ("role: " + row.role_name) if row.role_name else row.user_name }}</td>
        <td>{{ row.access_type }}</td>
        <td>{{ row.database or "—" }}</td>
        <td>{{ row.table or "—" }}</td>
        <td>{{ row.column or "—" }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</section>
```

- [ ] **Step 4: Include in `manage.html`**

Add `{% include "_audit.html" %}` after the policies-section include.

- [ ] **Step 5: Run + gates + commit**

```bash
uv run pytest tests/features/test_authorization_audit.py -v
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
git add -A
git commit -m "$(cat <<'EOF'
feat(features/authorization): manage audit section — list grants

Read-only table of system.grants rows scoped to the current database via
DatabaseAdminSession.list_grants (already exists). Renders below the
row-policies section. No routes added — the data is fetched as part of
manage_view on each render.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4.5: Danger zone — delete database with two-step confirmation

**Files:**
- Create: `src/iris/features/authorization/templates/_danger.html`
- Modify: `src/iris/features/authorization/templates/manage.html`
- Modify: `src/iris/features/authorization/routes.py` (add delete-database route)
- Test: `tests/features/test_authorization_danger.py`

Two-step UI: click "Delete database" → form expands to ask the user to type the database name to confirm → submit calls `delete_database`. Server-side, only the second submission triggers the destructive call. After delete succeeds, the route closes the tab (removes from `session.data['tabs']`, returns SSE removing button + panel).

- [ ] **Step 1: Write the failing test**

```python
# tests/features/test_authorization_danger.py
from __future__ import annotations

import asyncio


def _seed(app, sid: str, database="marketing", tab_id="DG12CD34"):
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": tab_id, "feature": "auth", "intent": "manage",
         "params": {"database": database}, "title": f"Manage {database}"},
    ]}))


def _csrf(client):
    client.get("/")
    return {"Datastar-Request": "true",
            "X-CSRF-Token": client.cookies.get("iris_csrf") or ""}


def test_delete_database_403_when_not_db_admin(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed(app, sid)
    r = client.delete(
        "/feature/auth/DG12CD34/database",
        params={"confirm": "marketing"},
        headers=_csrf(client),
    )
    assert r.status_code == 403


def test_delete_database_400_when_confirm_mismatches(app, capability_session, monkeypatch):
    async def must_not_call(self):
        raise AssertionError("delete_database should not have been called")
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.delete_database", must_not_call,
    )
    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    r = client.delete(
        "/feature/auth/DG12CD34/database",
        params={"confirm": "wrong-name"},
        headers=_csrf(client),
    )
    assert r.status_code == 400
    assert "confirmation" in r.text.lower() or "match" in r.text.lower()


def test_delete_database_calls_method_and_closes_tab(app, capability_session, monkeypatch):
    called = []
    async def fake_delete(self):
        called.append("delete")
    monkeypatch.setattr(
        "iris.auth.views.DatabaseAdminSession.delete_database", fake_delete,
    )
    client, sid = asyncio.run(capability_session(db_admin={"marketing"}))
    _seed(app, sid)
    r = client.delete(
        "/feature/auth/DG12CD34/database",
        params={"confirm": "marketing"},
        headers=_csrf(client),
    )
    assert r.status_code == 200
    assert called == ["delete"]
    # Tab is removed from session.data
    refreshed = asyncio.run(app.state.auth_session_store.get_and_refresh(sid))
    assert refreshed is not None
    assert refreshed.data.get("tabs", []) == []
    # SSE removes button + panel
    assert "tab-button-DG12CD34" in r.text
    assert "tab-content-DG12CD34" in r.text
```

- [ ] **Step 2: Create `_danger.html`**

```html
{# src/iris/features/authorization/templates/_danger.html #}
<section id="{{ panel_id }}-danger" class="iris-danger-section">
  <h3>Danger</h3>
  <details>
    <summary class="iris-danger-toggle">delete database</summary>
    <form data-on:submit="@delete('/feature/auth/{{ tab_id }}/database?confirm=' + encodeURIComponent($tabs.{{ tab_id }}.delete_confirm || ''))">
      <p>Type <code>{{ database }}</code> to confirm. This action is irreversible.</p>
      <input placeholder="confirm db name"
             data-bind="tabs.{{ tab_id }}.delete_confirm">
      <button type="submit" class="iris-danger-submit">delete database</button>
    </form>
  </details>
</section>
```

- [ ] **Step 3: Add `{% include "_danger.html" %}` to `manage.html`** (after audit)

- [ ] **Step 4: Add the delete-database route in `routes.py`**

```python
@router.delete("/{tab_id}/database")
async def delete_database(
    request: Request, session: Session, tab_id: str,
    confirm: Annotated[str, Query(min_length=0, max_length=255)],
    _: None = Depends(verify_csrf_header),
):
    from fastapi import HTTPException
    from iris.shell.tabs import find_tab, remove_tab

    rec = find_tab(session.data, tab_id)
    if rec is None or rec.feature != "auth" or rec.intent != "manage":
        raise HTTPException(status_code=404, detail="tab not found")
    database = rec.params.get("database", "")
    if not database:
        raise HTTPException(status_code=400, detail="database missing")
    if confirm != database:
        raise HTTPException(status_code=400,
                            detail="confirmation does not match the database name")

    db_session = _promote_to_db_admin(session, database)
    await db_session.delete_database()

    # Close the tab — the page no longer exists.
    remove_tab(session.data, tab_id)
    await session.persist_data()
    return DatastarResponse([
        SSE.patch_elements(selector=f"#tab-button-{tab_id}", mode="remove"),
        SSE.patch_elements(selector=f"#tab-content-{tab_id}", mode="remove"),
    ])
```

- [ ] **Step 5: Run tests + gates + commit**

```bash
uv run pytest tests/features/test_authorization_danger.py -v
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
git add -A
git commit -m "$(cat <<'EOF'
feat(features/authorization): manage danger zone — delete database

Two-step UI behind a <details> disclosure: typing the database name in
a confirm field is required before the DELETE submits. Server requires
exact match between the confirm query param and the tab's database; on
mismatch returns 400. On success calls DatabaseAdminSession.delete_database
(which sweeps grants and drops the DB), removes the tab from session.data,
and emits SSE patches removing the tab button + panel.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

End of Phase 4. The `manage` intent is fully functional: members CRUD across three tiers and two grantee kinds, row policies CRUD, audit view, and a confirmation-protected delete database. All routes gated by `DatabaseAdminSession`-style admin check; CSRF on every mutation.

---

## Phase 5 — `create_database` intent (spec §7 step 5)

A single-screen form: database name input + submit. On success, the panel re-targets to `manage` for the new database (the creator becomes its admin via the existing `DatabaseCreatorSession.create_database`). On validation failure, the form re-renders with an inline error.

### Task 5.1: Register intent + nav entry + render form + submit handler

**Files:**
- Modify: `src/iris/features/authorization/install.py` (register intent + nav entry)
- Modify: `src/iris/features/authorization/intents.py` (add `render_create_database`)
- Create: `src/iris/features/authorization/templates/create_database.html`
- Modify: `src/iris/features/authorization/routes.py` (add submit route)
- Test: `tests/features/test_authorization_create_database.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/features/test_authorization_create_database.py
from __future__ import annotations

import asyncio


def _seed(app, sid: str, tab_id="CR12CD34"):
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": tab_id, "feature": "auth", "intent": "create_database",
         "params": {}, "title": "Create database"},
    ]}))


def _csrf(client):
    client.get("/")
    return {"Datastar-Request": "true",
            "X-CSRF-Token": client.cookies.get("iris_csrf") or ""}


def test_create_database_intent_registered(app):
    spec = app.state.intent_dispatcher.resolve("auth", "create_database")
    assert spec.title({}) == "Create database"


def test_create_database_required_predicate(app):
    from iris.auth.rights import Capabilities, EMPTY_CAPABILITIES
    spec = app.state.intent_dispatcher.resolve("auth", "create_database")
    assert spec.required(EMPTY_CAPABILITIES) is False
    assert spec.required(Capabilities(
        is_admin=False, can_create_database=True,
        db_admin=frozenset(), db_writer=frozenset(), db_reader=frozenset(),
    )) is True
    assert spec.required(Capabilities(
        is_admin=True, can_create_database=False,
        db_admin=frozenset(), db_writer=frozenset(), db_reader=frozenset(),
    )) is True


def test_create_database_nav_entry_visible_when_can_create(app):
    from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
    contribs = app.state.contributions
    auth_group = next(g for g in contribs.nav.groups if g.label == "Authorization")
    create_entry = next(
        (e for e in auth_group.entries if e.label == "Create database"),
        None,
    )
    assert create_entry is not None
    assert create_entry.visible(EMPTY_CAPABILITIES) is False
    caps = Capabilities(
        is_admin=False, can_create_database=True,
        db_admin=frozenset(), db_writer=frozenset(), db_reader=frozenset(),
    )
    assert create_entry.visible(caps) is True


def test_render_create_database_shows_form(app, capability_session):
    client, sid = asyncio.run(capability_session(can_create_database=True))
    _seed(app, sid)
    r = client.get("/feature/auth/CR12CD34/render")
    assert r.status_code == 200
    assert "Create database" in r.text
    assert "data-bind=\"tabs.CR12CD34.new_db_name\"" in r.text


def test_submit_create_database_403_when_not_creator(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed(app, sid)
    r = client.post(
        "/feature/auth/CR12CD34/submit",
        params={"name": "marketing"},
        headers=_csrf(client),
    )
    assert r.status_code == 403


def test_submit_create_database_calls_method_and_retargets_tab(
    app, capability_session, monkeypatch
):
    calls = []
    async def fake_create(self, name):
        calls.append(("create", name))
    monkeypatch.setattr(
        "iris.auth.views.DatabaseCreatorSession.create_database", fake_create,
    )
    client, sid = asyncio.run(capability_session(can_create_database=True))
    _seed(app, sid)
    r = client.post(
        "/feature/auth/CR12CD34/submit",
        params={"name": "shiny_new_db"},
        headers=_csrf(client),
    )
    assert r.status_code == 200
    assert calls == [("create", "shiny_new_db")]
    # Tab re-targeted in session.data
    refreshed = asyncio.run(app.state.auth_session_store.get_and_refresh(sid))
    tabs = refreshed.data["tabs"]
    assert tabs[0]["intent"] == "manage"
    assert tabs[0]["params"]["database"] == "shiny_new_db"
    assert tabs[0]["title"] == "Manage shiny_new_db"
    # SSE re-renders the panel + button title
    assert "tab-button-CR12CD34" in r.text
    assert "tab-content-CR12CD34" in r.text


def test_submit_create_database_400_on_invalid_name(
    app, capability_session, monkeypatch
):
    async def fake_create(self, name):
        from iris.clickhouse.identifiers import IdentifierError
        raise IdentifierError("invalid")
    monkeypatch.setattr(
        "iris.auth.views.DatabaseCreatorSession.create_database", fake_create,
    )
    client, sid = asyncio.run(capability_session(can_create_database=True))
    _seed(app, sid)
    r = client.post(
        "/feature/auth/CR12CD34/submit",
        params={"name": "bad-name"},  # actual validation done by the helper
        headers=_csrf(client),
    )
    # 200 + an inline error fragment (form re-renders with the error)
    assert r.status_code == 200
    assert "invalid" in r.text.lower()
```

- [ ] **Step 2: Verify the IdentifierError name** matches the actual class in `iris.clickhouse.identifiers`. If it's `ValueError` instead, adjust the test and the route accordingly. Run:

```bash
grep -E "^class.*Error" src/iris/clickhouse/identifiers.py
```

If it raises `ValueError`, change the monkeypatch and the route's `except`.

- [ ] **Step 3: Register the intent**

In `install.py`, add to `_register_intents`:

```python
    dispatcher.register(IntentSpec(
        feature="auth",
        intent="create_database",
        title=lambda _params: "Create database",
        required=lambda c: c.is_admin or c.can_create_database,
    ))
```

And add the nav entry under the Authorization group:

```python
            NavEntry(
                "Create database",
                visible=lambda c: c.is_admin or c.can_create_database,
                on_click=TabIntent("auth", "create_database"),
            ),
```

- [ ] **Step 4: Add `render_create_database` to `intents.py`**

```python
async def render_create_database(
    request: Request,
    session: "AuthSession",
    rec: "TabRecord",
) -> Response:
    from fastapi import HTTPException
    from iris.shell.element_id import tab_panel_id

    if not (session.capabilities.is_admin or session.capabilities.can_create_database):
        raise HTTPException(status_code=403, detail="not allowed")

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "create_database.html",
        {"panel_id": tab_panel_id(rec.id), "tab_id": rec.id, "error": None},
    )


RENDER_BY_INTENT["create_database"] = render_create_database
```

- [ ] **Step 5: Create `create_database.html`**

```html
{# src/iris/features/authorization/templates/create_database.html #}
<div class="iris-feature-page" id="{{ panel_id }}">
  <h2>Create database</h2>
  <form id="{{ panel_id }}-form"
        data-on:submit="@post('/feature/auth/{{ tab_id }}/submit?name=' + encodeURIComponent($tabs.{{ tab_id }}.new_db_name || ''))">
    <label>
      Database name
      <input data-bind="tabs.{{ tab_id }}.new_db_name"
             placeholder="e.g. marketing">
    </label>
    {% if error %}
    <p class="iris-error" id="{{ panel_id }}-error">{{ error }}</p>
    {% endif %}
    <button type="submit">Create</button>
  </form>
</div>
```

- [ ] **Step 6: Add the submit route in `routes.py`**

```python
@router.post("/{tab_id}/submit")
async def submit_create_database(
    request: Request, session: Session, tab_id: str,
    name: Annotated[str, Query(min_length=0, max_length=64)],
    _: None = Depends(verify_csrf_header),
):
    from fastapi import HTTPException
    from iris.auth.views import DatabaseCreatorSession
    from iris.shell.tabs import find_tab, replace_tab, TabRecord
    from iris.shell.element_id import tab_panel_id

    rec = find_tab(session.data, tab_id)
    if rec is None or rec.feature != "auth" or rec.intent != "create_database":
        raise HTTPException(status_code=404, detail="tab not found")
    if not (session.capabilities.is_admin or session.capabilities.can_create_database):
        raise HTTPException(status_code=403, detail="not allowed")

    creator = DatabaseCreatorSession(
        id=session.id, user=session.user,
        created_at=session.created_at, expires_at=session.expires_at,
        data=session.data, capabilities=session.capabilities,
        client=session.client, http_client=session.http_client,
        settings=session.settings, store=session.store,
    )
    templates = request.app.state.templates
    panel_id = tab_panel_id(tab_id)

    try:
        await creator.create_database(name)
    except Exception as e:
        # Re-render the form with the error inline. Validation errors
        # from validate_identifier surface here as ValueError or
        # IdentifierError; CH-side errors as DatabaseError. All become
        # an inline error fragment — the user fixes and resubmits.
        html = templates.get_template("create_database.html").render(
            panel_id=panel_id, tab_id=tab_id, error=str(e),
        )
        return DatastarResponse(
            SSE.patch_elements(html, selector=f"#{panel_id}", mode="outer")
        )

    # Success: re-target the existing tab to manage <new_db>.
    new_rec = TabRecord(
        id=tab_id, feature="auth", intent="manage",
        params={"database": name}, title=f"Manage {name}",
    )
    replace_tab(session.data, tab_id, new_rec)
    await session.persist_data()
    return DatastarResponse([
        SSE.patch_elements(
            templates.get_template("shell/_tab_strip.html").render(tab=new_rec.to_json()),
            selector=f"#tab-button-{tab_id}",
            mode="outer",
        ),
        SSE.patch_elements(
            templates.get_template("shell/_tab_panel.html").render(tab=new_rec.to_json()),
            selector=f"#tab-content-{tab_id}",
            mode="outer",
        ),
    ])
```

- [ ] **Step 7: Run + gates + commit**

```bash
uv run pytest tests/features/test_authorization_create_database.py -v
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
git add -A
git commit -m "$(cat <<'EOF'
feat(features/authorization): create_database intent — form + submit

Single-screen form gated on is_admin or can_create_database (intent +
route layer). Submit POSTs the name as a query param, calls
DatabaseCreatorSession.create_database (validates identifier + creates
DB + grants creator the DBADMIN tier). On validation/CH error, the form
re-renders with an inline error fragment. On success, the same tab is
re-targeted to the manage intent for the new database — SSE morphs the
button title and panel content in place.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

End of Phase 5. A user with `can_create_database` can create a new database; the tab transitions seamlessly into the manage view for that DB.

---

## Phase 6 — `admin_console` intent with sub-tabs (spec §7 step 6)

The four sub-tabs (Users / Databases / Row policies / Audit) are read-mostly. Each is rendered server-side. Sub-tab selection is a per-tab signal (`$tabs.<tab_id>.subtab`). The first sub-tab (Users) renders inline at panel-init; the others lazy-fetch on first switch.

### Task 6.1: Register intent + Org admin nav sub-entries + admin_console shell with sub-tab framework

**Files:**
- Modify: `src/iris/features/authorization/install.py`
- Modify: `src/iris/features/authorization/intents.py`
- Create: `src/iris/features/authorization/templates/admin_console.html`
- Modify: `src/iris/features/authorization/routes.py` (sub-tab GET routes)
- Test: `tests/features/test_authorization_admin_console.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/features/test_authorization_admin_console.py
from __future__ import annotations

import asyncio


def _seed(app, sid: str, tab_id="AC12CD34"):
    asyncio.run(app.state.auth_session_store.update_data(sid, {"tabs": [
        {"id": tab_id, "feature": "auth", "intent": "admin_console",
         "params": {}, "title": "Org admin console"},
    ]}))


def _csrf(client):
    client.get("/")
    return {"Datastar-Request": "true",
            "X-CSRF-Token": client.cookies.get("iris_csrf") or ""}


def test_admin_console_intent_registered(app):
    spec = app.state.intent_dispatcher.resolve("auth", "admin_console")
    assert spec.title({}) == "Org admin console"


def test_admin_console_required_is_admin_only(app):
    from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
    spec = app.state.intent_dispatcher.resolve("auth", "admin_console")
    assert spec.required(EMPTY_CAPABILITIES) is False
    assert spec.required(Capabilities(
        is_admin=False, can_create_database=True,
        db_admin=frozenset({"x"}), db_writer=frozenset(), db_reader=frozenset(),
    )) is False
    assert spec.required(Capabilities(
        is_admin=True, can_create_database=False,
        db_admin=frozenset(), db_writer=frozenset(), db_reader=frozenset(),
    )) is True


def test_org_admin_nav_has_four_sub_entries(app):
    from iris.auth.rights import Capabilities
    contribs = app.state.contributions
    g = next(g for g in contribs.nav.groups if g.label == "Org admin")
    labels = [e.label for e in g.entries]
    assert labels == ["All users", "All databases", "Row policies", "Audit"]


def test_render_admin_console_shows_subtabs(app, capability_session):
    client, sid = asyncio.run(capability_session(is_admin=True))
    _seed(app, sid)
    r = client.get("/feature/auth/AC12CD34/render")
    assert r.status_code == 200
    assert ">Users<" in r.text or "Users</button>" in r.text
    assert "Databases" in r.text
    assert "Row policies" in r.text
    assert "Audit" in r.text


def test_render_admin_console_403_when_not_admin(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed(app, sid)
    r = client.get("/feature/auth/AC12CD34/render")
    assert r.status_code == 403


def test_subtab_get_users_403_when_not_admin(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed(app, sid)
    r = client.get("/feature/auth/AC12CD34/admin/users")
    assert r.status_code == 403


def test_subtab_get_users_returns_users_table(app, capability_session, monkeypatch):
    async def fake_users(self):
        return [{"name": "alice", "groups": ["data-team"]}]
    monkeypatch.setattr(
        "iris.features.authorization.service.list_all_users", fake_users,
    )
    client, sid = asyncio.run(capability_session(is_admin=True))
    _seed(app, sid)
    r = client.get("/feature/auth/AC12CD34/admin/users")
    assert r.status_code == 200
    assert "alice" in r.text


def test_subtab_get_databases_returns_databases_table(app, capability_session, monkeypatch):
    async def fake_dbs(self):
        return [{"name": "marketing", "admin_count": 1, "writer_count": 0, "reader_count": 3}]
    monkeypatch.setattr(
        "iris.features.authorization.service.list_all_databases", fake_dbs,
    )
    client, sid = asyncio.run(capability_session(is_admin=True))
    _seed(app, sid)
    r = client.get("/feature/auth/AC12CD34/admin/databases")
    assert r.status_code == 200
    assert "marketing" in r.text


def test_subtab_get_policies_returns_policies_table(app, capability_session, monkeypatch):
    async def fake_pol(self):
        return [{"database": "marketing", "table": "events",
                 "name": "p1", "select_filter": "user_id = $alice"}]
    monkeypatch.setattr(
        "iris.features.authorization.service.list_all_row_policies", fake_pol,
    )
    client, sid = asyncio.run(capability_session(is_admin=True))
    _seed(app, sid)
    r = client.get("/feature/auth/AC12CD34/admin/policies")
    assert r.status_code == 200
    assert "marketing" in r.text and "events" in r.text


def test_subtab_get_audit_returns_grants_table(app, capability_session, monkeypatch):
    async def fake_audit(self):
        return [{"user_name": "bob", "role_name": None,
                 "access_type": "INSERT", "database": "events"}]
    monkeypatch.setattr(
        "iris.features.authorization.service.list_all_grants", fake_audit,
    )
    client, sid = asyncio.run(capability_session(is_admin=True))
    _seed(app, sid)
    r = client.get("/feature/auth/AC12CD34/admin/audit")
    assert r.status_code == 200
    assert "bob" in r.text and "INSERT" in r.text
```

- [ ] **Step 2: Register the intent + nav sub-entries**

In `install.py`, append to `_register_intents`:

```python
    dispatcher.register(IntentSpec(
        feature="auth",
        intent="admin_console",
        title=lambda _params: "Org admin console",
        required=lambda c: c.is_admin,
    ))
```

Replace the empty `Org admin` NavGroup with:

```python
    contribs.nav.add(NavGroup(
        label="Org admin",
        visible=lambda c: c.is_admin,
        entries=[
            NavEntry("All users",      on_click=TabIntent("auth", "admin_console", {"subtab": "users"})),
            NavEntry("All databases",  on_click=TabIntent("auth", "admin_console", {"subtab": "databases"})),
            NavEntry("Row policies",   on_click=TabIntent("auth", "admin_console", {"subtab": "policies"})),
            NavEntry("Audit",          on_click=TabIntent("auth", "admin_console", {"subtab": "audit"})),
        ],
    ))
```

(All four entries open the same admin_console intent but seed a different `subtab` param — the panel reads it to choose the initial sub-tab.)

- [ ] **Step 3: Add `render_admin_console` in `intents.py`**

```python
async def render_admin_console(
    request: Request,
    session: "AuthSession",
    rec: "TabRecord",
) -> Response:
    from fastapi import HTTPException
    from iris.shell.element_id import tab_panel_id

    if not session.capabilities.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    templates = request.app.state.templates
    initial_subtab = rec.params.get("subtab", "users")
    return templates.TemplateResponse(
        request,
        "admin_console.html",
        {
            "panel_id": tab_panel_id(rec.id),
            "tab_id": rec.id,
            "initial_subtab": initial_subtab,
        },
    )


RENDER_BY_INTENT["admin_console"] = render_admin_console
```

- [ ] **Step 4: Create `admin_console.html`**

```html
{# src/iris/features/authorization/templates/admin_console.html #}
<div class="iris-feature-page" id="{{ panel_id }}"
     data-signals='{"tabs": {{ {("" + tab_id): {"subtab": initial_subtab}} | tojson }}}'>
  <h2>Org admin console</h2>
  <div class="iris-subtabs">
    {% for st in [("users", "Users"), ("databases", "Databases"),
                  ("policies", "Row policies"), ("audit", "Audit")] %}
    <button data-class="{active: $tabs.{{ tab_id }}.subtab === {{ st[0] | tojson }}}"
            data-on:click="$tabs.{{ tab_id }}.subtab = {{ st[0] | tojson }}; @get('/feature/auth/{{ tab_id }}/admin/' + {{ st[0] | tojson }})">
      {{ st[1] }}
    </button>
    {% endfor %}
  </div>
  <div id="{{ panel_id }}-subtab"
       data-init="@get('/feature/auth/{{ tab_id }}/admin/' + ({{ initial_subtab | tojson }}))">
  </div>
</div>
```

(The initial sub-tab lazy-fetches via `data-init`. Subsequent switches do `@get` again — it's a fresh server roundtrip per switch, simplest pattern. State preservation per sub-tab can be added later if a real need surfaces.)

- [ ] **Step 5: Create the four sub-tab partials**

```html
{# src/iris/features/authorization/templates/_admin_users.html #}
<section id="{{ panel_id }}-subtab">
  <h3>Users</h3>
  <table>
    <thead><tr><th>Username</th><th>Groups</th><th>Actions</th></tr></thead>
    <tbody>
      {% for u in users %}
      <tr>
        <td>{{ u.name }}</td>
        <td>{{ u.groups | join(", ") }}</td>
        <td>
          <button data-on:click="@post('/feature/auth/{{ tab_id }}/admin/users/' + encodeURIComponent({{ u.name | tojson }}) + '/reprovision')">
            reprovision
          </button>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</section>
```

```html
{# src/iris/features/authorization/templates/_admin_databases.html #}
<section id="{{ panel_id }}-subtab">
  <h3>Databases</h3>
  <table>
    <thead><tr><th>Name</th><th>Admins</th><th>Writers</th><th>Readers</th><th></th></tr></thead>
    <tbody>
      {% for db in databases %}
      <tr>
        <td>{{ db.name }}</td>
        <td>{{ db.admin_count }}</td>
        <td>{{ db.writer_count }}</td>
        <td>{{ db.reader_count }}</td>
        <td>
          <button data-on:click="@post('/api/tabs?feature=auth&amp;intent=manage&amp;params=' + encodeURIComponent(JSON.stringify({database: {{ db.name | tojson }}})))">
            manage
          </button>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</section>
```

```html
{# src/iris/features/authorization/templates/_admin_policies.html #}
<section id="{{ panel_id }}-subtab">
  <h3>Row policies (all databases)</h3>
  <table>
    <thead><tr><th>Database</th><th>Table</th><th>Name</th><th>Filter</th></tr></thead>
    <tbody>
      {% for p in policies %}
      <tr>
        <td>{{ p.database }}</td>
        <td>{{ p.table }}</td>
        <td>{{ p.name | default("?") }}</td>
        <td><code>{{ p.select_filter | default("") }}</code></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</section>
```

```html
{# src/iris/features/authorization/templates/_admin_audit.html #}
<section id="{{ panel_id }}-subtab">
  <h3>Audit (system.grants)</h3>
  <table>
    <thead><tr><th>Grantee</th><th>Access</th><th>Database</th><th>Table</th></tr></thead>
    <tbody>
      {% for row in grants %}
      <tr>
        <td>{{ row.user_name or ("role: " + (row.role_name or "?")) }}</td>
        <td>{{ row.access_type }}</td>
        <td>{{ row.database or "—" }}</td>
        <td>{{ row.table or "—" }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</section>
```

- [ ] **Step 6: Add the four service helpers in `service.py`**

```python
async def list_all_users(session: "AdminSession") -> list[dict[str, Any]]:
    """All users with their group memberships. Uses CH system.users +
    iris's existing groups-on-user metadata (if available) or just
    falls back to the username with no groups."""
    # System.users gives username; group membership in iris is derived from
    # the SSO claim at login time and stored in the session row, not in CH.
    # For the admin view we list CH usernames and (best-effort) any roles
    # they hold via system.role_grants.
    import asyncio
    client = session._ch()[0]
    def _q():
        rows = client.query("SELECT name FROM system.users ORDER BY name")
        users: list[dict[str, Any]] = []
        for row in rows.named_results():
            uname = row["name"]
            role_rows = client.query(
                "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String}",
                {"u": uname},
            )
            roles = [r["granted_role_name"] for r in role_rows.named_results()]
            users.append({"name": uname, "groups": roles})
        return users
    return await asyncio.to_thread(_q)


async def list_all_databases(session: "AdminSession") -> list[dict[str, Any]]:
    """All databases with admin/writer/reader counts derived from system.role_grants."""
    import asyncio
    from iris.clickhouse.grants import (
        TIER_DBADMIN, TIER_DBREADER, TIER_DBWRITER, tier_role_name,
    )
    client = session._ch()[0]
    def _q():
        db_rows = client.query("SELECT name FROM system.databases ORDER BY name")
        out: list[dict[str, Any]] = []
        for row in db_rows.named_results():
            db = row["name"]
            counts = {}
            for tier_const, key in (
                (TIER_DBADMIN, "admin_count"),
                (TIER_DBWRITER, "writer_count"),
                (TIER_DBREADER, "reader_count"),
            ):
                role = tier_role_name(db, tier_const)
                count_rows = client.query(
                    "SELECT count() AS c FROM system.role_grants WHERE granted_role_name = {r:String}",
                    {"r": role},
                )
                counts[key] = next(count_rows.named_results())["c"]
            out.append({"name": db, **counts})
        return out
    return await asyncio.to_thread(_q)


async def list_all_row_policies(session: "AdminSession") -> list[dict[str, Any]]:
    import asyncio
    client = session._ch()[0]
    def _q():
        rows = client.query("SELECT * FROM system.row_policies ORDER BY database, table")
        return list(rows.named_results())
    return await asyncio.to_thread(_q)


async def list_all_grants(session: "AdminSession") -> list[dict[str, Any]]:
    import asyncio
    client = session._ch()[0]
    def _q():
        rows = client.query("SELECT * FROM system.grants ORDER BY database, user_name, role_name")
        return list(rows.named_results())
    return await asyncio.to_thread(_q)
```

- [ ] **Step 7: Add the four sub-tab GET routes in `routes.py`**

```python
def _promote_to_admin(session):
    from fastapi import HTTPException
    from iris.auth.views import AdminSession
    if not session.capabilities.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return AdminSession(
        id=session.id, user=session.user,
        created_at=session.created_at, expires_at=session.expires_at,
        data=session.data, capabilities=session.capabilities,
        client=session.client, http_client=session.http_client,
        settings=session.settings, store=session.store,
    )


@router.get("/{tab_id}/admin/users")
async def admin_users(request: Request, session: Session, tab_id: str):
    from iris.shell.element_id import tab_panel_id
    from iris.features.authorization.service import list_all_users
    admin = _promote_to_admin(session)
    users = await list_all_users(admin)
    templates = request.app.state.templates
    html = templates.get_template("_admin_users.html").render(
        panel_id=tab_panel_id(tab_id), tab_id=tab_id, users=users,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{tab_panel_id(tab_id)}-subtab", mode="outer"
    ))


@router.get("/{tab_id}/admin/databases")
async def admin_databases(request: Request, session: Session, tab_id: str):
    from iris.shell.element_id import tab_panel_id
    from iris.features.authorization.service import list_all_databases
    admin = _promote_to_admin(session)
    databases = await list_all_databases(admin)
    templates = request.app.state.templates
    html = templates.get_template("_admin_databases.html").render(
        panel_id=tab_panel_id(tab_id), tab_id=tab_id, databases=databases,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{tab_panel_id(tab_id)}-subtab", mode="outer"
    ))


@router.get("/{tab_id}/admin/policies")
async def admin_policies(request: Request, session: Session, tab_id: str):
    from iris.shell.element_id import tab_panel_id
    from iris.features.authorization.service import list_all_row_policies
    admin = _promote_to_admin(session)
    policies = await list_all_row_policies(admin)
    templates = request.app.state.templates
    html = templates.get_template("_admin_policies.html").render(
        panel_id=tab_panel_id(tab_id), tab_id=tab_id, policies=policies,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{tab_panel_id(tab_id)}-subtab", mode="outer"
    ))


@router.get("/{tab_id}/admin/audit")
async def admin_audit(request: Request, session: Session, tab_id: str):
    from iris.shell.element_id import tab_panel_id
    from iris.features.authorization.service import list_all_grants
    admin = _promote_to_admin(session)
    grants = await list_all_grants(admin)
    templates = request.app.state.templates
    html = templates.get_template("_admin_audit.html").render(
        panel_id=tab_panel_id(tab_id), tab_id=tab_id, grants=grants,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{tab_panel_id(tab_id)}-subtab", mode="outer"
    ))
```

- [ ] **Step 8: Run + gates + commit**

```bash
uv run pytest tests/features/test_authorization_admin_console.py -v
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
git add -A
git commit -m "$(cat <<'EOF'
feat(features/authorization): admin_console intent — Users/DBs/Policies/Audit sub-tabs

Registers admin_console intent (gated is_admin) and four sub-entries
under the Org admin nav group, each opening admin_console with a
distinct {subtab} param so the panel knows which sub-tab to show first.
Sub-tab selection is a per-tab signal; switching does a fresh server
roundtrip via @get to one of /admin/{users,databases,policies,audit}.
Each route promotes the session to AdminSession, fetches the data via
a service helper that queries CH system tables, and SSE-morphs the
sub-tab section.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6.2: Reprovision-user action on the Users sub-tab

**Files:**
- Modify: `src/iris/features/authorization/routes.py`
- Test: extend `tests/features/test_authorization_admin_console.py`

The Users sub-tab template already wires the reprovision button (`@post`); add the route.

- [ ] **Step 1: Write the failing test**

Append to `tests/features/test_authorization_admin_console.py`:

```python
def test_reprovision_user_403_when_not_admin(app, capability_session):
    client, sid = asyncio.run(capability_session())
    _seed(app, sid)
    r = client.post(
        "/feature/auth/AC12CD34/admin/users/alice/reprovision",
        headers=_csrf(client),
    )
    assert r.status_code == 403


def test_reprovision_user_calls_admin_session_method(
    app, capability_session, monkeypatch
):
    calls = []
    async def fake_reprov(self, *, username, groups):
        calls.append(("reprov", username, list(groups)))
    monkeypatch.setattr(
        "iris.auth.views.AdminSession.reprovision_user", fake_reprov,
    )
    async def fake_users(s):
        return [{"name": "alice", "groups": []}]
    monkeypatch.setattr(
        "iris.features.authorization.service.list_all_users", fake_users,
    )
    client, sid = asyncio.run(capability_session(is_admin=True))
    _seed(app, sid)
    r = client.post(
        "/feature/auth/AC12CD34/admin/users/alice/reprovision",
        headers=_csrf(client),
    )
    assert r.status_code == 200
    # reprovision is called with empty groups (we don't have access to the
    # user's IdP groups from this code path; reprovision rebuilds from CH state)
    assert calls == [("reprov", "alice", [])]
```

- [ ] **Step 2: Add the route**

```python
@router.post("/{tab_id}/admin/users/{username}/reprovision")
async def admin_reprovision_user(
    request: Request, session: Session, tab_id: str, username: str,
    _: None = Depends(verify_csrf_header),
):
    from iris.shell.element_id import tab_panel_id
    from iris.features.authorization.service import list_all_users
    admin = _promote_to_admin(session)
    # We don't have the user's IdP groups here (that's a session-only fact);
    # reprovision_user rebuilds from CH state with empty groups.
    await admin.reprovision_user(username=username, groups=[])
    # Re-render the users sub-tab to reflect any state changes
    users = await list_all_users(admin)
    templates = request.app.state.templates
    html = templates.get_template("_admin_users.html").render(
        panel_id=tab_panel_id(tab_id), tab_id=tab_id, users=users,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{tab_panel_id(tab_id)}-subtab", mode="outer"
    ))
```

- [ ] **Step 3: Run + gates + commit**

```bash
uv run pytest tests/features/test_authorization_admin_console.py -v
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
git add -A
git commit -m "$(cat <<'EOF'
feat(features/authorization): admin Users sub-tab — reprovision route

POST /feature/auth/{tab_id}/admin/users/{username}/reprovision calls
AdminSession.reprovision_user (rebuilds CH user identity + tier roles
from current state). Re-renders the Users sub-tab so the table reflects
the post-reprovision state.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

End of Phase 6. The full Authorization feature is shipped: my_access, manage (members + policies + audit + danger), create_database, admin_console (Users + Databases + Policies + Audit, with reprovision action). All routes pass through the three defense-in-depth layers.

---

## Phase 7 — `CLAUDE.md` update (spec §7 step 7)

Add a Frontend section to `CLAUDE.md` linking to `docs/frontend.md`, plus the conventions and discipline rules that constrain future feature work.

### Task 7.1: Update `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Read the existing CLAUDE.md to find the right insertion point**

```bash
grep -n "^##" CLAUDE.md
```

Identify the line after `## Architecture & Datastar integration` (or wherever the layout/architecture section ends) and before `## Module map`. The new Frontend section goes between them.

- [ ] **Step 2: Edit `CLAUDE.md` to add the Frontend section**

Insert the following section before `## Module map`:

````markdown
## Frontend architecture

The user-facing frontend is built on a shell module (`src/iris/shell/`) plus
feature modules under `src/iris/features/<name>/`. Full surface in
`docs/frontend.md`.

### Conventions an agent must follow that aren't obvious from reading code

- **One feature = one directory** under `src/iris/features/<name>/`. Required
  contents: `install.py` (with public `install(app)` re-exported from
  `__init__.py`), `routes.py` (with `APIRouter(prefix="/feature/<name>")`),
  `intents.py` (with `RENDER_BY_INTENT` mapping intent names to render
  functions), `service.py` (read-side helpers, no FastAPI imports), and
  `templates/` for Jinja templates. Optional: `static/` for feature-specific
  assets.
- **Install order is fixed**: `build_app` calls auth → clickhouse → shell →
  features → `init_templates()`. Features assume `app.state.contributions`
  and `app.state.intent_dispatcher` exist; the shell creates them.
- **Templates**: each subsystem / feature owns its templates dir, registered
  via `iris.templates.register_template_dir(...)` from its `install`. The
  process-wide loader is built once by `init_templates()` after all installs.
  First-registered wins on path collisions; namespace by directory
  (`shell/shell.html`, `auth/forbidden.html`, …).
- **Tabs are server-side state.** Open tabs live in `session.data['tabs']`
  (a list of `{id, feature, intent, params, title}` dicts). Mutations go
  through `iris.shell.tabs.{append,remove,replace}_tab` then
  `await session.persist_data()`. Do not store tab state in localStorage or
  the URL — refresh restores from `session.data`.
- **Per-tab signals** live under `$tabs.<tab_id>.*`. DOM ids inside a tab
  fragment are derived from the tab id via `iris.shell.element_id.el(...)`.
  Server-side only; never compute ids in JS.
- **Datastar discipline.** Server is the source of truth for state. Signals
  carry only ephemeral UI state (`$active`, `$nav_collapsed`, form input
  bindings). All structural changes are SSE patches via
  `DatastarResponse([SSE.patch_elements(...), SSE.patch_signals(...)])`.
  No JS in templates. Lazy-load fragments with `data-init="@get(...)"`
  (NOT `data-on:load` — `load` doesn't fire on `<div>`).
- **Defense in depth, three layers**:
  1. Nav rendering (`render_nav` filters by `Capabilities`).
  2. Intent gate (`POST /api/tabs` runs the intent's `required` predicate).
  3. Per-route guard (every feature route uses `Annotated` `Session*` deps).
  Only (3) enforces; (1) and (2) are UX. Always implement all three.
- **Contribution registry discipline rule.** Do not add a new registry to
  `iris.shell.contributions.Contributions` until at least one feature has a
  concrete need to contribute and at least one feature has a concrete need
  to consume. Every registry is permanent API surface.
- **No cross-feature imports.** Features may import `iris.auth`,
  `iris.clickhouse`, `iris.shell` — never another feature. Cross-feature
  integration goes through the contribution registry. (Soft rule for now;
  reconsider if a real exception appears.)
- **CSRF on every state-changer.** Datastar `@post` / `@put` / `@patch` /
  `@delete` routes use `Depends(verify_csrf_header)` (token transmitted via
  the `X-CSRF-Token` HTTP header read from the JS-readable `iris_csrf`
  cookie). Form POSTs use `verify_csrf_form`.
- **Tab cap.** `MAX_TABS_PER_SESSION = 32`. Over the cap returns 409.
````

Also update the existing `## Module map` section to include the new directories:

````markdown
## Module map

```
src/iris/
├── __init__.py        # main() + load_dotenv
├── app.py             # build_app() — wires auth, ch, shell, features
├── middleware.py      # SecurityHeadersMiddleware (CSP)
├── templates.py       # register_template_dir / init_templates registry
├── auth/              # auth subsystem — full surface in docs/auth.md
├── clickhouse/        # CH subsystem — full surface in docs/clickhouse.md
├── shell/             # frontend shell — full surface in docs/frontend.md
├── features/          # feature modules — one dir per feature
│   └── authorization/  # Authorization feature (my_access / manage / create_database / admin_console)
├── static/            # global vendored assets (datastar.js)
└── (auth/ and shell/ each ship their own static/ subdir mounted at /static/<name>/)
```
````

And update the `## See also` section:

```markdown
## See also

- `docs/auth.md` — full auth surface (alias deps, Session hierarchy, providers, login flows, tests)
- `docs/clickhouse.md` — full CH surface (tier roles, bootstrap, row policies, the bridge with auth)
- `docs/frontend.md` — full frontend surface (shell, contributions, tabs, Datastar conventions)
- `docs/operations.md` — deployment, env-var depth, security follow-ups, migration runbooks
```

- [ ] **Step 3: Verify gates still pass (no code change but a sanity check)**

```bash
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -x
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: zero failures, zero issues.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs(claude-md): add Frontend architecture section + conventions

Documents the feature-module pattern (src/iris/features/<name>/), the
fixed install order, the templates-loader registry, the tab system
conventions (server-side state, derived ids, per-tab signals), the
Datastar discipline rules (lazy fragments via data-init not data-on:load,
no JS in templates), the three-layer defense-in-depth model, the
contribution-registry discipline rule, the no-cross-feature-imports rule,
CSRF on every state-changer, and the per-session tab cap. Updates the
Module map and See also to reference shell/, features/, and
docs/frontend.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

End of Phase 7. The implementation is complete. CLAUDE.md captures the conventions for future feature authors.

---

## Recap

7 phases, 19 tasks, 19 commits. End-state:

- A two-panel collapsible-nav shell with server-persisted tabs.
- A typed `Contributions` registry on `app.state` (only `nav` shipped).
- An `IntentDispatcher` enforcing layer-2 of defense in depth.
- A per-feature template + static + routes convention under `src/iris/features/<name>/`.
- The Authorization feature: `my_access` (capability-adaptive home), `manage`
  (per-database CRUD across members / row policies / audit / danger zone),
  `create_database` (form with success → re-target to manage), `admin_console`
  (4 sub-tabs over CH system tables, with reprovision action on Users).
- `docs/frontend.md` documenting the shell + conventions.
- `CLAUDE.md` updated with the rules future agents must follow.

All routes pass three authz layers; all state-changers are CSRF-protected;
all SSE responses follow the Datastar `patch_elements` / `patch_signals`
pattern; no JS in templates; tabs survive refresh via `session.data['tabs']`.

