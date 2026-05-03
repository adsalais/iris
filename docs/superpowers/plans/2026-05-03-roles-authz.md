# Role-Based Authorization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `require_group(...)` with a role-based authorization layer that maps internal role names (`admin`, `writer`, `reader`) to external IdP groups/usernames via a YAML file (`AUTHZ_CONFIG_PATH`), supporting `includes`-based inheritance, with per-request mtime-cached reload and last-good fallback on bad edits.

**Architecture:** New `iris.auth.authz` subpackage with four modules — `config` (env), `mapping` (pure-logic parser + closure), `loader` (file I/O + mtime cache + last-good fallback), `deps` (`require_role`, `CurrentRoles`, `_current_mapping`). `User` gains a `username` field used for case-insensitive YAML matching. The existing `require_group` API is deleted (no production callers).

**Tech Stack:** Python 3.13, `pyyaml` (new), FastAPI, pytest (existing).

**Spec:** `docs/superpowers/specs/2026-05-03-roles-authz-design.md` (committed).

---

## Pre-flight

Tasks assume:
- Working tree clean.
- `uv run pytest` is green before starting.
- All commits in this plan use `git add <listed-paths>` — never `git add -A`.

Run a baseline:

```bash
uv run pytest
```

Expected: all tests pass. Note the count for sanity-checking later tasks.

---

## Task 1: Add `pyyaml` runtime dep

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add the dep**

```bash
uv add pyyaml
```

Expected: `pyproject.toml` `dependencies` array gains `pyyaml>=...`. `uv.lock` updated.

- [ ] **Step 2: Confirm import works**

```bash
uv run python -c "import yaml; print(yaml.__version__)"
```

Expected: a version string prints (e.g., `6.0.x`).

- [ ] **Step 3: Run full test suite**

```bash
uv run pytest
```

Expected: same green baseline.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): add pyyaml for role-mapping YAML loader"
```

---

## Task 2: Add `username` field to `User` and populate it from each provider

**Why:** Every authz lookup matches against `User.username`. Adding the field early lets later tasks reference it freely. All existing call sites must be updated in the same commit so the suite stays green.

**Files:**
- Modify: `src/iris/auth/identity.py` — add field
- Modify: `src/iris/auth/providers/mock.py:37-41` — populate
- Modify: `src/iris/auth/providers/ldap.py:70` — populate
- Modify: `src/iris/auth/providers/oauth.py:205-215` — populate with fallback
- Modify: `tests/conftest.py:32` — pass `username="alice"`
- Modify: `tests/auth/test_deps.py:60-65` — pass `username` in `_seed`
- Modify: `tests/auth/test_session_store.py:12,114` — pass `username` in two `User(...)` calls

- [ ] **Step 1: Write a failing identity test**

Append to `tests/auth/test_identity.py` (create if missing):

```python
from iris.auth.identity import User


def test_user_carries_username_separate_from_subject():
    u = User(
        subject="mock:alice",
        username="alice",
        display_name="Alice",
        groups=("admins",),
    )
    assert u.username == "alice"
    assert u.subject == "mock:alice"
```

- [ ] **Step 2: Run it to verify failure**

```bash
uv run pytest tests/auth/test_identity.py -v
```

Expected: FAIL — `TypeError: User.__init__() got an unexpected keyword argument 'username'`.

- [ ] **Step 3: Add `username` to `User`**

Update `src/iris/auth/identity.py` so `User` reads:

```python
@dataclass(frozen=True, slots=True)
class User:
    subject: str
    username: str
    display_name: str
    groups: tuple[str, ...]
```

- [ ] **Step 4: Update `MockProvider` to populate `username`**

In `src/iris/auth/providers/mock.py`, the `authenticate(...)` return becomes:

```python
return User(
    subject=f"mock:{self._settings.username}",
    username=self._settings.username,
    display_name=self._settings.display_name,
    groups=self._settings.groups,
)
```

- [ ] **Step 5: Update `LDAPProvider` to populate `username`**

In `src/iris/auth/providers/ldap.py`, the `authenticate(...)` return (line 70) becomes:

```python
return User(
    subject=bind_dn,
    username=username,
    display_name=display_name,
    groups=tuple(groups),
)
```

(The `username` parameter is already in scope — it's the function argument.)

- [ ] **Step 6: Update `OAuthProvider` to populate `username` with `sub` fallback**

In `src/iris/auth/providers/oauth.py`, replace `_user_from_claims` (lines 205-215) with:

```python
def _user_from_claims(self, claims: dict) -> User:
    groups = tuple(claims.get("groups") or ())
    if not groups:
        logger.warning(
            "auth: OAuth userinfo had no `groups` claim — check IdP client mapper"
        )
    sub = str(claims["sub"])
    username = str(claims.get("preferred_username") or sub)
    return User(
        subject=sub,
        username=username,
        display_name=str(claims.get("name") or username),
        groups=groups,
    )
```

- [ ] **Step 7: Update `tests/conftest.py:32` `authed_client` User construction**

Change:

```python
user = User(subject="mock:alice", display_name="Alice", groups=("admins", "users"))
```

to:

```python
user = User(subject="mock:alice", username="alice", display_name="Alice", groups=("admins", "users"))
```

- [ ] **Step 8: Update `tests/auth/test_deps.py:60-65` `_seed` helper**

Change the `User(...)` call in `_seed` to:

```python
user = User(
    subject=overrides.get("subject", "alice"),
    username=overrides.get("username", overrides.get("subject", "alice")),
    display_name=overrides.get("display_name", "Alice"),
    groups=overrides.get("groups", ("admins",)),
)
```

(Default `username` to `subject` so existing tests that pass `subject="bob"` get `username="bob"` automatically.)

- [ ] **Step 9: Update `tests/auth/test_session_store.py` User constructions**

Both calls (lines 12 and 114) become, respectively:

```python
return User(subject="alice", username="alice", display_name="Alice", groups=("admins",))
```

```python
other = User(subject="bob", username="bob", display_name="Bob", groups=())
```

- [ ] **Step 10: Run full suite**

```bash
uv run pytest
```

Expected: same green baseline (existing tests + the new `test_identity.py`).

- [ ] **Step 11: Commit**

```bash
git add src/iris/auth/identity.py \
        src/iris/auth/providers/mock.py \
        src/iris/auth/providers/ldap.py \
        src/iris/auth/providers/oauth.py \
        tests/conftest.py \
        tests/auth/test_deps.py \
        tests/auth/test_session_store.py \
        tests/auth/test_identity.py
