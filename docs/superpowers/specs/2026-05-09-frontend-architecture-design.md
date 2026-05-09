# Frontend architecture — design

**Date:** 2026-05-09
**Status:** approved, ready for implementation plan

## Context

Iris today is a FastAPI + Datastar + Jinja2 server with two fully-developed backend subsystems (`iris.auth`, `iris.clickhouse`), each shipping an `install(app)` that wires routes, exception handlers, state, hooks, and templates. Cross-subsystem extension already works via named hook lists on `app.state` (`shutdown_hooks`, `post_login_hooks`); templates are namespaced by subsystem (`templates/auth/forbidden.html`); authz is encoded as `Annotated` dep aliases (`Session`, `SessionRead`, `SessionDatabaseAdmin`, …) so routes declare their requirements via type annotations.

What does *not* exist yet is the user-facing frontend. The current `app.py` defines three demo routes (`/`, `/api/greet`, `/api/clock`) and `templates/index.html` to prove the Datastar wiring works.

The next sessions will start building that frontend. The user has signalled the application will grow into four feature categories on top of the existing auth/CH foundation:

- DB administration & governance (manage CH databases, grants, users, row policies, audit)
- Interactive data tooling (SQL workbench, schema browser, table explorer, query history)
- Dashboards & visualization (charts, saved/shared views, scheduled reports)
- Ingestion & pipelines (CSV/file upload, scheduled imports, transform jobs)

The user's stated concern is that without a clear architectural choice up-front, this will produce routes scattered across hard-to-find locations and a `templates/` folder that becomes unnavigable.

## Goal

Establish the architectural shape for iris's frontend before any feature code is written, so each future feature lands in a predictable, isolated location and integrates with the rest of the app through a small set of well-typed extension points. Prove the shape by designing — but not yet implementing all of — the first feature: an Authorization management UI that adapts to the logged-in user's capabilities.

## Non-goals (out of scope for this spec)

- **Persistence model for future features.** Authorization needs none beyond what already exists (CH grants/policies, SQLite session store). Workbench/dashboards/ingestion will each motivate their own persistence decision when started; not pre-deciding now.
- **Workbench, Dashboards, Ingestion features.** Their directories may be empty at MVP; they get their own design specs when work begins.
- **Cross-feature integration registries** beyond `nav` (table actions, settings tabs, result actions, database actions, etc.). Added one at a time when a real cross-feature integration appears.
- **Third-party / out-of-tree plugin loading.** Iris is a single-repo integrated app. Features are convention-based modules in `src/iris/features/`, not loadable wheels.
- **URL-driven tab state.** Tabs are persisted in `session.data['tabs']`; refresh restores them. Promotion to URL-shareable tab state is additive and deferred.

## Architectural decision

**Integrated app with feature modules + typed contribution registry.** One cohesive UI with a shared shell (two-panel layout, tabs); features are directories under `src/iris/features/<name>/` that each ship an `install(app)` and contribute to the shell through a single typed registry. Only the `nav` extension point exists at MVP; new extension points get added when (and only when) a concrete cross-feature integration needs one.

Three options were considered and rejected:

- **Lightweight feature modules with no contribution registry.** Cheaper, but cross-feature UI integration would require direct imports between features. Rejected because the user wants the framework in place from day one.
- **Full plugin protocol with discovery.** Manifests, lifecycle hooks, entry-point loading. Rejected because there is no third-party / out-of-tree requirement; the ceremony is overkill for an integrated app.

The chosen shape extends what already works in `auth/` and `clickhouse/`: one `install(app)` per module, templates namespaced by module, state stashed on `app.state`, hooks via shared lists.

---

## 1. Shell layout

Two panels, no global header.

- **Left panel:** collapsible nav. Expanded shows feature label + (optional) icon + (optional) sub-entries; collapsed shows icon-only.
- **Right panel:** tab strip on top, active tab content below. Multiple tabs can hold different intents of the same feature concurrently.

### 1.1 Mockups

Expanded:

