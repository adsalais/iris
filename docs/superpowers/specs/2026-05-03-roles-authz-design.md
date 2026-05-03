# Role-Based Authorization for `iris`

**Date:** 2026-05-03
**Status:** Design — approved through brainstorming; pending user review of this document.
**Supersedes:** the `require_group(...)` API introduced in `2026-05-03-auth-design.md`. The session/provider/identity machinery from that spec is unchanged; only the authorization layer is replaced.

## Context

`iris` currently authorizes routes with `require_group("admins")`, which checks intersections against IdP group names exposed verbatim on `User.groups`. This couples application code to operator-managed identity-provider configuration: route guards reference external strings (`"ldap-admins"`, `"platform-team"`, etc.), and the same role under OAuth and LDAP requires identical group naming across both IdPs.

We want application code to reference **internal role names only** (e.g. `admin`, `writer`, `reader`). The mapping from internal role → external groups/usernames lives in a YAML configuration file outside the code, edited by operators without a redeploy. Roles support inheritance: declaring `admin includes writer includes reader` means a route that requires `reader` admits writers and admins as well.

## Requirements (locked in via brainstorming)

1. **Internal role names in code only.** Routes use `require_role("reader")`. They never reference IdP group names or usernames.
2. **External mapping in YAML.** A single YAML file defines roles and maps each to external groups and usernames. Path is provided by `AUTHZ_CONFIG_PATH` (required env var, fail-loud if unset).
3. **Inheritance via `includes`.** Each role can declare other roles it transitively includes. `admin: { includes: [writer] }` plus `writer: { includes: [reader] }` means `admin` implicitly grants `writer` and `reader`.
4. **Single required role per route.** `require_role("reader")` admits any user whose effective role set contains `reader` (directly or via inheritance).
5. **Auth-only routes are unchanged.** `CurrentUser` (auth required, no role check) and `OptionalCurrentUser` (no auth required) keep their existing semantics.
6. **Live reload.** Editing the YAML at runtime takes effect on the next request — no restart, no waiting for sessions to roll over.
7. **Robustness against bad edits.** A YAML file that becomes invalid (syntax error, schema error, cycle, undefined include) does not break running requests: the last successfully loaded mapping continues to serve until the file is fixed.
8. **`require_group` is deleted.** The previous API is removed outright. There are no production callers (only tests), and keeping a parallel API would invite reintroducing external names in code.

## Out of scope (explicit non-goals)

- Persistent or distributed role-mapping storage (Redis, DB). YAML on disk is sufficient at this deploy scale.
- Per-resource (record-level) authorization. Routes still gate on a single role; "Alice can edit *this* document" is application logic on top, not part of the authz layer.
- Multiple role-mapping files merged at load time. Single file only.
- Hot reload via file watcher (inotify, watchfiles). Mtime-on-request is sufficient at this scale.
- Validation of group names against the live IdP. Group lists are dynamic; the mapping cannot statically prove a referenced group exists in Keycloak/LDAP.
- A UI for editing roles. YAML edited by hand or by configuration management.
- Any-of-roles dep (`require_any_role(...)`). Not needed for the spec; can be added later if a real use case appears.

---

## Architecture

### Module map

```
src/iris/auth/authz/
├── __init__.py          # re-exports require_role, CurrentRoles, RoleMapping, RoleMappingLoader
├── config.py            # AuthzSettings.from_env() — reads AUTHZ_CONFIG_PATH (required)
├── mapping.py           # RoleMapping dataclass; parse() + transitive-closure compute + cycle detection
├── loader.py            # RoleMappingLoader: mtime-cached loader with last-good fallback
└── deps.py              # require_role(name) factory; CurrentRoles dep; _resolve_roles helper
```

`src/iris/auth/__init__.py` adds `require_role` and `CurrentRoles` to the public re-exports. The existing `require_group` symbol is removed from both `deps.py` and the package `__all__`.

The subpackage is self-contained: `loader.py` handles file I/O, `mapping.py` handles graph algorithms, `deps.py` handles FastAPI wiring. Each is independently testable.

### Identity model changes

`User` (`src/iris/auth/identity.py`) gains a `username` field used for YAML matching. `UserSession` is unchanged — roles are not cached on the session.

```python
@dataclass(frozen=True, slots=True)
class User:
    subject: str                    # stable IdP id (OIDC sub / LDAP DN / "mock:<name>")
    username: str                   # NEW: human-friendly stable id used for YAML matching
    display_name: str
    groups: tuple[str, ...]         # external IdP groups, verbatim — kept for whoami/templates only
```

Per-provider `username` source:

| Provider | `username` source | Fallback |
|---|---|---|
| OAuth (`oauth.py`) | `claims["preferred_username"]` | `claims["sub"]` if `preferred_username` is absent |
| LDAP (`ldap.py`) | the username substituted into `LDAP_BIND_DN_TEMPLATE` (captured before bind) | n/a — always available |
| Mock (`mock.py`) | `MockSettings.username` (already configured) | n/a |

The OAuth fallback to `sub` means a deploy without `preferred_username` still functions, at the cost of operators having to write opaque IdP UUIDs into `users:` lists. This will be documented in CLAUDE.md.

### YAML schema

```yaml
roles:
  reader:
    groups: []
    users: []
  writer:
    groups: ["editors"]
    users: []
    includes: ["reader"]
  admin:
    groups: ["ldap-admins", "platform-team"]
    users: ["alice"]
    includes: ["writer"]
```

**Structural rules** (enforced at load, fail-loud with line numbers):

- Top-level: exactly one key, `roles:`. Any other top-level key is rejected.
- Each role entry's keys are restricted to `{groups, users, includes}`. Unknown keys reject.
- `groups`, `users`, `includes` all default to `[]` if omitted.
- All three are lists of strings.
- Role names: case-sensitive, non-empty, matching `[a-zA-Z0-9_-]+`. No spaces, no colons, no slashes.
- Every name in `includes` must reference a defined role. Undefined includes reject.
- The `includes` graph must be a DAG. Cycles (`admin → writer → admin`) reject.
- Duplicate top-level role keys reject (custom `yaml.SafeLoader` subclass; PyYAML's default silently overwrites).
- Group names: opaque, case-sensitive, exact-match against `User.groups` entries.
- Usernames: case-insensitive match against `User.username` (lowercased on both sides during the comparison; the original casing in the YAML is preserved for error messages).

**Empty `users` and empty `groups` for a role are valid** — useful as a placeholder while operators decide membership.

**Validation library:** hand-rolled with `pyyaml` + Python stdlib. Pydantic is overkill for ~30 lines of structural checks; a small parser keeps the dep surface minimal and gives precise error messages.

**Transitive closure:** computed once per successful load. The `RoleMapping` exposes `closure: dict[str, frozenset[str]]` mapping each role to its full implied role set (`admin → {admin, writer, reader}`). Per-request lookup is then a small number of dict accesses and frozenset operations.

### `RoleMapping` value type

```python
@dataclass(frozen=True, slots=True)
class RoleDef:
    name: str
    groups: frozenset[str]            # exact-match set against User.groups
    users_lower: frozenset[str]       # lowercased usernames for case-insensitive match
    includes: tuple[str, ...]         # direct includes (closure stored separately)

@dataclass(frozen=True, slots=True)
class RoleMapping:
    roles: dict[str, RoleDef]                    # by role name
    closure: dict[str, frozenset[str]]           # role name → full transitive set incl. self
```

`mapping.parse(yaml_text: str) -> RoleMapping` returns the value type or raises `RoleMappingError` (subclass of `ValueError`) with a precise message including the offending YAML line number where derivable.

### `RoleMappingLoader`

Single instance per app, stored on `app.state.authz_loader`.

```python
class RoleMappingLoader:
    def __init__(self, path: Path) -> None: ...
    def get(self) -> RoleMapping:
        """Return the current mapping, reloading from disk if mtime changed.
        On reload failure after at least one successful load, return the
        last good mapping and log at ERROR.
        """
```

Flow inside `get()`:

1. `os.stat(path).st_mtime_ns` — sub-millisecond.
2. If unchanged from last seen mtime, return the cached `RoleMapping`.
3. If changed, acquire `threading.Lock` (held only across the reload), re-stat (double-check inside the lock to avoid duplicate work under concurrency), then attempt parse:
   - Success → store new mapping, update mtime, return it.
   - Failure (file missing, YAML syntax error, schema/cycle/undefined-include error):
     - If a previous good mapping exists in memory: log `ERROR` with path + reason, return the cached mapping.
     - If this is the *first* load (no cached mapping): re-raise — the caller (typically `install(app)`) propagates it as a startup failure.

`install(app)` calls `loader.get()` once eagerly so a bad initial YAML stops the app from booting (consistent with the rest of the auth config style: fail-loud at startup, never silent).

### `require_role` and `CurrentRoles`

```python
# deps.py
def _resolve_roles(user: User, mapping: RoleMapping) -> frozenset[str]:
    base: set[str] = set()
    for role_name, role_def in mapping.roles.items():
        if user.username.lower() in role_def.users_lower:
            base.add(role_name)
        elif role_def.groups & set(user.groups):
            base.add(role_name)
    effective: set[str] = set()
    for r in base:
        effective |= mapping.closure[r]
    return frozenset(effective)


async def _current_mapping(request: Request) -> RoleMapping:
    return request.app.state.authz_loader.get()


_CurrentMapping = Annotated[RoleMapping, Depends(_current_mapping)]


async def _current_roles(mapping: _CurrentMapping, user: CurrentUser) -> frozenset[str]:
    return _resolve_roles(user, mapping)


CurrentRoles = Annotated[frozenset[str], Depends(_current_roles)]


def require_role(role: str):
    async def _check(
        mapping: _CurrentMapping,
        roles: CurrentRoles,
        user: CurrentUser,
    ) -> User:
        if role not in mapping.roles:
            raise AuthorizationMisconfigured(role)
        if role not in roles:
            raise AuthForbidden(needed=(role,), have=tuple(sorted(roles)))
        return user
    return _check
```

`require_role` returns the `User` so route handlers can keep the `user: User = Depends(require_role("reader"))` pattern they already use with `require_group`. `CurrentRoles` is a separate dep for routes/templates that need to display the user's effective roles without gating on a specific one (e.g. `/api/whoami`).

`_current_mapping` is its own dep so FastAPI's per-request dep cache resolves the loader exactly once per request, even when both `require_role(...)` and `CurrentRoles` are present on the same route. The mtime stat plus the cache check is sub-millisecond, but pulling it through one dep keeps the request profile clean and avoids any chance of the YAML being re-read mid-request.

### Errors

`exceptions.py` is extended with one new type. Existing exception handlers cover the rest.

| Exception | When raised | HTTP response |
|---|---|---|
| `AuthRequired` (existing) | No valid session | 302 to `/login` (HTML) or 401 (API) |
| `AuthForbidden` (existing) | Effective roles do not contain the required role | 403 with `needed`/`have` shown on the page |
| `AuthorizationMisconfigured` (**new**, subclass of `RuntimeError`) | Route requires a role that is not defined in the current YAML | 500 with a generic message; the missing role name goes to the logs only — 500s must not leak config details |

`AuthorizationMisconfigured` is treated as a deploy bug, not a permission denial. Silently returning 403 for an unknown required role would mask operator typos (`require_role("reder")` would deny everyone forever without a signal).

### Install wiring

`src/iris/auth/__init__.py`:

```python
def install(app: FastAPI) -> None:
    settings = AuthSettings.from_env()
    authz_settings = AuthzSettings.from_env()                 # NEW: required env var, fail-loud
    loader = RoleMappingLoader(authz_settings.config_path)
    loader.get()                                              # eager initial load — boot fails if YAML invalid
    app.state.authz_loader = loader
    # ... existing session store, provider, exception handlers, router ...
```

---

## Edge cases

| Case | Behavior |
|---|---|
| `AUTHZ_CONFIG_PATH` unset | Startup error (matches existing fail-loud config style) |
| Path set, file missing at boot | Startup error |
| Path set, YAML syntax error at boot | Startup error |
| Path set, schema error at boot (unknown key, cycle, undefined include, duplicate role key) | Startup error with line number + reason |
| File edited at runtime, new content valid | Next request picks up new mapping (mtime change → reload) |
| File edited at runtime, new content invalid | `ERROR` log; last good mapping stays in effect; subsequent requests keep working |
| File deleted at runtime | Treated as invalid content — `ERROR` log, last good mapping stays in effect |
| User authenticated but matches no role | `CurrentUser` succeeds; `CurrentRoles` returns empty `frozenset`; `require_role(any)` raises `AuthForbidden` |
| Route requires a role that is not defined in the YAML | `AuthorizationMisconfigured` → 500 |
| Two roles in `users:` resolve the same person | No-op; closure dedup handles it |
| Group in `groups:` doesn't exist in any IdP | No error — that role just has no group-based members |
| Username casing differs between YAML and IdP | Match succeeds (case-insensitive comparison on both sides) |
| OAuth IdP doesn't issue `preferred_username` | `username` falls back to `sub` (UUID); operator must put the UUID in `users:` if they want explicit user matching |

---

## Test plan

New test files under `tests/auth/authz/`:

- `test_mapping.py` — parser unit tests:
  - Valid file → parses with expected `RoleDef`s and closure.
  - Empty `roles:` block → parses to empty mapping.
  - Unknown top-level key → rejects.
  - Unknown role-entry key → rejects.
  - Role name with disallowed characters → rejects.
  - Undefined include → rejects.
  - Cycle in `includes` (direct and indirect) → rejects.
  - Duplicate top-level role key → rejects (verifies the custom loader, since PyYAML's default would silently overwrite).
  - Closure correctness for chains (`admin → writer → reader`) and diamonds (`admin → writer, admin → reviewer, writer → reader, reviewer → reader`).
- `test_loader.py` — uses `tmp_path` to write real YAML files:
  - Initial load returns parsed mapping.
  - Cached read after no mtime change does not re-parse (verified by mutating the file content without touching mtime, or by spying on the parse function).
  - Mtime change triggers reload and returns new mapping.
  - Invalid edit after a good initial load: `get()` returns last good mapping; an `ERROR` is logged.
  - File deleted after good initial load: same fallback behavior.
  - First load failure: `get()` raises (no last-good to fall back to).
  - Concurrent `get()` calls during a slow parse: only one parse runs (via the lock).
- `test_deps.py` — wires `require_role` and `CurrentRoles` into a tiny FastAPI app with a fixture YAML:
  - User in admin's `groups` reaches a `require_role("reader")` route (transitive).
  - User in writer's `users` reaches a `require_role("reader")` route (case-insensitive match).
  - User matching nothing → 403 on `require_role("reader")`.
  - Route requires undefined role `"super_admin"` → 500 (with the missing-role name in logs, not the response body).
  - `CurrentRoles` returns the full effective set including transitively-included roles.

Modifications to existing tests:

- `tests/auth/test_deps.py` — `require_group_admits_member` / `require_group_rejects_non_member` rewritten as the `require_role` equivalents.
- `tests/auth/test_error_pages.py` — replace `require_group("admins")` with `require_role("admin")`; the test app's lifespan/install needs the fixture YAML.
- `tests/conftest.py` — write a minimal `authz.yaml` to a session-scoped `tmp_path` (or to a fixed path under `tests/`) once at module scope, then `os.environ.setdefault("AUTHZ_CONFIG_PATH", str(path))` so `iris.app:app` can be imported by the suite without per-test arrangement. The fixture YAML maps the mock user's `MOCK_GROUPS` value into one or more roles so `authed_client` continues to work for feature tests of role-gated routes.

The fixture YAML written by `conftest.py` should contain at least:

```yaml
roles:
  reader:
    groups: []
    users: []
  writer:
    groups: []
    users: []
    includes: ["reader"]
  admin:
    groups: ["admins"]      # matches MOCK_GROUPS=admins,users from conftest
    users: []
    includes: ["writer"]
```

---

## Migration steps

1. Add `pyyaml` to `[project.dependencies]` in `pyproject.toml`. Run `uv sync`.
2. Add `username` field to `User` in `identity.py`. Update each provider (`oauth.py`, `ldap.py`, `mock.py`) to populate it. Update any existing `User(...)` construction in tests.
3. Create `src/iris/auth/authz/` with `config.py`, `mapping.py`, `loader.py`, `deps.py`, `__init__.py`.
4. Add `AuthorizationMisconfigured` to `exceptions.py` and register a 500 handler in `install_exception_handlers`.
5. Wire `AuthzSettings.from_env()` and `RoleMappingLoader` into `install(app)` in `auth/__init__.py`.
6. Re-export `require_role` and `CurrentRoles` from `iris.auth`. Remove `require_group` from `deps.py` and from the package re-exports.
7. Rewrite the affected tests; add the new `tests/auth/authz/` suite; update `conftest.py` to write the fixture YAML and set `AUTHZ_CONFIG_PATH`.
8. Update CLAUDE.md: replace the `require_group` example, add an "Authorization (roles)" subsection covering the YAML schema, the env var, the case-insensitive username matching, the OAuth `preferred_username` fallback, the live-reload semantics, and the last-good-on-bad-reload behavior. Update the Authentication module map to include the `authz/` subpackage.

## Documentation deltas (CLAUDE.md)

A new "Authorization (roles)" subsection under "Authentication" will cover:

- The internal-role-name principle (no IdP names in code).
- The YAML schema and an example file.
- The required `AUTHZ_CONFIG_PATH` env var.
- Live-reload semantics: mtime per request, last-good fallback on bad reload, eager validation at boot.
- Username matching: case-insensitive against `User.username`; OAuth fallback to `sub` when `preferred_username` is absent.
- The `require_role` / `CurrentRoles` API; the deletion of `require_group`.
- The `AuthorizationMisconfigured` → 500 behavior for routes that name an undefined role.

The "Open security follow-ups (v1.1)" section gets one addition: the per-request mtime stat is acceptable at ≤20-user scale; for higher request volumes, swap to a file watcher or event-driven invalidation.