git commit -m "feat(auth): add User.username field, populate from each provider"
```

---

## Task 3: Build `mapping.py` — pure-logic parser, validation, closure

**Why:** Pure data transformation, no I/O. Easiest to test in isolation. Once this is locked, the loader (Task 5) is just file glue.

**Files:**
- Create: `src/iris/auth/authz/__init__.py` (empty for now; populated by Task 12)
- Create: `src/iris/auth/authz/mapping.py`
- Create: `tests/auth/authz/test_mapping.py`

- [ ] **Step 1: Create the package directory and write the failing test file**

Create `src/iris/auth/authz/__init__.py` as an empty file:

```bash
mkdir -p src/iris/auth/authz
touch src/iris/auth/authz/__init__.py
```

Then write `tests/auth/authz/test_mapping.py`:

```python
import pytest

from iris.auth.authz.mapping import RoleMapping, RoleMappingError, parse


def test_parses_minimal_valid_file():
    text = """
roles:
  reader:
    groups: []
    users: []
"""
    m = parse(text)
    assert isinstance(m, RoleMapping)
    assert set(m.roles.keys()) == {"reader"}
    assert m.closure["reader"] == frozenset({"reader"})


def test_omitted_lists_default_to_empty():
    text = """
roles:
  reader: {}
"""
    m = parse(text)
    assert m.roles["reader"].groups == frozenset()
    assert m.roles["reader"].users_lower == frozenset()
    assert m.roles["reader"].includes == ()


def test_includes_creates_transitive_closure():
    text = """
roles:
  reader: {}
  writer:
    includes: [reader]
  admin:
    includes: [writer]
"""
    m = parse(text)
    assert m.closure["reader"] == frozenset({"reader"})
    assert m.closure["writer"] == frozenset({"reader", "writer"})
    assert m.closure["admin"] == frozenset({"reader", "writer", "admin"})


def test_diamond_inheritance_resolves_correctly():
    text = """
roles:
  reader: {}
  writer:
    includes: [reader]
  reviewer:
    includes: [reader]
  admin:
    includes: [writer, reviewer]
"""
    m = parse(text)
    assert m.closure["admin"] == frozenset({"reader", "writer", "reviewer", "admin"})


def test_users_are_lowercased_for_matching():
    text = """
roles:
  admin:
    users: ["Alice", "BOB"]
"""
    m = parse(text)
    assert m.roles["admin"].users_lower == frozenset({"alice", "bob"})


def test_groups_remain_case_sensitive():
    text = """
roles:
  admin:
    groups: ["LDAP-Admins", "platform-team"]
"""
    m = parse(text)
    assert m.roles["admin"].groups == frozenset({"LDAP-Admins", "platform-team"})


def test_rejects_unknown_top_level_key():
    text = """
roles:
  reader: {}
extra: stuff
"""
    with pytest.raises(RoleMappingError, match="unknown top-level key"):
        parse(text)


def test_rejects_missing_top_level_roles_key():
    with pytest.raises(RoleMappingError, match="missing required key 'roles'"):
        parse("other: 1\n")


def test_rejects_unknown_role_entry_key():
    text = """
roles:
  reader:
    extras: []
"""
    with pytest.raises(RoleMappingError, match="unknown key 'extras'"):
        parse(text)


def test_rejects_role_name_with_disallowed_chars():
    text = """
roles:
  "bad name":
    groups: []
"""
    with pytest.raises(RoleMappingError, match="invalid role name"):
        parse(text)


def test_rejects_undefined_include():
    text = """
roles:
  writer:
    includes: [reader]
"""
    with pytest.raises(RoleMappingError, match="undefined role 'reader'"):
        parse(text)


def test_rejects_direct_cycle():
    text = """
roles:
  a:
    includes: [b]
  b:
    includes: [a]
"""
    with pytest.raises(RoleMappingError, match="cycle"):
        parse(text)


def test_rejects_self_cycle():
    text = """
roles:
  a:
    includes: [a]
"""
    with pytest.raises(RoleMappingError, match="cycle"):
        parse(text)


def test_rejects_indirect_cycle():
    text = """
roles:
  a:
    includes: [b]
  b:
    includes: [c]
  c:
    includes: [a]
"""
    with pytest.raises(RoleMappingError, match="cycle"):
        parse(text)


def test_rejects_duplicate_role_keys():
    text = """
roles:
  reader:
    groups: []
  reader:
    users: []
"""
    with pytest.raises(RoleMappingError, match="duplicate"):
        parse(text)


def test_rejects_non_list_groups():
    text = """
roles:
  reader:
    groups: "not-a-list"
"""
    with pytest.raises(RoleMappingError, match="must be a list"):
        parse(text)


def test_rejects_non_string_in_groups():
    text = """
roles:
  reader:
    groups: [123]
"""
    with pytest.raises(RoleMappingError, match="must be a string"):
        parse(text)


def test_empty_roles_block_parses_to_empty_mapping():
    text = "roles: {}\n"
    m = parse(text)
    assert m.roles == {}
    assert m.closure == {}


def test_yaml_syntax_error_raised_as_role_mapping_error():
    text = "roles:\n  - this is: malformed\n   indent: bad\n"
    with pytest.raises(RoleMappingError):
        parse(text)