```
┌──────────────────┬──────────────────────────────────────────────┐
│ ◀ ⚙ 👤           │ ┌─────────┐┌──────────┐┌──────────┐┌─┐       │
│                  │ │My access││Manage    ││Workbench ││+│       │
│ 🔐 Authorization │ │         ││ analyt…  ││ #2       ││ │       │
│    My access     │ └─────────┘└──────────┘└══════════┘└─┘       │
│    Databases I   │ ┌──────────────────────────────────────────┐ │
│    admin (3) ▸   │ │                                          │ │
│    Create db     │ │                                          │ │
│                  │ │                                          │ │
│ ⌨  Workbench     │ │   <content of the currently active tab>  │ │
│ 📊 Dashboards    │ │                                          │ │
│ 📥 Ingestion     │ │                                          │ │
│                  │ │                                          │ │
│ ── Org admin ──  │ │                                          │ │
│    All users     │ │                                          │ │
│    Row policies  │ │                                          │ │
│    Audit         │ │                                          │ │
└──────────────────┴──────────────────────────────────────────────┘
```

Collapsed (icon-only nav):

```
┌──┬─────────────────────────────────────────────────────────────┐
│▶ │ ┌─────────┐┌──────────┐┌──────────┐┌─┐                       │
│⚙ │ │My access││Manage…   ││Workben…  ││+│                       │
│👤│ └─────────┘└──────────┘└══════════┘└─┘                       │
│  │ ┌─────────────────────────────────────────────────────────┐  │
│🔐│ │                                                         │  │
│⌨ │ │                                                         │  │
│📊│ │            <content of the active tab>                  │  │
│📥│ │                                                         │  │
│  │ │                                                         │  │
└──┴─────────────────────────────────────────────────────────────┘
```

The icons in the mockups are visual placeholders for the icon design; the implementation uses inline SVG (or a small icon font) rendered server-side from a string identifier on each `NavGroup` / `NavEntry`.

### 1.2 Top-left buttons

Three shell-owned buttons stacked above the nav:

- **◀ / ▶** — toggle nav collapse. Flips a `$nav_collapsed` boolean signal; CSS reads `[data-nav-collapsed]` on the shell root for the width swap.
- **⚙ Settings** — opens the (yet-to-exist) Settings feature in a tab; focuses the existing one if already open.
- **👤 Account** — popover with display name, group memberships, and the existing CSRF-protected `Sign out` form-POST.

Features cannot add buttons here in the MVP. If a real cross-cutting need appears later, a `top_buttons` extension point is added to the contribution registry then.

---

## 2. Tab system

A tab is one instance of a feature page. Multiple tabs can run the same feature with different parameters (e.g., "Manage analytics" and "Manage marketing" each in their own tab). Each tab has its own DOM ids and its own slice of signals — no leakage.

### 2.1 Conventions

- **Tab id**: short server-generated URL-safe random string (`secrets.token_urlsafe(6)` → 8 chars). Lives in the URL of every route inside the tab: `/feature/<feature>/{tab_id}/...`.
- **DOM id helper**: every id in a tab fragment is derived from `tab_id`. A single helper, exposed as a Jinja global:
  ```python
  def el(tab_id: str, *parts: str) -> str:
      return "t-" + tab_id + "-" + "-".join(parts)
  # el("AB12CD34", "results") → "t-AB12CD34-results"
  ```
  Templates write `id="{{ el(tab_id, 'results') }}"`.
- **Per-tab signals**: live under a `tabs` namespace. The shell declares `data-signals="{tabs: {}, active: ''}"` once on the shell root. When a tab opens, the SSE response includes `patch_signals({tabs: {AB12CD34: {…initial state…}}, active: 'AB12CD34'})`. Inside the fragment, expressions reference `$tabs.AB12CD34.search` etc., where the `tab_id` literal is rendered server-side.
- **Visibility**: each panel is shown via `data-show="$active === 'AB12CD34'"`. Closing a tab removes the panel and deletes the `$tabs.AB12CD34` subtree.
- **Tab cap**: maximum 32 open tabs per session, enforced server-side on tab open; over the cap returns a fragment with an inline error toast and the tab is not created.

### 2.2 Tab persistence

Open tabs are server-side state, persisted on the session row.

```python
session.data['tabs'] = [
    {"id": "AB12CD34", "feature": "auth", "intent": "manage",
     "params": {"database": "marketing"}, "title": "Manage marketing"},
    {"id": "EF56GH78", "feature": "auth", "intent": "my_access",
     "params": {}, "title": "My access"},
]
```

Persisted via the existing `await session.persist_data()`. Refresh restores the full tab strip; no localStorage, no JS.

### 2.3 Tab open / close / switch / re-render flow

All state-changing operations are CSRF-protected POST/DELETE/PATCH on the shell's `/api/tabs` routes.

**Open** — `POST /api/tabs?feature=auth&intent=manage&database=marketing`
1. Server generates `tab_id`, validates the intent against the feature's intent registry, gates on capabilities (intent gate), appends to `session.data['tabs']`, persists.
2. Returns SSE:
   - `patch_elements` to append `<button id="tab-button-AB12CD34" data-on:click="$active='AB12CD34'">Manage marketing</button>` to `#tab-strip`.
   - `patch_elements` to append `<div id="tab-content-AB12CD34" data-show="$active==='AB12CD34'" data-on:load="@get('/feature/auth/AB12CD34/render')"></div>` to `#tab-content`.
   - `patch_signals` to seed `$tabs.AB12CD34 = {…}` and set `$active = 'AB12CD34'`.
3. The `data-on:load` causes the empty panel to immediately fetch its real HTML from the server. Lazy panel rendering keeps the open response light.

**Close** — `DELETE /api/tabs/AB12CD34`
1. Server removes the entry from `session.data['tabs']`, persists.
2. Returns SSE:
   - `patch_elements` to remove `#tab-button-AB12CD34` and `#tab-content-AB12CD34`.
   - `patch_signals` to delete the `$tabs.AB12CD34` subtree and update `$active` to a sibling tab (or `''`).

**Switch** — purely client-side. Tab buttons use `data-on:click="$active='AB12CD34'"`. No round-trip; `data-show` re-evaluates.

**Re-target an existing tab** — `PATCH /api/tabs/AB12CD34` with new `intent` + `params`. Server updates the entry in `session.data['tabs']`, persists, returns SSE that morphs `#tab-content-AB12CD34` with the new render and updates the tab button label.

### 2.4 Datastar discipline

This design adheres to Datastar's hypermedia-first philosophy:

- **Server is the source of truth for state.** Open tabs, their parameters, capabilities — all server-side. Signals carry only ephemeral UI state (active tab, nav-collapsed, form input bindings).
- **All structural changes are SSE patches** of HTML fragments returned by FastAPI routes.
- **No JS in templates.** All interactivity is via Datastar attributes (`data-on:*`, `data-bind`, `data-show`, `data-signals`).
- **All state-changing actions are CSRF-protected** POST/DELETE/PATCH (existing pattern from `/logout`).

---

## 3. Authorization feature UI

The feature's directory is `src/iris/features/authorization/`. It exposes four intents, each adapting to the logged-in user's `Capabilities`.

### 3.1 Intents

| Intent             | Required capability                             | Tab title                  |
|--------------------|-------------------------------------------------|----------------------------|
| `my_access`        | any signed-in user                              | "My access"                |
| `manage`           | `capabilities.has_admin(database)`              | "Manage \<database\>"      |
| `create_database`  | `capabilities.is_admin or capabilities.can_create_database` | "Create database" |
| `admin_console`    | `capabilities.is_admin`                         | "Org admin console"        |

Each intent is a function in `intents.py` that takes the session + tab params and returns the panel HTML.

### 3.2 Nav contributions

The auth feature contributes two `NavGroup`s. Predicates are evaluated per-render against `session.capabilities`.

```python
# src/iris/features/authorization/install.py
def install(app: FastAPI) -> None:
    contribs: Contributions = app.state.contributions
    contribs.nav.add(NavGroup(
        label="Authorization", icon="lock",
        entries=[
            NavEntry("My access", on_click=TabIntent("auth", "my_access")),
            NavEntry("Databases I admin",
                visible=lambda c: bool(c.db_admin),
                badge=lambda c: str(len(c.db_admin)),
                children=lambda c: [
                    NavEntry(db, on_click=TabIntent("auth", "manage", {"database": db}))
                    for db in sorted(c.db_admin)
                ]),
            NavEntry("Create database",
                visible=lambda c: c.is_admin or c.can_create_database,
                on_click=TabIntent("auth", "create_database")),
        ],
    ))
    contribs.nav.add(NavGroup(
        label="Org admin", icon="bolt",
        visible=lambda c: c.is_admin,
        entries=[
            NavEntry("All users",      on_click=TabIntent("auth", "admin_users")),
            NavEntry("All databases",  on_click=TabIntent("auth", "admin_databases")),
            NavEntry("Row policies",   on_click=TabIntent("auth", "admin_policies")),
            NavEntry("Audit",          on_click=TabIntent("auth", "admin_audit")),
        ],
    ))
    # mount the feature's APIRouter
    from .routes import router
    app.include_router(router, prefix="/feature/auth")
```