```

- [ ] **Step 2: Run tests — expect collection failure (module not found)**

```bash
uv run pytest tests/auth/authz/test_mapping.py -v
```

Expected: collection error — `ModuleNotFoundError: No module named 'iris.auth.authz.mapping'`.

- [ ] **Step 3: Implement `mapping.py`**

Write `src/iris/auth/authz/mapping.py`:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

_ROLE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_ALLOWED_ROLE_KEYS = frozenset({"groups", "users", "includes"})


class RoleMappingError(ValueError):
    """Raised when a role mapping YAML file fails to load or validate."""


@dataclass(frozen=True, slots=True)
class RoleDef:
    name: str
    groups: frozenset[str]
    users_lower: frozenset[str]
    includes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoleMapping:
    roles: dict[str, RoleDef]
    closure: dict[str, frozenset[str]]


class _NoDuplicatesSafeLoader(yaml.SafeLoader):
    """SafeLoader subclass that rejects duplicate mapping keys.

    PyYAML's default behavior silently overwrites earlier occurrences,
    which would mask operator typos like two `reader:` blocks.
    """


def _construct_mapping_no_dupes(loader: yaml.Loader, node: yaml.MappingNode) -> dict:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise RoleMappingError(
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_NoDuplicatesSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_no_dupes,
)


def parse(text: str) -> RoleMapping:
    try:
        doc = yaml.load(text, Loader=_NoDuplicatesSafeLoader)
    except yaml.YAMLError as exc:
        raise RoleMappingError(f"YAML syntax error: {exc}") from exc
    except RoleMappingError:
        raise

    if not isinstance(doc, dict):
        raise RoleMappingError("file must contain a top-level mapping")
    if "roles" not in doc:
        raise RoleMappingError("missing required key 'roles'")
    extra = set(doc) - {"roles"}
    if extra:
        raise RoleMappingError(f"unknown top-level key(s): {sorted(extra)}")

    roles_doc = doc["roles"]
    if roles_doc is None:
        roles_doc = {}
    if not isinstance(roles_doc, dict):
        raise RoleMappingError("'roles' must be a mapping")

    roles: dict[str, RoleDef] = {}
    for name, body in roles_doc.items():
        if not isinstance(name, str) or not _ROLE_NAME_RE.fullmatch(name):
            raise RoleMappingError(f"invalid role name {name!r}")
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise RoleMappingError(f"role {name!r}: body must be a mapping")
        unknown = set(body) - _ALLOWED_ROLE_KEYS
        if unknown:
            raise RoleMappingError(
                f"role {name!r}: unknown key(s) {sorted(unknown)}"
            )

        groups = _coerce_string_list(body.get("groups", []), where=f"role {name!r}: groups")
        users = _coerce_string_list(body.get("users", []), where=f"role {name!r}: users")
        includes = _coerce_string_list(
            body.get("includes", []), where=f"role {name!r}: includes"
        )

        roles[name] = RoleDef(
            name=name,
            groups=frozenset(groups),
            users_lower=frozenset(u.lower() for u in users),
            includes=tuple(includes),
        )

    for role in roles.values():
        for inc in role.includes:
            if inc not in roles:
                raise RoleMappingError(
                    f"role {role.name!r}: includes undefined role {inc!r}"
                )

    closure = _compute_closure(roles)
    return RoleMapping(roles=roles, closure=closure)


def _coerce_string_list(value: Any, *, where: str) -> list[str]:
    if not isinstance(value, list):
        raise RoleMappingError(f"{where}: must be a list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RoleMappingError(f"{where}: each entry must be a string, got {type(item).__name__}")
        out.append(item)
    return out


def _compute_closure(roles: dict[str, RoleDef]) -> dict[str, frozenset[str]]:
    closure: dict[str, frozenset[str]] = {}
    visiting: set[str] = set()

    def visit(name: str) -> frozenset[str]:
        if name in closure:
            return closure[name]
        if name in visiting:
            raise RoleMappingError(f"cycle detected involving role {name!r}")
        visiting.add(name)
        result = {name}
        for inc in roles[name].includes:
            result |= visit(inc)
        visiting.remove(name)
        frozen = frozenset(result)
        closure[name] = frozen
        return frozen

    for name in roles:
        visit(name)
    return closure
```

- [ ] **Step 4: Run tests — expect all pass**

```bash
uv run pytest tests/auth/authz/test_mapping.py -v
```

Expected: all `test_mapping.py` tests pass.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/iris/auth/authz/__init__.py src/iris/auth/authz/mapping.py tests/auth/authz/test_mapping.py
git commit -m "feat(authz): add YAML role-mapping parser with cycle detection and closure"
```

---

## Task 4: Build `config.py` — `AuthzSettings.from_env()`

**Files:**
- Create: `src/iris/auth/authz/config.py`
- Create: `tests/auth/authz/test_authz_config.py`

(Filename uses the `authz_` prefix because the test suite's `--import-mode=importlib` requires unique basenames across the whole `tests/` tree, and `tests/auth/test_config.py` already exists.)

- [ ] **Step 1: Write failing test**

Write `tests/auth/authz/test_authz_config.py`:

```python
from pathlib import Path

import pytest

from iris.auth.authz.config import AuthzSettings


def test_from_env_reads_path(monkeypatch, tmp_path):
    p = tmp_path / "authz.yaml"
    p.write_text("roles: {}\n")
    monkeypatch.setenv("AUTHZ_CONFIG_PATH", str(p))
    s = AuthzSettings.from_env()
    assert s.config_path == p
    assert isinstance(s.config_path, Path)


def test_from_env_rejects_missing_var(monkeypatch):
    monkeypatch.delenv("AUTHZ_CONFIG_PATH", raising=False)
    with pytest.raises(ValueError, match="AUTHZ_CONFIG_PATH"):
        AuthzSettings.from_env()


def test_from_env_rejects_empty_var(monkeypatch):
    monkeypatch.setenv("AUTHZ_CONFIG_PATH", "   ")
    with pytest.raises(ValueError, match="AUTHZ_CONFIG_PATH"):
        AuthzSettings.from_env()
```

- [ ] **Step 2: Run — expect collection failure**

```bash
uv run pytest tests/auth/authz/test_authz_config.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `config.py`**

Write `src/iris/auth/authz/config.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AuthzSettings:
    config_path: Path

    @classmethod
    def from_env(cls) -> "AuthzSettings":
        raw = os.environ.get("AUTHZ_CONFIG_PATH", "").strip()
        if not raw:
            raise ValueError("Missing required env var: AUTHZ_CONFIG_PATH")
        return cls(config_path=Path(raw))
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/auth/authz/test_authz_config.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/iris/auth/authz/config.py tests/auth/authz/test_authz_config.py
git commit -m "feat(authz): add AuthzSettings env loader (AUTHZ_CONFIG_PATH)"
```

---

## Task 5: Build `loader.py` — `RoleMappingLoader` with mtime cache + last-good

**Files:**
- Create: `src/iris/auth/authz/loader.py`
- Create: `tests/auth/authz/test_loader.py`

- [ ] **Step 1: Write failing tests**

Write `tests/auth/authz/test_loader.py`:

```python
import logging
import time

import pytest

from iris.auth.authz.loader import RoleMappingLoader
from iris.auth.authz.mapping import RoleMapping, RoleMappingError


_VALID = """
roles:
  reader:
    groups: ["readers"]
"""

_VALID_2 = """
roles:
  writer:
    groups: ["writers"]
"""


def _write(path, text):
    """Write text and bump mtime by 1s to ensure st_mtime_ns changes."""
    path.write_text(text)
    # On some filesystems (e.g., older ext4 without nanosecond precision)
    # consecutive writes within the same second can leave mtime unchanged.
    # Bump it explicitly.
    new_t = time.time() + 1
    import os
    os.utime(path, (new_t, new_t))


def test_initial_load_returns_parsed_mapping(tmp_path):
    p = tmp_path / "authz.yaml"
    p.write_text(_VALID)
    loader = RoleMappingLoader(p)
    m = loader.get()
    assert isinstance(m, RoleMapping)
    assert "reader" in m.roles


def test_cached_read_does_not_reparse(tmp_path, monkeypatch):
    p = tmp_path / "authz.yaml"
    p.write_text(_VALID)
    loader = RoleMappingLoader(p)
    first = loader.get()

    # Spy on parse to confirm it isn't called again on the second get()
    from iris.auth.authz import loader as loader_mod
    calls = {"n": 0}
    real_parse = loader_mod.parse

    def counting_parse(text):
        calls["n"] += 1
        return real_parse(text)

    monkeypatch.setattr(loader_mod, "parse", counting_parse)

    second = loader.get()
    assert second is first
    assert calls["n"] == 0


def test_mtime_change_triggers_reload(tmp_path):
    p = tmp_path / "authz.yaml"
    p.write_text(_VALID)
    loader = RoleMappingLoader(p)
    first = loader.get()
    assert "reader" in first.roles

    _write(p, _VALID_2)
    second = loader.get()
    assert "writer" in second.roles
    assert "reader" not in second.roles
    assert second is not first


def test_invalid_edit_after_good_load_returns_last_good(tmp_path, caplog):
    p = tmp_path / "authz.yaml"
    p.write_text(_VALID)
    loader = RoleMappingLoader(p)
    first = loader.get()

    _write(p, "roles:\n  bad:\n    unknown_key: 1\n")

    with caplog.at_level(logging.ERROR, logger="iris.auth.authz.loader"):
        second = loader.get()

    assert second is first  # last-good fallback
    assert any("authz" in rec.message.lower() or "role" in rec.message.lower() for rec in caplog.records)


def test_deleted_file_after_good_load_returns_last_good(tmp_path, caplog):
    p = tmp_path / "authz.yaml"
    p.write_text(_VALID)
    loader = RoleMappingLoader(p)
    first = loader.get()

    p.unlink()

    with caplog.at_level(logging.ERROR, logger="iris.auth.authz.loader"):
        second = loader.get()

    assert second is first


def test_first_load_failure_raises(tmp_path):
    p = tmp_path / "missing.yaml"
    loader = RoleMappingLoader(p)
    with pytest.raises((FileNotFoundError, RoleMappingError)):
        loader.get()


def test_first_load_invalid_content_raises(tmp_path):
    p = tmp_path / "authz.yaml"
    p.write_text("roles:\n  bad:\n    unknown_key: 1\n")
    loader = RoleMappingLoader(p)
    with pytest.raises(RoleMappingError):
        loader.get()


def test_recovery_after_bad_then_good(tmp_path):
    p = tmp_path / "authz.yaml"
    p.write_text(_VALID)
    loader = RoleMappingLoader(p)
    first = loader.get()

    _write(p, "garbage: [unclosed")
    bad = loader.get()
    assert bad is first  # fallback

    _write(p, _VALID_2)
    recovered = loader.get()
    assert "writer" in recovered.roles
    assert recovered is not first
```

- [ ] **Step 2: Run — expect collection failure**

```bash
uv run pytest tests/auth/authz/test_loader.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `loader.py`**

Write `src/iris/auth/authz/loader.py`:

```python
from __future__ import annotations

import logging
import threading
from pathlib import Path

from iris.auth.authz.mapping import RoleMapping, RoleMappingError, parse

logger = logging.getLogger("iris.auth.authz.loader")


class RoleMappingLoader:
    """Loads a role mapping from disk, caching by mtime.

    On `get()`:
      1. stat the file. If mtime unchanged, return cached mapping.
      2. Otherwise, attempt to re-read and parse.
         - On success: cache and return the new mapping.
         - On failure: if a previously good mapping exists, log ERROR and
           return the cached one; otherwise re-raise (first-load failure
           must propagate so install() can fail loudly at boot).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._cached: RoleMapping | None = None
        self._cached_mtime_ns: int | None = None

    def get(self) -> RoleMapping:
        # Fast path: no lock when nothing changed.
        try:
            mtime = self._path.stat().st_mtime_ns
        except FileNotFoundError:
            return self._handle_load_failure(FileNotFoundError(f"file missing: {self._path}"))

        if self._cached is not None and mtime == self._cached_mtime_ns:
            return self._cached

        with self._lock:
            # Re-stat under lock in case another request just reloaded.
            try:
                mtime = self._path.stat().st_mtime_ns
            except FileNotFoundError:
                return self._handle_load_failure(
                    FileNotFoundError(f"file missing: {self._path}")
                )

            if self._cached is not None and mtime == self._cached_mtime_ns:
                return self._cached

            try:
                text = self._path.read_text()
                mapping = parse(text)
            except (FileNotFoundError, RoleMappingError, OSError) as exc:
                return self._handle_load_failure(exc)

            self._cached = mapping
            self._cached_mtime_ns = mtime
            return mapping

    def _handle_load_failure(self, exc: Exception) -> RoleMapping:
        if self._cached is None:
            raise exc
        logger.error(
            "authz: failed to reload role mapping from %s; keeping last good mapping. error=%s",
            self._path,
            exc,
        )
        return self._cached
```

- [ ] **Step 4: Run loader tests — expect pass**

```bash
uv run pytest tests/auth/authz/test_loader.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/iris/auth/authz/loader.py tests/auth/authz/test_loader.py
git commit -m "feat(authz): add RoleMappingLoader with mtime cache and last-good fallback"
```

---

## Task 6: Add `AuthorizationMisconfigured` exception + 500 handler

**Files:**
- Modify: `src/iris/auth/exceptions.py`
- Create: `tests/auth/test_authorization_misconfigured.py`

- [ ] **Step 1: Write failing test**

Write `tests/auth/test_authorization_misconfigured.py`:

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iris.auth.exceptions import AuthorizationMisconfigured, install_exception_handlers


def test_authorization_misconfigured_returns_500_without_leaking_role_name(caplog):
    import logging

    app = FastAPI()
    install_exception_handlers(app, cookie_name="iris_session")

    @app.get("/oops")
    async def oops():
        raise AuthorizationMisconfigured("super_admin")

    with caplog.at_level(logging.ERROR, logger="iris.auth"):
        r = TestClient(app).get("/oops")

    assert r.status_code == 500
    assert "super_admin" not in r.text  # role name must not leak in response body
    # but it should appear in logs so operators can find the misconfig
    assert any("super_admin" in rec.message for rec in caplog.records)


def test_authorization_misconfigured_constructor_stores_role_name():
    exc = AuthorizationMisconfigured("missing_role")
    assert exc.role == "missing_role"
    assert "missing_role" in str(exc)
```

- [ ] **Step 2: Run — expect ImportError**

```bash
uv run pytest tests/auth/test_authorization_misconfigured.py -v
```

Expected: `ImportError: cannot import name 'AuthorizationMisconfigured'`.

- [ ] **Step 3: Add exception and handler**

Append to `src/iris/auth/exceptions.py` (just after the existing `AuthError` class, before `_wants_html`):

```python
class AuthorizationMisconfigured(RuntimeError):
    """Raised when a route requires a role not defined in the current YAML.

    Treated as a deploy-time bug, not a permission denial. The handler
    returns 500 with a generic body; the missing role name is logged
    server-side but never returned to the client.
    """

    def __init__(self, role: str) -> None:
        super().__init__(f"role {role!r} is not defined in the role mapping")
        self.role = role
```

Then add a logger at the top of the file (after the imports):

```python
import logging

logger = logging.getLogger("iris.auth")
```

And add a handler inside `install_exception_handlers`, after `_on_auth_forbidden`:

```python
@app.exception_handler(AuthorizationMisconfigured)
async def _on_authorization_misconfigured(
    request: Request, exc: AuthorizationMisconfigured
) -> Response:
    logger.error(
        "authz: route requires role %r which is not defined in the role mapping",
        exc.role,
    )
    return Response(status_code=500, content="Internal Server Error")
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/auth/test_authorization_misconfigured.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/iris/auth/exceptions.py tests/auth/test_authorization_misconfigured.py
git commit -m "feat(auth): add AuthorizationMisconfigured exception with 500 handler"
```

---

## Task 7: Build `deps.py` — `require_role`, `CurrentRoles`, `_current_mapping`

**Files:**
- Create: `src/iris/auth/authz/deps.py`
- Create: `tests/auth/authz/test_authz_deps.py`

(Filename uses the `authz_` prefix to avoid colliding with the existing `tests/auth/test_deps.py` — `--import-mode=importlib` requires unique basenames across the test tree.)

- [ ] **Step 1: Write failing tests**

Write `tests/auth/authz/test_authz_deps.py`:

```python
import asyncio
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from iris.auth.authz.deps import CurrentRoles, require_role
from iris.auth.authz.loader import RoleMappingLoader
from iris.auth.deps import set_session_store, set_settings
from iris.auth.exceptions import install_exception_handlers
from iris.auth.identity import User
from iris.auth.sessions import InMemorySessionStore


_FIXTURE_YAML = """
roles:
  reader:
    groups: []
    users: []
  writer:
    groups: ["editors"]
    users: ["bob"]
    includes: ["reader"]
  admin:
    groups: ["admins"]
    users: ["Alice"]
    includes: ["writer"]