Children-rendering rule: if `len(children(c)) > 10`, the children render as a scrollable popover instead of inline list, so the nav doesn't blow out vertically when a user admins many databases.

### 3.3 `my_access` panel (every signed-in user)

```
┌──────────────────────────────────────────────────────────┐
│  My access                                               │
│                                                          │
│  ┌──────────────────────────────────────────────────┐    │
│  │ alice@example.com                                │    │
│  │ Groups: analytics, dev                           │    │
│  └──────────────────────────────────────────────────┘    │
│                                                          │
│  Databases you can read                                  │
│    marketing          [open in workbench →]              │
│    analytics_dev      [open in workbench →]              │
│                                                          │
│  Databases you can write to                              │
│    analytics_dev      [open in workbench →]              │
│                                                          │
│  Databases you administer                                │
│    marketing          [manage →]                         │
│                                                          │
│  ─────────────────────────────────────────────────       │
│                                                          │
│  [ + Create new database ]      ← if can_create_database │
│                                                          │
│  ─────────────────────────────────────────────────       │
│  Org administration               ← if is_admin           │
│  [ Open admin console → ]                                │
└──────────────────────────────────────────────────────────┘
```

Render rules:
- Each tier section (`Readers/Writers/Admins`) is omitted entirely when its set is empty.
- The "Create database" CTA is rendered only when `capabilities.is_admin or capabilities.can_create_database`.
- The "Org administration" block is rendered only when `capabilities.is_admin`.
- `[open in workbench →]` issues `POST /api/tabs?feature=workbench&intent=open&database=...`
- `[manage →]` issues `POST /api/tabs?feature=auth&intent=manage&database=...`
- `[Open admin console →]` issues `POST /api/tabs?feature=auth&intent=admin_console`

### 3.4 `manage` panel (per-database admin)

Loaded only by users with `capabilities.has_admin(database)`. Route guard: existing `SessionDatabaseAdmin` dep alias.

```
┌──────────────────────────────────────────────────────────┐
│  ←  Manage marketing                                     │
│                                                          │
│  Members ─────────────────────────────────────────       │
│                                                          │
│   Readers                       [+ add user] [+ group]   │
│     alice@example.com                       [revoke]     │
│     group: data-team                        [revoke]     │
│                                                          │
│   Writers                       [+ add user] [+ group]   │
│     bob@example.com                         [revoke]     │
│                                                          │
│   Admins                        [+ add user] [+ group]   │
│     alice@example.com                       [revoke]     │
│                                                          │
│  Row policies ───────────────────────────────────────    │
│                                                          │
│   events.user_id  =  $alice          ON role X    [×]    │
│   orders.region   =  'eu'            ON role data-eu [×] │
│   [ + add row policy ]                                   │
│                                                          │
│  Audit ───────────────────────────────────────────────   │
│   2026-05-09  alice  granted READ to bob                 │
│   2026-05-08  alice  added row policy on events          │
│   [ view full audit → ]                                  │
│                                                          │
│  Danger ──────────────────────────────────────────────   │
│   [ delete database ]      (requires confirmation)       │
└──────────────────────────────────────────────────────────┘
```

Each `[+ add]` / `[revoke]` / `[×]` is a CSRF-protected POST/DELETE returning a fragment that morphs the relevant section. If the user loses admin between actions (another admin revoked them), the route returns the existing `auth/forbidden.html` fragment scoped to the tab.

### 3.5 `create_database` panel

A single-screen form: database name input + submit. On success, the panel re-renders as the new database's `manage` view (the creator becomes its admin via the existing `DatabaseCreatorSession.create_database`). On validation failure (reserved name, invalid identifier), the form re-renders with an inline error.

### 3.6 `admin_console` panel (`is_admin`)

Sub-tabs *within* the tab — a row of buttons that swap the panel below:

```
┌──────────────────────────────────────────────────────────┐
│  Org admin console                                       │
│                                                          │
│  [ Users ] [ Databases ] [ Row policies ] [ Audit ]      │
│  ─────────────────────────────────────────────────       │
│                                                          │
│   <selected sub-tab content>                             │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

- **Users** — all users with their group memberships and effective tiers across all databases. Action: `[reprovision]` (calls `AdminSession.reprovision_user`).
- **Databases** — all databases, with admin/writer/reader counts. Click a row → opens that DB's `manage` view in a new tab.
- **Row policies** — global view across all databases, filterable by database/role/table.
- **Audit** — `system.grants` browser with database/user/role filters and time range.

Sub-tab selection is a per-tab signal: `$tabs.AB12CD34.subtab = 'users'`. Pure client-side flip; sub-tab content fragments load lazily on first switch via `data-on:click="$tabs.AB12CD34.subtab='users'; @get('/feature/auth/AB12CD34/admin/users')"`.

### 3.7 Defense in depth

Three layers, in order of trust:

1. **Nav rendering** (presentation). The nav macro skips entries that fail their `visible` predicate, so unauthorized features don't appear in the menu.
2. **Intent gate** (gateway). `POST /api/tabs` looks up the requested intent's required capability via the feature's intent registry and checks it against `session.capabilities` before generating a `tab_id`. Returns 403 + a small inline error fragment otherwise.
3. **Per-route guard** (authoritative). Every route inside the feature uses an `Annotated` `Session*` dep (`SessionDatabaseAdmin`, `SessionAdmin`, …). This is the only level that *enforces*; (1) and (2) are UX. Existing pattern, no change.

Stale-cap handling: capabilities are snapshotted on the session at login and refreshed by the existing `_provision_on_login` hook. A user whose admin role is revoked mid-session continues to see the nav entries until next login but gets 403s on the routes — surfaced as inline error fragments.

---

## 4. Contribution registry

A small typed registry on `app.state.contributions`. At MVP only `nav` is implemented. New extension points get added one at a time when (and only when) a concrete cross-feature integration motivates one.

### 4.1 Types

```python
# src/iris/shell/contributions.py
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from iris.auth.rights import Capabilities

CapPredicate = Callable[[Capabilities], bool]
CapDerived   = Callable[[Capabilities], Any]


@dataclass(frozen=True, slots=True)
class TabIntent:
    """Open-a-tab descriptor: which feature, which intent, with what params."""
    feature: str
    intent: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NavEntry:
    label: str
    on_click: TabIntent | None = None        # None = pure parent (group label)
    icon: str | None = None
    visible: CapPredicate = lambda _: True
    badge:    CapDerived | None = None       # → str | None
    children: CapDerived | None = None       # → Sequence[NavEntry]


@dataclass(frozen=True, slots=True)
class NavGroup:
    label: str
    icon: str | None = None
    visible: CapPredicate = lambda _: True
    entries: Sequence[NavEntry] = ()


@dataclass(slots=True)
class NavRegistry:
    groups: list[NavGroup] = field(default_factory=list)
    def add(self, group: NavGroup) -> None: self.groups.append(group)


@dataclass(slots=True)
class Contributions:
    nav: NavRegistry = field(default_factory=NavRegistry)
    # Future (add only when a real integration needs it):
    # database_actions: ActionRegistry
    # table_actions:    ActionRegistry
    # result_actions:   ActionRegistry
    # settings_tabs:    SettingsTabRegistry