"""


def _build_app(tmp_path: Path) -> tuple[FastAPI, InMemorySessionStore]:
    yaml_path = tmp_path / "authz.yaml"
    yaml_path.write_text(_FIXTURE_YAML)

    app = FastAPI()
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600)
    set_session_store(app, store)
    set_settings(app, cookie_name="iris_session")
    install_exception_handlers(app, cookie_name="iris_session")
    app.state.authz_loader = RoleMappingLoader(yaml_path)

    @app.get("/reader-only")
    async def reader_only(user: User = Depends(require_role("reader"))):
        return {"subject": user.subject}

    @app.get("/admin-only")
    async def admin_only(user: User = Depends(require_role("admin"))):
        return {"subject": user.subject}

    @app.get("/needs-undefined-role")
    async def needs_undefined(user: User = Depends(require_role("super_admin"))):
        return {"subject": user.subject}

    @app.get("/my-roles")
    async def my_roles(roles: CurrentRoles):
        return {"roles": sorted(roles)}

    return app, store


def _seed(store: InMemorySessionStore, *, username: str, groups: tuple[str, ...]) -> str:
    user = User(
        subject=f"mock:{username}",
        username=username,
        display_name=username.title(),
        groups=groups,
    )
    session = asyncio.run(store.create(user))
    return session.id


def test_admin_via_group_reaches_reader_only_route(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, username="charlie", groups=("admins",))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/reader-only", headers={"accept": "application/json"})
    assert r.status_code == 200


def test_writer_via_username_reaches_reader_only_route(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, username="bob", groups=())
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/reader-only", headers={"accept": "application/json"})
    assert r.status_code == 200


def test_username_match_is_case_insensitive(tmp_path):
    app, store = _build_app(tmp_path)
    # YAML lists "Alice" with capital A; user logs in as "alice"
    sid = _seed(store, username="alice", groups=())
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/admin-only", headers={"accept": "application/json"})
    assert r.status_code == 200


def test_user_with_no_matching_role_is_forbidden(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, username="dave", groups=("strangers",))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/reader-only", headers={"accept": "application/json"})
    assert r.status_code == 403


def test_unauthenticated_user_gets_401(tmp_path):
    app, _ = _build_app(tmp_path)
    r = TestClient(app).get(
        "/reader-only", headers={"accept": "application/json"}
    )
    assert r.status_code == 401


def test_route_requiring_undefined_role_returns_500(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, username="alice", groups=("admins",))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/needs-undefined-role", headers={"accept": "application/json"})
    assert r.status_code == 500
    assert "super_admin" not in r.text


def test_current_roles_returns_full_effective_set_for_admin(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, username="charlie", groups=("admins",))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/my-roles", headers={"accept": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"roles": ["admin", "reader", "writer"]}


def test_current_roles_returns_empty_set_for_user_with_no_match(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, username="nobody", groups=())
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/my-roles", headers={"accept": "application/json"})
    assert r.json() == {"roles": []}
```

- [ ] **Step 2: Run — expect collection failure**

```bash
uv run pytest tests/auth/authz/test_authz_deps.py -v
```

Expected: `ModuleNotFoundError: No module named 'iris.auth.authz.deps'`.

- [ ] **Step 3: Implement `deps.py`**

Write `src/iris/auth/authz/deps.py`:

```python
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from iris.auth.authz.mapping import RoleMapping
from iris.auth.deps import CurrentUser
from iris.auth.exceptions import AuthForbidden, AuthorizationMisconfigured
from iris.auth.identity import User


def _resolve_roles(user: User, mapping: RoleMapping) -> frozenset[str]:
    base: set[str] = set()
    username_lower = user.username.lower()
    user_groups = set(user.groups)
    for role_name, role_def in mapping.roles.items():
        if username_lower in role_def.users_lower:
            base.add(role_name)
        elif role_def.groups & user_groups:
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

- [ ] **Step 4: Run deps tests**

```bash
uv run pytest tests/auth/authz/test_authz_deps.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/iris/auth/authz/deps.py tests/auth/authz/test_authz_deps.py
git commit -m "feat(authz): add require_role, CurrentRoles, and _current_mapping deps"
```

---

## Task 8: Update `tests/conftest.py` to provide `AUTHZ_CONFIG_PATH` fixture

**Why now:** Task 9 will make `install()` require `AUTHZ_CONFIG_PATH`. We must provide it via conftest *before* the install change so the suite never breaks. This commit changes only the test harness; production behavior is unaffected.

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Update conftest**

Replace `tests/conftest.py` with:

```python
import asyncio
import os
import tempfile

# Test fixtures that the auth layer needs at import time. setdefault means
# a developer's real .env / shell env can still override these.
os.environ.setdefault("AUTH_METHOD", "mock")
os.environ.setdefault("MOCK_USERNAME", "alice")
os.environ.setdefault("MOCK_PASSWORD", "secret")
os.environ.setdefault("MOCK_GROUPS", "admins,users")
os.environ.setdefault("MOCK_DISPLAY_NAME", "Alice")
os.environ.setdefault("COOKIE_SECURE", "false")

# Write a fixture role mapping that maps the mock user's groups into roles
# so authed_client can hit role-gated routes. Lives in a tempfile that's
# not cleaned up — leaks one file per test session, acceptable for v1.
_AUTHZ_FIXTURE = """\
roles:
  reader:
    groups: []
    users: []
  writer:
    groups: []
    users: []
    includes: ["reader"]
  admin:
    groups: ["admins"]
    users: []
    includes: ["writer"]