```

### 4.2 Discipline rule

**Do not add a new registry to `Contributions` until at least one feature has a concrete need to contribute and at least one feature has a concrete need to consume.** Every registry is permanent API surface; the cost compounds. This rule is added to `CLAUDE.md` as part of the migration plan.

### 4.3 Server-side rendering

A single Jinja macro walks `contribs.nav.groups`, evaluates `visible(session.capabilities)` per `NavGroup` and `NavEntry`, calls `children(...)` and `badge(...)` lazily, and emits the `<nav>` HTML. `on_click` becomes `data-on:click="@post('/api/tabs?feature={feature}&intent={intent}&...')"`. No JS, no client-side filtering.

---

## 5. File layout

```
src/iris/
├── __init__.py
├── app.py                       # build_app(): wires auth, ch, shell, features
├── middleware.py                # SecurityHeadersMiddleware (existing)
├── templates.py                 # shared Jinja loader; register_template_dir()
├── auth/                        # unchanged
├── clickhouse/                  # unchanged
├── static/
│   └── datastar.js              # global vendored asset (existing)
│
├── shell/                       # the integrated-app frame
│   ├── __init__.py
│   ├── install.py               # install(app) — Contributions + shell routes
│   ├── contributions.py         # Contributions, NavRegistry, NavEntry, …
│   ├── tabs.py                  # tab id generation, session.data['tabs'] helpers
│   ├── routes.py                # GET /, POST/DELETE/PATCH /api/tabs[/{id}]
│   ├── element_id.py            # el(tab_id, *parts) + Jinja global registration
│   ├── nav_render.py            # render nav HTML from contributions + capabilities
│   ├── intent_dispatch.py       # IntentHandler protocol; features register handlers
│   ├── templates/
│   │   ├── shell.html           # base layout: nav + tab strip + content panels
│   │   ├── _nav.html            # nav macro
│   │   ├── _tab_strip.html
│   │   ├── _tab_panel.html
│   │   └── _account_popover.html
│   └── static/
│       └── shell.css            # two-panel layout, collapsible nav, popover
│
└── features/
    ├── __init__.py
    └── authorization/
        ├── __init__.py
        ├── install.py           # nav contributions + intent handlers + router mount
        ├── routes.py            # APIRouter(prefix="/feature/auth")
        ├── intents.py           # one render function per intent
        ├── service.py           # business logic (no FastAPI imports)
        ├── templates/
        │   ├── my_access.html
        │   ├── manage.html
        │   ├── _members_section.html
        │   ├── _row_policies.html
        │   ├── create_database.html
        │   └── admin_console.html
        └── static/              # rare; most styling stays in shell.css
```

### 5.1 Conventions enforced by the file layout

1. **One feature = one directory** under `src/iris/features/<name>/`. The directory IS the contract.
2. **`install(app)` is the only public entry point** of a feature. `app.py` calls each feature's `install` in a fixed order (auth → clickhouse → shell → features). Order matters: features depend on `app.state.contributions` existing.
3. **Templates namespaced by feature/subsystem.** A feature's templates render as `<feature>/<name>.html`; the loader assembles a `ChoiceLoader` that includes each module's `templates/` dir. Naming collisions impossible because the path includes the namespace dir.
4. **Static namespaced by feature.** Each feature mounts at `/static/<feature>/` from its own `static/` dir.
5. **Routes namespaced by feature.** The feature's `APIRouter` has `prefix="/feature/<name>"`. All tab routes therefore land at `/feature/<name>/{tab_id}/...`.
6. **Authz via existing `Session*` aliases.** Features import the type they need; no new authz abstraction.
7. **No cross-feature imports.** Features may import `iris.auth`, `iris.clickhouse`, `iris.shell` — never another feature. Cross-feature integration goes through the contribution registry. (Soft rule for now; reconsidered if a real exception appears.)

### 5.2 Templates loader

```python
# src/iris/templates.py
from pathlib import Path
from fastapi.templating import Jinja2Templates

_dirs: list[Path] = []

def register_template_dir(path: Path) -> None:
    """Append a templates dir before init_templates() is called. Call from install()."""
    _dirs.append(path)

def init_templates() -> Jinja2Templates:
    """Build the loader once all install()s have registered their dirs."""
    return Jinja2Templates(directory=_dirs)
```

`build_app()` calls `init_templates()` after all `install()`s and stashes the result on `app.state.templates`. Each `install()` (shell + every feature + auth + clickhouse) calls `register_template_dir(Path(__file__).parent / "templates")` early in its body.

---

## 6. Testing pattern

Test layout mirrors the source tree:

```
tests/
├── shell/
│   ├── test_nav_render.py
│   ├── test_tabs.py
│   └── test_intent_dispatch.py
├── features/
│   └── authorization/
│       ├── test_my_access.py
│       ├── test_manage.py
│       ├── test_admin_console.py
│       └── test_nav_contributions.py
├── auth/                        # existing
├── clickhouse/                  # existing
└── conftest.py                  # shared fixtures
```

### 6.1 Conventions

- **Real CH testcontainer.** No DB mocks. Continues the existing rule from CLAUDE.md.
- **Capability-controlled session fixture** is the workhorse for feature tests:
  ```python
  @pytest.fixture
  def session_with(app, store):
      async def _make(**caps) -> tuple[str, dict]:
          # Build a Capabilities, mint a User, create a session row,
          # return (sid, cookies) for use with TestClient.
          ...
      return _make
  ```
  Tests then read like: `sid, cookies = await session_with(is_admin=True)` or `session_with(db_admin={"marketing"})`.

- **Three layers of authz tests per feature**, matching the three defense-in-depth layers:
  1. **Nav render**: hit `GET /` with a session of capabilities X, assert the right entries are present/absent in the rendered HTML.
  2. **Intent gate**: hit `POST /api/tabs` with `intent=admin_console` from a non-admin session, assert 403.
  3. **Per-route**: hit `/feature/auth/{tab_id}/manage/marketing` from a non-`db_admin` session, assert 403.

- **SSE assertions**: small helper `parse_sse(response) -> list[Event]` parses the `text/event-stream` response into `[(event_name, data_dict)]` so tests can assert the expected `datastar-patch-elements` / `datastar-patch-signals` events readably.

- **Per-test CH isolation**: continue using the existing `prefix` fixture (UUID-prefixed entity names).

- **Intent unit tests**: each intent has a pure render function — `def render_my_access(session) -> str` returns HTML. Unit-test those directly with a fake session before adding the SSE/route shell tests on top.

---

## 7. Migration plan

The current `src/iris/app.py` defines demo routes (`/`, `/api/greet`, `/api/clock`) and `templates/index.html`. These are retired.

One commit per step, each shipping passing tests at all three authz layers where applicable:

1. **Shell scaffold.** `src/iris/shell/` with `install(app)`, empty `Contributions`, tab routes, base template, two-panel CSS, account popover. Wire from `build_app`. Delete the demo routes (`/api/greet`, `/api/clock`) and `templates/index.html`. The old `templates/base.html` is moved into `shell/templates/shell.html` and expanded into the two-panel layout. Also writes `docs/frontend.md` covering the shell module surface, tab system, and Datastar conventions (peer to `docs/auth.md` and `docs/clickhouse.md`).
2. **Templates loader refactor.** `src/iris/templates.py` becomes a registry; each `install()` calls `register_template_dir(...)`; `build_app` calls `init_templates()` once after all installs. Existing `templates/auth/{forbidden,ldap_form}.html` move under `src/iris/auth/templates/auth/...` to fit the per-subsystem ownership rule. The `auth/` template prefix is preserved in references — no caller changes.
3. **Authorization feature scaffold.** `src/iris/features/authorization/` with `install(app)`, nav contributions, `intents.py` containing only the `my_access` handler, `manage.html` is stubbed as a placeholder.
4. **`manage` intent.** Full per-database management page: members section (grant/revoke), row policies section, audit section, danger zone. Routes guarded by `SessionDatabaseAdmin` (existing).
5. **`create_database` intent.** Single-screen form, gated by `SessionDatabaseCreator` (existing). Validation surfaced as inline error fragments.
6. **`admin_console` intent** with sub-tabs (Users / Databases / Row policies / Audit), gated by `SessionAdmin` (existing).
7. **CLAUDE.md update.** Add a "Frontend architecture" navigation section that links to `docs/frontend.md`. Add the contribution-registry discipline rule, the `iris.features.<name>` convention, the no-cross-feature-imports rule, and a Datastar-philosophy reminder.

What does NOT migrate yet, parked for their own future specs:
- Workbench, Dashboards, Ingestion features — empty directories appear under `src/iris/features/` only when work begins on each.
- Any persistence model for those features.
- Any contribution registry beyond `nav`.

---

## 8. Risks and tradeoffs

- **Datastar signal payload growth with many tabs.** Datastar sends all signals on every fetch by default; tens of tabs each with their own `$tabs.<id>.*` subtree could push payload size up. Mitigations available without architecture change: Datastar's `filterSignals` to send only the active tab's slice, and keeping per-tab signals minimal (any non-trivial state stays server-side). Re-evaluate if a real performance issue surfaces.
- **`session.data['tabs']` write amplification.** Every tab open / close / re-target writes the session row. Throughput is fine for human interaction (tens of writes per minute at most). The 32-tab cap also bounds row size.
- **Stale capabilities.** Mitigations documented in §3.7. Acceptable for MVP; an explicit "refresh capabilities" affordance can be added to the Account popover later if it becomes painful.
- **Children-rendering for very-long lists** (e.g., `Databases I admin` with 100 entries): rendered as a scrollable popover above 10 entries (§3.2). Re-evaluate if 1000-entry users appear.
- **Cross-feature integration without registries.** Until a second feature contributes/consumes, there is no `database_actions` registry — so e.g. dashboards' "Save chart" button cannot appear on the workbench results page. Acceptable: the registry shape is designed to make adding one a small change. Don't pre-build.