"""

_authz_path = os.path.join(tempfile.gettempdir(), "iris-test-authz.yaml")
with open(_authz_path, "w") as f:
    f.write(_AUTHZ_FIXTURE)
os.environ.setdefault("AUTHZ_CONFIG_PATH", _authz_path)

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app():
    from iris.app import build_app
    return build_app()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def authed_client(app):
    from iris.auth.identity import User

    c = TestClient(app)
    store = app.state.auth_session_store
    user = User(
        subject="mock:alice",
        username="alice",
        display_name="Alice",
        groups=("admins", "users"),
    )
    session = asyncio.run(store.create(user))
    c.cookies.set("iris_session", session.id)
    return c
```

- [ ] **Step 2: Run full suite**

```bash
uv run pytest
```

Expected: green. The conftest now writes the fixture YAML and sets `AUTHZ_CONFIG_PATH`, but nothing reads it yet from the install side, so behavior is unchanged.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test(authz): write fixture role-mapping YAML in conftest"
```

---

## Task 9: Wire `AuthzSettings` + `RoleMappingLoader` into `install()`

**Files:**
- Modify: `src/iris/auth/routes.py:161-200` (`install` function)
- Modify: `src/iris/auth/__init__.py` (re-export new symbols; do NOT remove `require_group` yet — that's Task 12, kept separate so this commit is reversible)

- [ ] **Step 1: Write a failing integration test for boot-time validation**

Write `tests/auth/authz/test_install_wiring.py`:

```python
import pytest


def test_install_fails_loud_when_authz_config_path_missing(monkeypatch):
    """Without AUTHZ_CONFIG_PATH, build_app must fail at boot.

    AuthzSettings.from_env() reads os.environ at call time, so removing
    the variable for this one test is sufficient — no module reload needed.
    """
    monkeypatch.delenv("AUTHZ_CONFIG_PATH", raising=False)

    from iris.app import build_app

    with pytest.raises(ValueError, match="AUTHZ_CONFIG_PATH"):
        build_app()


def test_install_fails_loud_when_authz_yaml_invalid(tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("roles:\n  bad:\n    unknown_key: 1\n")
    monkeypatch.setenv("AUTHZ_CONFIG_PATH", str(bad))

    from iris.app import build_app
    from iris.auth.authz.mapping import RoleMappingError

    with pytest.raises(RoleMappingError):
        build_app()


def test_install_attaches_loader_to_app_state(tmp_path, monkeypatch):
    good = tmp_path / "good.yaml"
    good.write_text("roles:\n  reader: {}\n")
    monkeypatch.setenv("AUTHZ_CONFIG_PATH", str(good))

    from iris.app import build_app
    from iris.auth.authz.loader import RoleMappingLoader

    app = build_app()
    assert isinstance(app.state.authz_loader, RoleMappingLoader)
```

- [ ] **Step 2: Run — expect failures (no wiring yet)**

```bash
uv run pytest tests/auth/authz/test_install_wiring.py -v
```

Expected: failures because `app.state.authz_loader` doesn't exist and bad YAML doesn't fail boot.

- [ ] **Step 3: Update `install()` in `src/iris/auth/routes.py`**

Modify the `install` function (currently lines 161-200). Add the authz wiring near the top, before the session store creation:

```python
def install(app: FastAPI) -> None:
    """Wire the auth package into a FastAPI app: settings, store, exception handlers, router."""
    from iris.auth.config import AuthSettings
    from iris.auth.authz.config import AuthzSettings
    from iris.auth.authz.loader import RoleMappingLoader
    from iris.auth.deps import set_session_store, set_settings
    from iris.auth.exceptions import install_exception_handlers
    from iris.auth.providers import build_provider

    settings = AuthSettings.from_env()
    authz_settings = AuthzSettings.from_env()
    loader = RoleMappingLoader(authz_settings.config_path)
    loader.get()  # eager initial load; bad YAML stops the app from booting
    app.state.authz_loader = loader

    store = InMemorySessionStore(
        ttl_seconds=settings.ttl_seconds,
        absolute_ttl_seconds=settings.absolute_ttl_seconds,
        max_per_user=settings.max_per_user,
    )
    provider = build_provider(settings)

    from iris.app import TEMPLATES
    app.state.templates = TEMPLATES

    set_session_store(app, store)
    set_settings(
        app, cookie_name=settings.cookie_name, cookie_secure=settings.cookie_secure
    )
    install_exception_handlers(app, cookie_name=settings.cookie_name)

    router = build_auth_router(
        provider=provider,
        store=store,
        cookie_name=settings.cookie_name,
        cookie_secure=settings.cookie_secure,
        ttl_seconds=settings.ttl_seconds,
    )
    app.include_router(router)

    if isinstance(provider, OAuthProvider):
        @app.on_event("shutdown")
        async def _close_oauth_provider() -> None:  # pragma: no cover
            await provider.close()
```

- [ ] **Step 4: Add `require_role` and `CurrentRoles` to public re-exports**

Update `src/iris/auth/__init__.py` to:

```python
from iris.auth.authz.deps import CurrentRoles, require_role
from iris.auth.deps import (
    CurrentSession,
    CurrentUser,
    OptionalCurrentUser,
    SessionData,
    require_group,
)
from iris.auth.identity import User, UserSession
from iris.auth.routes import install

__all__ = [
    "CurrentRoles",
    "CurrentSession",
    "CurrentUser",
    "OptionalCurrentUser",
    "SessionData",
    "User",
    "UserSession",
    "install",
    "require_group",
    "require_role",
]
```

(`require_group` is intentionally still here; Task 12 deletes it.)

- [ ] **Step 5: Run install-wiring tests**

```bash
uv run pytest tests/auth/authz/test_install_wiring.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Run full suite**

```bash
uv run pytest
```

Expected: green. The conftest already provides a valid `AUTHZ_CONFIG_PATH`, so the eager `loader.get()` succeeds for every test that calls `build_app()`.

- [ ] **Step 7: Commit**

```bash
git add src/iris/auth/routes.py \
        src/iris/auth/__init__.py \
        tests/auth/authz/test_install_wiring.py
git commit -m "feat(authz): wire AuthzSettings + RoleMappingLoader into install()"
```

---

## Task 10: Migrate `tests/auth/test_deps.py` from `require_group` to `require_role`

**Why before Task 12:** Deleting `require_group` last keeps the suite green at every commit boundary.

**Files:**
- Modify: `tests/auth/test_deps.py:11,36-37,112-127`

- [ ] **Step 1: Update imports in `tests/auth/test_deps.py`**

Change the import block (lines 6-17) to:

```python
from iris.auth.authz.deps import require_role
from iris.auth.authz.loader import RoleMappingLoader
from iris.auth.deps import (
    CurrentSession,
    CurrentUser,
    OptionalCurrentUser,
    SessionData,
    set_session_store,
    set_settings,
)
from iris.auth.exceptions import install_exception_handlers
from iris.auth.identity import User
from iris.auth.sessions import InMemorySessionStore
```

- [ ] **Step 2: Update `_build_app` to attach a loader and use `require_role`**

The existing `_build_app` function (lines 20-56) builds an app without an `authz_loader`. Add the loader. At the top of the function, before the existing app setup, add a `tmp_path` parameter and write a fixture YAML:

```python
def _build_app(tmp_path) -> tuple[FastAPI, InMemorySessionStore]:
    yaml_path = tmp_path / "authz.yaml"
    yaml_path.write_text(
        "roles:\n"
        "  admin:\n"
        "    groups: [\"admins\"]\n"
    )
    app = FastAPI()
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600)
    set_session_store(app, store)
    set_settings(app, cookie_name="iris_session")
    install_exception_handlers(app, cookie_name="iris_session")
    app.state.authz_loader = RoleMappingLoader(yaml_path)

    @app.get("/me")
    async def me(user: CurrentUser):
        return {"subject": user.subject}

    @app.get("/optional")
    async def optional(user: OptionalCurrentUser):
        return {"present": user is not None}

    @app.get("/admin")
    async def admin(user: User = Depends(require_role("admin"))):
        return {"subject": user.subject}

    @app.get("/data")
    async def read_data(data: SessionData):
        return {"counter": data.get("counter", 0)}

    @app.post("/data")
    async def bump_data(data: SessionData):
        data["counter"] = data.get("counter", 0) + 1
        return {"counter": data["counter"]}

    @app.get("/whoami-full")
    async def whoami_full(session: CurrentSession):
        return {
            "id": session.id,
            "subject": session.user.subject,
            "data_keys": sorted(session.data.keys()),
        }

    return app, store
```

- [ ] **Step 3: Update every test function to pass `tmp_path` to `_build_app`**

Every existing test calls `_build_app()` with no arguments. Add `tmp_path` as a fixture param. Example for the first test:

```python
def test_no_credentials_returns_401_for_api(tmp_path):
    app, _ = _build_app(tmp_path)
    r = TestClient(app).get("/me", headers={"accept": "application/json"})
    assert r.status_code == 401
```

Apply the same change to every test in the file:

- `test_no_credentials_returns_401_for_api`
- `test_cookie_credential_resolves_user`
- `test_bearer_credential_resolves_user`
- `test_optional_returns_none_when_unauthenticated`
- `test_optional_returns_user_when_authenticated`
- `test_require_group_admits_member` → rename to `test_require_role_admits_member`
- `test_require_group_rejects_non_member` → rename to `test_require_role_rejects_non_member`
- `test_session_data_round_trip`
- `test_session_data_isolated_between_sessions`
- `test_session_data_requires_auth`
- `test_current_session_exposes_id_user_and_data`

Each gets `tmp_path` added as a parameter, and the `_build_app()` call becomes `_build_app(tmp_path)`.

- [ ] **Step 4: Verify rename of the two require_group tests**

The two renamed tests should now read:

```python
def test_require_role_admits_member(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, groups=("admins", "users"))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/admin", headers={"accept": "application/json"})
    assert r.status_code == 200


def test_require_role_rejects_non_member(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, groups=("users",))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/admin", headers={"accept": "application/json"})
    assert r.status_code == 403
```

- [ ] **Step 5: Run the file**

```bash
uv run pytest tests/auth/test_deps.py -v
```

Expected: all tests pass with the new role-based names.

- [ ] **Step 6: Run full suite**

```bash
uv run pytest
```

Expected: green.

- [ ] **Step 7: Commit**

```bash
git add tests/auth/test_deps.py
git commit -m "test(auth): migrate test_deps.py from require_group to require_role"
```

---

## Task 11: Migrate `tests/auth/test_error_pages.py`

**Files:**
- Modify: `tests/auth/test_error_pages.py:4,14-15`

- [ ] **Step 1: Replace the `require_group` usage**

Change the file to:

```python
from fastapi import Depends
from fastapi.testclient import TestClient

from iris.auth.authz.deps import require_role
from iris.auth.identity import User


def test_forbidden_html_renders_template(monkeypatch):
    monkeypatch.setenv("MOCK_GROUPS", "users")  # NOT admins
    from iris.app import build_app

    app = build_app()

    @app.get("/admin-only")
    async def admin_only(_: User = Depends(require_role("admin"))):
        return {"ok": True}

    client = TestClient(app)
    from iris.auth.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD

    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "username": "alice",
            "password": "secret",
            "next": "/",
        },
    )
    r = client.get("/admin-only", headers={"accept": "text/html"})
    assert r.status_code == 403
    assert "Forbidden" in r.text
    assert "admin" in r.text
```

(The conftest's fixture YAML defines `admin` with `groups: ["admins"]`; the test sets `MOCK_GROUPS=users`, so the user is not in `admins` and the role check fails.)

- [ ] **Step 2: Run the file**

```bash
uv run pytest tests/auth/test_error_pages.py -v
```

Expected: pass.

- [ ] **Step 3: Run full suite**

```bash
uv run pytest
```

Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/auth/test_error_pages.py
git commit -m "test(auth): migrate test_error_pages.py to require_role"
```

---

## Task 12: Delete `require_group` from `deps.py` and from public re-exports

**Files:**
- Modify: `src/iris/auth/deps.py:81-87` (delete `require_group`)
- Modify: `src/iris/auth/__init__.py` (drop `require_group` import + `__all__` entry)

- [ ] **Step 1: Confirm no remaining references**

```bash
grep -rn "require_group" /home/driou/dev/project/iris/src /home/driou/dev/project/iris/tests --include='*.py'
```

Expected: only the two locations being modified appear (`src/iris/auth/deps.py`, `src/iris/auth/__init__.py`). If any other reference remains, fix it before continuing.

- [ ] **Step 2: Delete the `require_group` function**

Remove lines 81-87 from `src/iris/auth/deps.py` (the entire `def require_group(*groups: str): ...` block).

- [ ] **Step 3: Update `src/iris/auth/__init__.py` to drop the symbol**

```python
from iris.auth.authz.deps import CurrentRoles, require_role
from iris.auth.deps import (
    CurrentSession,
    CurrentUser,
    OptionalCurrentUser,
    SessionData,
)
from iris.auth.identity import User, UserSession
from iris.auth.routes import install

__all__ = [
    "CurrentRoles",
    "CurrentSession",
    "CurrentUser",
    "OptionalCurrentUser",
    "SessionData",
    "User",
    "UserSession",
    "install",
    "require_role",
]
```

- [ ] **Step 4: Run full suite**

```bash
uv run pytest
```

Expected: green.

- [ ] **Step 5: Confirm `require_group` is truly gone**

```bash
uv run python -c "from iris.auth import require_group" 2>&1
```

Expected: `ImportError: cannot import name 'require_group' from 'iris.auth'`.

- [ ] **Step 6: Commit**

```bash
git add src/iris/auth/deps.py src/iris/auth/__init__.py
git commit -m "feat(auth)!: remove require_group; use require_role exclusively"
```

---

## Task 13: Update `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

The Authentication section currently documents `require_group`. Replace that documentation with the new role-based authorization story.

- [ ] **Step 1: Update the public-API example block at the top of the Authentication section**

Find the block that reads:

```python
from iris.auth import CurrentUser, OptionalCurrentUser, CurrentSession, SessionData, require_group
```

Change it to:

```python
from iris.auth import CurrentUser, OptionalCurrentUser, CurrentSession, SessionData, CurrentRoles, require_role
```

And the sentence that follows about `require_group`:

> `CurrentUser` requires a valid session (cookie or `Authorization: Bearer <session-id>`); `OptionalCurrentUser` returns `None` if no session is present. `require_group("admins")` is a dependency factory that 403s if the user isn't in the listed group.

becomes:

> `CurrentUser` requires a valid session (cookie or `Authorization: Bearer <session-id>`); `OptionalCurrentUser` returns `None` if no session is present. `require_role("admin")` is a dependency factory that 403s if the user's effective role set (computed from the role-mapping YAML) doesn't contain the named role. See "Authorization (roles)" below for the YAML schema and inheritance semantics.

- [ ] **Step 2: Add an "Authorization (roles)" subsection**

Insert a new subsection just before "Configuration":

````markdown
### Authorization (roles)

Application code references **internal role names only** (`admin`, `writer`, `reader`, etc.). The mapping from role → external IdP groups/usernames lives in a YAML file outside the code, edited by operators without a redeploy.

**YAML schema** (single file, path from `AUTHZ_CONFIG_PATH`):

```yaml
roles:
  reader:
    groups: []
    users: []
  writer:
    groups: ["editors"]
    users: ["bob"]
    includes: ["reader"]            # writers also have reader's permissions
  admin:
    groups: ["ldap-admins", "platform-team"]
    users: ["alice"]
    includes: ["writer"]            # admins transitively get writer + reader
```

Validation rules (enforced at load, fail-loud with line numbers):
- Top-level: exactly `roles:`. Other keys reject.
- Per-role keys restricted to `{groups, users, includes}`; all default to `[]`.
- Role names match `[a-zA-Z0-9_-]+`.
- `includes` must reference defined roles; the graph must be a DAG (cycles reject).
- Duplicate top-level role keys reject (custom YAML loader; PyYAML's default would silently overwrite).

**Identity matching:**
- `groups` — exact, case-sensitive match against `User.groups` (verbatim from the IdP).
- `users` — case-insensitive match against `User.username` (the new field on `User`).
  - OAuth provider sources `username` from the `preferred_username` claim, falling back to `sub` (the IdP's opaque subject identifier) if absent. If your OIDC IdP doesn't issue `preferred_username`, your `users:` lists must contain the `sub` UUIDs.
  - LDAP provider sources `username` from the `username` substituted into `LDAP_BIND_DN_TEMPLATE`.
  - Mock provider sources `username` from `MOCK_USERNAME`.

**Use in routes:**

```python
from iris.auth import require_role, CurrentRoles, CurrentUser

@app.get("/docs")
async def list_docs(user: User = Depends(require_role("reader"))):
    ...

@app.get("/me/roles")
async def my_roles(roles: CurrentRoles):
    return {"roles": sorted(roles)}
```

`require_role("reader")` admits any user whose effective role set contains `reader`, directly or via `includes` (so admins and writers get in too). `CurrentRoles` returns the user's full effective role set as a `frozenset[str]` — useful for templates and `/api/whoami`-style endpoints.

If a route names a role that isn't defined in the YAML, the request returns **500** (not 403) with a generic body — silent 403s would mask operator typos like `require_role("reder")`. The missing role name is logged server-side.

**Live reload:** the loader stats the YAML file on every request and reloads when mtime changes. Edit the file, save, and the next request sees the new mapping — no restart, no waiting for sessions to expire.

**Robustness against bad edits:** if a YAML edit produces an invalid file (syntax error, schema error, cycle, undefined include), the loader logs an `ERROR` and **keeps serving the last-known-good mapping**. Subsequent requests keep working until the file is fixed. Note that the *initial* load at boot is not protected by this fallback — a bad initial YAML stops the app from booting (consistent with the rest of the auth config's fail-loud style).
````

- [ ] **Step 3: Update the env-var section**

Add `AUTHZ_CONFIG_PATH` to the Configuration block:

```
AUTHZ_CONFIG_PATH=./authz.yaml       # role mapping; required, fail-loud if unset
```

Place it right after `COOKIE_SECURE`.

- [ ] **Step 4: Update the auth module map**

The "Module map" subsection lists the contents of `src/iris/auth/`. Add the new subpackage at the bottom:

```
src/iris/auth/
├── ...                # existing entries unchanged
└── authz/
    ├── __init__.py    # re-exports require_role, CurrentRoles, RoleMapping, RoleMappingLoader
    ├── config.py      # AuthzSettings.from_env() — reads AUTHZ_CONFIG_PATH
    ├── mapping.py     # RoleMapping value type + parse() with cycle detection + closure
    ├── loader.py      # RoleMappingLoader: mtime-cached, last-good fallback on bad reload
    └── deps.py        # require_role(name) factory; CurrentRoles dep
```

- [ ] **Step 5: Add an entry to "Open security follow-ups (v1.1)"**

Append a bullet:

```
- The role-mapping loader stats the YAML on every request. Sub-millisecond at ≤20-user scale; for higher request volumes, swap to a file watcher (e.g., `watchfiles`) or event-driven invalidation.
```

- [ ] **Step 6: Sanity-read CLAUDE.md to confirm no stale `require_group` references**

```bash
grep -n "require_group" /home/driou/dev/project/iris/CLAUDE.md
```

Expected: no matches (or only inside a "removed" / "previously" annotation if you chose to leave a migration note).

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): document role-based authorization (require_role, YAML, AUTHZ_CONFIG_PATH)"
```

---

## Final verification

- [ ] **Step 1: Run the full suite**

```bash
uv run pytest
```

Expected: all tests pass (the original count + the new authz tests).

- [ ] **Step 2: Boot the dev server with the fixture YAML**

```bash
AUTHZ_CONFIG_PATH=/tmp/iris-test-authz.yaml uv run iris &
sleep 1
curl -sf http://127.0.0.1:8000/ > /dev/null && echo "server up"
kill %1
```

Expected: `server up` printed.

- [ ] **Step 3: Confirm no orphan `require_group` references**

```bash
grep -rn "require_group" /home/driou/dev/project/iris/ --include='*.py' --include='*.md'
```

Expected: no matches anywhere.
