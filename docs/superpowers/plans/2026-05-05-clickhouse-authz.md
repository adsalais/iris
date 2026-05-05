# ClickHouse Authorization Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a self-contained `iris.clickhouse` package that provisions ClickHouse users, roles, grants, and row policies, plus audit-query helpers. Tested end-to-end against a real ClickHouse server via `testcontainers-python` (Docker). No auth integration, no routes — pure database-side, exposed as plain functions over a `clickhouse_connect` `Client`.

**Architecture:** Operations are free functions over `clickhouse_connect.driver.client.Client`. DDL/DCL is built from validated identifiers via the `identifiers` module and run via `client.command()`; audit `SELECT`s use `client.query(..., parameters={...})` with native `{name:Type}` binding. A frozen `ClickHouseSettings` dataclass loads connection + service-admin identities from env vars (mirrors `AuthSettings.from_env()`). Bootstrap (`ensure_service_admin`) creates the service-admin role and grants it to the configured user at startup.

**Tech Stack:** Python 3.13, `clickhouse-connect` (HTTP/8123, official driver, runtime), `testcontainers[clickhouse]` (dev), `pytest`, `basedpyright`, `ruff`, `uv`.

---

## Up-front conventions

- **Test file basenames must be globally unique** under `tests/` (per `CLAUDE.md` and the importlib import mode). All clickhouse tests are prefixed `test_clickhouse_*`.
- **No `__init__.py` in test directories** (per `CLAUDE.md`). `tests/clickhouse/` does not get one.
- **After every task, before committing**, run:
  - `uv run ruff check`
  - `uv run basedpyright --level error`
  - The relevant `uv run pytest tests/clickhouse/...` invocation for the task
  - These must all be clean. The project gates on basedpyright `--level warning` too — keep that clean by avoiding the suppressions categories listed in `pyproject.toml`.
- **chdb is being removed.** It was added speculatively for testing; the Phase-0 spike (documented in the spec) found it cannot run RBAC DDL.
- **Docker is required** for the full test suite. Tasks that use the testcontainer fixture won't pass in a Docker-less environment; that's expected.
- **Commits**: small, focused, conventional-commits style (`feat(clickhouse): ...`, `test(clickhouse): ...`, `chore(deps): ...`, `docs(clickhouse): ...`).

---

## File map

### Created

| Path | Purpose |
|---|---|
| `src/iris/clickhouse/__init__.py` | Public surface re-exports |
| `src/iris/clickhouse/identifiers.py` | `validate_identifier`, `quote_identifier`, `quote_string`, `policy_name`, `InvalidIdentifierError` |
| `src/iris/clickhouse/config.py` | `ClickHouseSettings` frozen dataclass + `from_env()` |
| `src/iris/clickhouse/client.py` | `build_client(settings)` factory |
| `src/iris/clickhouse/bootstrap.py` | `ensure_service_admin(client, settings)` |
| `src/iris/clickhouse/users.py` | `init_user_rights`, `USER_ROLE_SUFFIX`, `GROUP_ROLE_SUFFIX` |
| `src/iris/clickhouse/grants.py` | `grant_select_to_database`, `grant_insert_update_to_table` |
| `src/iris/clickhouse/policies.py` | `add_row_policy`, `revoke_row_policy` |
| `src/iris/clickhouse/audit.py` | Six audit functions |
| `tests/clickhouse/conftest.py` | Session-scoped `ch_container`, per-test `ch_settings`, `ch_client`, `prefix` |
| `tests/clickhouse/test_clickhouse_identifiers.py` | Pure unit tests for the identifiers module |
| `tests/clickhouse/test_clickhouse_settings.py` | Env parsing + validation |
| `tests/clickhouse/test_clickhouse_smoke.py` | Phase-0 DDL surface verification |
| `tests/clickhouse/test_clickhouse_bootstrap.py` | `ensure_service_admin` |
| `tests/clickhouse/test_clickhouse_users.py` | `init_user_rights` end-to-end |
| `tests/clickhouse/test_clickhouse_grants.py` | Both grant functions |
| `tests/clickhouse/test_clickhouse_policies.py` | `add_row_policy`, `revoke_row_policy` |
| `tests/clickhouse/test_clickhouse_audit.py` | All six audit functions |

### Modified

| Path | What changes |
|---|---|
| `pyproject.toml` | Remove `chdb>=4.1.6`; add `clickhouse-connect`; add `testcontainers[clickhouse]` (dev) |
| `.env` | Append a `# ClickHouse` block with example connection + service-admin vars |
| `CLAUDE.md` | Append a `## ClickHouse` section mirroring the Authentication section's style |

---

## Task 1: Swap dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1.1: Remove chdb, add clickhouse-connect**

```bash
uv remove chdb
uv add clickhouse-connect
```

- [ ] **Step 1.2: Add testcontainers as a dev dep**

```bash
uv add --dev "testcontainers[clickhouse]"
```

- [ ] **Step 1.3: Verify both libraries import cleanly**

```bash
uv run python -c "import clickhouse_connect; print('ok', clickhouse_connect.__version__)"
uv run python -c "from testcontainers.clickhouse import ClickHouseContainer; print('ok', ClickHouseContainer.__module__)"
```
Expected: each prints `ok <version>` / `ok testcontainers.clickhouse`. Any `ImportError` is a real problem.

- [ ] **Step 1.4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): swap chdb for clickhouse-connect + testcontainers"
```

---

## Task 2: Update .env with ClickHouse block

**Files:**
- Modify: `.env`

- [ ] **Step 2.1: Append a ClickHouse block to `.env`**

Append the following at the end of `.env` (do NOT replace existing content):

```
# ClickHouse connection (the server-side identity iris connects as).
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=iris_service
CLICKHOUSE_PASSWORD=replace-me
CLICKHOUSE_SECURE=true
CLICKHOUSE_VERIFY=true
# CLICKHOUSE_CA_CERT_PATH=/etc/ssl/certs/ca-bundle.crt

# ClickHouse: identity used for impersonation and as the wildcard-policy grantee.
# Typically equals CLICKHOUSE_USER. The role is granted to that user at startup
# and is the grantee of every wildcard `USING 1` row policy.
CLICKHOUSE_SERVICE_ADMIN_USER=iris_service
CLICKHOUSE_SERVICE_ADMIN_ROLE=service_admin_role
```

- [ ] **Step 2.2: Commit**

```bash
git add .env
git commit -m "chore(env): document ClickHouse connection vars"
```

---

## Task 3: identifiers — validate_identifier

**Files:**
- Create: `src/iris/clickhouse/__init__.py`
- Create: `src/iris/clickhouse/identifiers.py`
- Create: `tests/clickhouse/test_clickhouse_identifiers.py`

- [ ] **Step 3.1: Write the failing tests**

Create `tests/clickhouse/test_clickhouse_identifiers.py` with:

```python
import pytest

from iris.clickhouse.identifiers import (
    InvalidIdentifierError,
    validate_identifier,
)


def test_validate_identifier_accepts_alphanumeric_underscore():
    assert validate_identifier("alice", kind="username") == "alice"
    assert validate_identifier("user_42", kind="username") == "user_42"
    assert validate_identifier("ABC", kind="role") == "ABC"


def test_validate_identifier_rejects_empty_string():
    with pytest.raises(InvalidIdentifierError, match="username"):
        validate_identifier("", kind="username")


def test_validate_identifier_rejects_dash_dot_space():
    for bad in ("a-b", "a.b", "a b", "a/b", "a`b", "a;b"):
        with pytest.raises(InvalidIdentifierError, match="role"):
            validate_identifier(bad, kind="role")


def test_validate_identifier_kind_appears_in_error_message():
    with pytest.raises(InvalidIdentifierError, match=r"invalid database: 'has space'"):
        validate_identifier("has space", kind="database")
```

- [ ] **Step 3.2: Run tests, verify they fail with ImportError**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py -v
```
Expected: 4 errors (collection failures) — module `iris.clickhouse.identifiers` not found.

- [ ] **Step 3.3: Create empty package init**

Create `src/iris/clickhouse/__init__.py` with one line:

```python
"""ClickHouse provisioning and audit helpers."""
```

- [ ] **Step 3.4: Implement `validate_identifier` and the error type**

Create `src/iris/clickhouse/identifiers.py`:

```python
"""Validation and quoting helpers for ClickHouse SQL identifiers and string literals."""

from __future__ import annotations

import re

_IDENT_RE = re.compile(r"^[a-zA-Z0-9_]+$")


class InvalidIdentifierError(ValueError):
    """Raised when an identifier from external input would have to be escaped to be safe."""


def validate_identifier(name: str, *, kind: str) -> str:
    """Reject anything outside ``[a-zA-Z0-9_]+``. Returns ``name`` unchanged on success.

    ``kind`` is woven into the error message ("username", "role", "database", ...) so
    operators tracing a bad input can see where it entered.
    """
    if not isinstance(name, str) or not _IDENT_RE.fullmatch(name):
        raise InvalidIdentifierError(f"invalid {kind}: {name!r}")
    return name
```

- [ ] **Step 3.5: Run tests, verify they pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: 4 passed; ruff clean; basedpyright clean.

- [ ] **Step 3.6: Commit**

```bash
git add src/iris/clickhouse/__init__.py src/iris/clickhouse/identifiers.py tests/clickhouse/test_clickhouse_identifiers.py
git commit -m "feat(clickhouse): add validate_identifier"
```

---

## Task 4: identifiers — quote_identifier

**Files:**
- Modify: `src/iris/clickhouse/identifiers.py`
- Modify: `tests/clickhouse/test_clickhouse_identifiers.py`

- [ ] **Step 4.1: Add the failing tests**

Append to `tests/clickhouse/test_clickhouse_identifiers.py`:

```python
from iris.clickhouse.identifiers import quote_identifier  # noqa: E402  -- grouped with module imports above when applying


def test_quote_identifier_backticks_a_valid_name():
    assert quote_identifier("alice", kind="username") == "`alice`"


def test_quote_identifier_rejects_invalid_input():
    with pytest.raises(InvalidIdentifierError):
        quote_identifier("a b", kind="role")
```

(Move the new `quote_identifier` import up alongside the existing imports rather than leaving the `# noqa` — that comment exists only for clarity here.)

- [ ] **Step 4.2: Run tests, verify they fail**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py -v
```
Expected: 2 failures — `quote_identifier` not defined in module.

- [ ] **Step 4.3: Implement `quote_identifier`**

Append to `src/iris/clickhouse/identifiers.py`:

```python
def quote_identifier(name: str, *, kind: str) -> str:
    """Validate then backtick-quote. The validating regex blocks backticks, so the
    quoted form is always safe to inline into DDL."""
    return f"`{validate_identifier(name, kind=kind)}`"
```

- [ ] **Step 4.4: Run tests, verify they pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: 6 passed; clean.

- [ ] **Step 4.5: Commit**

```bash
git add src/iris/clickhouse/identifiers.py tests/clickhouse/test_clickhouse_identifiers.py
git commit -m "feat(clickhouse): add quote_identifier"
```

---

## Task 5: identifiers — quote_string

**Files:**
- Modify: `src/iris/clickhouse/identifiers.py`
- Modify: `tests/clickhouse/test_clickhouse_identifiers.py`

- [ ] **Step 5.1: Add the failing tests**

Append to `tests/clickhouse/test_clickhouse_identifiers.py`:

```python
from iris.clickhouse.identifiers import quote_string


def test_quote_string_wraps_plain_value():
    assert quote_string("EU") == "'EU'"


def test_quote_string_doubles_embedded_single_quotes():
    assert quote_string("O'Brien") == "'O''Brien'"


def test_quote_string_escapes_backslashes():
    assert quote_string(r"a\b") == r"'a\\b'"


def test_quote_string_handles_combined_escapes():
    # backslash must be escaped before quotes, otherwise '\\\'' would be ambiguous
    assert quote_string("a\\'b") == "'a\\\\''b'"
```

- [ ] **Step 5.2: Run tests, verify they fail**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py -v
```
Expected: 4 failures — `quote_string` not defined.

- [ ] **Step 5.3: Implement `quote_string`**

Append to `src/iris/clickhouse/identifiers.py`:

```python
def quote_string(value: str) -> str:
    """Quote a SQL string literal: backslashes are doubled, then single quotes are doubled."""
    if not isinstance(value, str):
        raise TypeError(f"quote_string expects str, got {type(value).__name__}")
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"
```

- [ ] **Step 5.4: Run tests, verify they pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: 10 passed; clean.

- [ ] **Step 5.5: Commit**

```bash
git add src/iris/clickhouse/identifiers.py tests/clickhouse/test_clickhouse_identifiers.py
git commit -m "feat(clickhouse): add quote_string"
```

---

## Task 6: identifiers — policy_name

**Files:**
- Modify: `src/iris/clickhouse/identifiers.py`
- Modify: `tests/clickhouse/test_clickhouse_identifiers.py`

- [ ] **Step 6.1: Add the failing tests**

Append to `tests/clickhouse/test_clickhouse_identifiers.py`:

```python
from iris.clickhouse.identifiers import policy_name


def test_policy_name_basic_shape():
    name = policy_name("orders", "lines", "writer", "EU")
    # <db>_<table>_<role>_<slug>_<8charhash>
    assert name.startswith("orders_lines_writer_EU_")
    suffix = name.split("_")[-1]
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)


def test_policy_name_distinct_for_distinct_values_with_same_slug():
    a = policy_name("db", "t", "r", "EU/UK")
    b = policy_name("db", "t", "r", "EU UK")
    # Slug strips both '/' and ' ' to '_', producing the same prefix...
    assert a.startswith("db_t_r_EU_UK_")
    assert b.startswith("db_t_r_EU_UK_")
    # ...but the trailing hash disambiguates.
    assert a != b


def test_policy_name_validates_identifier_arguments():
    with pytest.raises(InvalidIdentifierError):
        policy_name("bad-db", "t", "r", "EU")
    with pytest.raises(InvalidIdentifierError):
        policy_name("db", "bad table", "r", "EU")
    with pytest.raises(InvalidIdentifierError):
        policy_name("db", "t", "bad role", "EU")


def test_policy_name_handles_empty_or_only_special_value():
    name = policy_name("db", "t", "r", "!!!")
    # Slug of '!!!' is empty after stripping; substitute the placeholder 'v' and
    # rely on the hash to make it unique.
    assert name.startswith("db_t_r_v_")
```

- [ ] **Step 6.2: Run tests, verify they fail**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py -v
```
Expected: 4 failures — `policy_name` not defined.

- [ ] **Step 6.3: Implement `policy_name`**

Append to `src/iris/clickhouse/identifiers.py`:

```python
import hashlib

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")


def policy_name(database: str, table: str, role: str, value: str) -> str:
    """Build a row-policy name: ``<database>_<table>_<role>_<slug>_<8charhash>``.

    ``database``, ``table``, ``role`` are validated as identifiers. ``value`` is treated
    as opaque — non-[a-zA-Z0-9_] characters collapse to '_' for the slug, and an
    8-character SHA-256 hex digest of the raw value is appended so distinct values
    that happen to share a slug (``'EU/UK'`` vs ``'EU UK'``) get distinct names.
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(role, kind="role")
    if not isinstance(value, str):
        raise TypeError(f"policy_name value expects str, got {type(value).__name__}")
    slug = _SLUG_RE.sub("_", value).strip("_") or "v"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{database}_{table}_{role}_{slug}_{digest}"
```

(Move the `import hashlib` line up to the existing import block at the top of the file. The structure shown here is for clarity in the diff.)

- [ ] **Step 6.4: Run tests, verify they pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: 14 passed; clean.

- [ ] **Step 6.5: Commit**

```bash
git add src/iris/clickhouse/identifiers.py tests/clickhouse/test_clickhouse_identifiers.py
git commit -m "feat(clickhouse): add policy_name with slug+hash"
```

---

## Task 7: ClickHouseSettings — happy path

**Files:**
- Create: `src/iris/clickhouse/config.py`
- Create: `tests/clickhouse/test_clickhouse_settings.py`

- [ ] **Step 7.1: Write the failing test**

Create `tests/clickhouse/test_clickhouse_settings.py`:

```python
import os
import pytest

from iris.clickhouse.config import ClickHouseSettings


@pytest.fixture
def env(monkeypatch):
    """Wipe and rebuild the CLICKHOUSE_* env so tests are hermetic."""
    for key in list(os.environ):
        if key.startswith("CLICKHOUSE_"):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_from_env_minimal_happy_path(env):
    env.setenv("CLICKHOUSE_HOST", "ch.example.com")
    env.setenv("CLICKHOUSE_PORT", "8443")
    env.setenv("CLICKHOUSE_USER", "iris_service")
    env.setenv("CLICKHOUSE_PASSWORD", "secret")
    env.setenv("CLICKHOUSE_SECURE", "true")
    env.setenv("CLICKHOUSE_VERIFY", "true")
    env.setenv("CLICKHOUSE_SERVICE_ADMIN_USER", "iris_service")
    env.setenv("CLICKHOUSE_SERVICE_ADMIN_ROLE", "service_admin_role")

    s = ClickHouseSettings.from_env()

    assert s.host == "ch.example.com"
    assert s.port == 8443
    assert s.user == "iris_service"
    assert s.password == "secret"
    assert s.secure is True
    assert s.verify is True
    assert s.ca_cert_path is None
    assert s.service_admin_user == "iris_service"
    assert s.service_admin_role == "service_admin_role"


def test_from_env_optional_ca_cert_path(env):
    env.setenv("CLICKHOUSE_HOST", "h")
    env.setenv("CLICKHOUSE_PORT", "9000")
    env.setenv("CLICKHOUSE_USER", "u")
    env.setenv("CLICKHOUSE_PASSWORD", "p")
    env.setenv("CLICKHOUSE_SECURE", "false")
    env.setenv("CLICKHOUSE_VERIFY", "false")
    env.setenv("CLICKHOUSE_SERVICE_ADMIN_USER", "u")
    env.setenv("CLICKHOUSE_SERVICE_ADMIN_ROLE", "r")
    env.setenv("CLICKHOUSE_CA_CERT_PATH", "/etc/ssl/ca.pem")

    s = ClickHouseSettings.from_env()

    assert s.ca_cert_path == "/etc/ssl/ca.pem"
    assert s.secure is False
    assert s.verify is False
```

- [ ] **Step 7.2: Run tests, verify they fail**

```bash
uv run pytest tests/clickhouse/test_clickhouse_settings.py -v
```
Expected: collection error — module `iris.clickhouse.config` not found.

- [ ] **Step 7.3: Implement `ClickHouseSettings.from_env`**

Create `src/iris/clickhouse/config.py`:

```python
"""Settings for the ClickHouse module, loaded from the process environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

from iris.clickhouse.identifiers import validate_identifier


def _required(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ValueError(f"{name} is required")
    return val


def _get_bool(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("true", "1"):
        return True
    if raw in ("false", "0"):
        return False
    raise ValueError(f"{name} must be 'true' or 'false', got {raw!r}")


@dataclass(frozen=True, slots=True)
class ClickHouseSettings:
    host: str
    port: int
    user: str
    password: str
    secure: bool
    verify: bool
    ca_cert_path: str | None
    service_admin_user: str
    service_admin_role: str

    @classmethod
    def from_env(cls) -> "ClickHouseSettings":
        host = _required("CLICKHOUSE_HOST")
        port_raw = _required("CLICKHOUSE_PORT")
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ValueError(f"CLICKHOUSE_PORT must be an integer, got {port_raw!r}") from exc
        user = _required("CLICKHOUSE_USER")
        password = _required("CLICKHOUSE_PASSWORD")
        secure = _get_bool("CLICKHOUSE_SECURE")
        verify = _get_bool("CLICKHOUSE_VERIFY")
        ca_cert_path = os.environ.get("CLICKHOUSE_CA_CERT_PATH", "").strip() or None

        service_admin_user = validate_identifier(
            _required("CLICKHOUSE_SERVICE_ADMIN_USER"),
            kind="CLICKHOUSE_SERVICE_ADMIN_USER",
        )
        service_admin_role = validate_identifier(
            _required("CLICKHOUSE_SERVICE_ADMIN_ROLE"),
            kind="CLICKHOUSE_SERVICE_ADMIN_ROLE",
        )

        return cls(
            host=host,
            port=port,
            user=user,
            password=password,
            secure=secure,
            verify=verify,
            ca_cert_path=ca_cert_path,
            service_admin_user=service_admin_user,
            service_admin_role=service_admin_role,
        )
```

- [ ] **Step 7.4: Run tests, verify they pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_settings.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: 2 passed; clean.

- [ ] **Step 7.5: Commit**

```bash
git add src/iris/clickhouse/config.py tests/clickhouse/test_clickhouse_settings.py
git commit -m "feat(clickhouse): add ClickHouseSettings.from_env"
```

---

## Task 8: ClickHouseSettings — validation rules

**Files:**
- Modify: `tests/clickhouse/test_clickhouse_settings.py`

- [ ] **Step 8.1: Add failing validation tests**

Append to `tests/clickhouse/test_clickhouse_settings.py`:

```python
from iris.clickhouse.identifiers import InvalidIdentifierError


def _set_minimum(env):
    env.setenv("CLICKHOUSE_HOST", "h")
    env.setenv("CLICKHOUSE_PORT", "9000")
    env.setenv("CLICKHOUSE_USER", "u")
    env.setenv("CLICKHOUSE_PASSWORD", "p")
    env.setenv("CLICKHOUSE_SECURE", "false")
    env.setenv("CLICKHOUSE_VERIFY", "true")
    env.setenv("CLICKHOUSE_SERVICE_ADMIN_USER", "u")
    env.setenv("CLICKHOUSE_SERVICE_ADMIN_ROLE", "r")


@pytest.mark.parametrize(
    "missing",
    [
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_PORT",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_PASSWORD",
        "CLICKHOUSE_SECURE",
        "CLICKHOUSE_VERIFY",
        "CLICKHOUSE_SERVICE_ADMIN_USER",
        "CLICKHOUSE_SERVICE_ADMIN_ROLE",
    ],
)
def test_from_env_rejects_missing_required(env, missing):
    _set_minimum(env)
    env.delenv(missing)
    with pytest.raises(ValueError, match=missing):
        ClickHouseSettings.from_env()


def test_from_env_rejects_non_int_port(env):
    _set_minimum(env)
    env.setenv("CLICKHOUSE_PORT", "not-a-number")
    with pytest.raises(ValueError, match="CLICKHOUSE_PORT"):
        ClickHouseSettings.from_env()


def test_from_env_rejects_typo_boolean(env):
    _set_minimum(env)
    env.setenv("CLICKHOUSE_SECURE", "ture")
    with pytest.raises(ValueError, match="CLICKHOUSE_SECURE"):
        ClickHouseSettings.from_env()


def test_from_env_rejects_bad_service_admin_user_identifier(env):
    _set_minimum(env)
    env.setenv("CLICKHOUSE_SERVICE_ADMIN_USER", "has space")
    with pytest.raises(InvalidIdentifierError, match="CLICKHOUSE_SERVICE_ADMIN_USER"):
        ClickHouseSettings.from_env()


def test_from_env_rejects_bad_service_admin_role_identifier(env):
    _set_minimum(env)
    env.setenv("CLICKHOUSE_SERVICE_ADMIN_ROLE", "weird-role")
    with pytest.raises(InvalidIdentifierError, match="CLICKHOUSE_SERVICE_ADMIN_ROLE"):
        ClickHouseSettings.from_env()
```

- [ ] **Step 8.2: Run tests, verify they pass**

The implementation from Task 7 already enforces all of these. Verify nothing regresses:

```bash
uv run pytest tests/clickhouse/test_clickhouse_settings.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: all settings tests pass (2 from Task 7 + 12 new); clean.

- [ ] **Step 8.3: Commit**

```bash
git add tests/clickhouse/test_clickhouse_settings.py
git commit -m "test(clickhouse): pin ClickHouseSettings validation rules"
```

---

## Task 9: build_client factory + container fixture

**Files:**
- Create: `src/iris/clickhouse/client.py`
- Create: `tests/clickhouse/conftest.py`
- Create: `tests/clickhouse/test_clickhouse_smoke.py` (skeleton; populated in Task 10)

- [ ] **Step 9.1: Write the conftest**

Create `tests/clickhouse/conftest.py`:

```python
"""Fixtures for the ClickHouse test suite.

Spins up a real ClickHouse server in a Docker container once per pytest session,
populates the CLICKHOUSE_* env vars to point at it, and yields a `build_client`
result with the service admin already bootstrapped.

Tests should namespace any entities they create (users, roles, databases, tables)
with the ``prefix`` fixture, since state accumulates across tests within a session.
"""

from __future__ import annotations

import uuid

import pytest
from testcontainers.clickhouse import ClickHouseContainer

from iris.clickhouse.bootstrap import ensure_service_admin
from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings


@pytest.fixture(scope="session")
def ch_container():
    """One ClickHouse server per test session."""
    container = ClickHouseContainer("clickhouse/clickhouse-server:24")
    with container as ch:
        yield ch


@pytest.fixture
def ch_settings(ch_container, monkeypatch):
    """ClickHouseSettings pointing at the running container.

    Each test gets a fresh ``from_env()`` so the dataclass is hermetic — the env
    overrides are scoped to the test by ``monkeypatch``.
    """
    host = ch_container.get_container_host_ip()
    port = int(ch_container.get_exposed_port(8123))
    user = ch_container.username  # type: ignore[attr-defined]
    password = ch_container.password  # type: ignore[attr-defined]

    monkeypatch.setenv("CLICKHOUSE_HOST", host)
    monkeypatch.setenv("CLICKHOUSE_PORT", str(port))
    monkeypatch.setenv("CLICKHOUSE_USER", user)
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", password)
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")
    monkeypatch.setenv("CLICKHOUSE_VERIFY", "false")
    monkeypatch.setenv("CLICKHOUSE_SERVICE_ADMIN_USER", user)
    monkeypatch.setenv("CLICKHOUSE_SERVICE_ADMIN_ROLE", "service_admin_role")
    monkeypatch.delenv("CLICKHOUSE_CA_CERT_PATH", raising=False)

    return ClickHouseSettings.from_env()


@pytest.fixture
def ch_client(ch_settings):
    client = build_client(ch_settings)
    try:
        ensure_service_admin(client, ch_settings)
        yield client
    finally:
        client.close()


@pytest.fixture
def prefix() -> str:
    """Per-test UUID-derived prefix for entity names. Use it for usernames,
    roles, databases, and tables so tests don't collide on shared state."""
    return "t_" + uuid.uuid4().hex[:8]
```

(The two `# type: ignore[attr-defined]` lines are there because testcontainers exposes `username`/`password` on `ClickHouseContainer` instances at runtime but the type stubs may not surface them. If your installed `testcontainers` version has them typed, drop the suppressions.)

- [ ] **Step 9.2: Write the failing test**

Create `tests/clickhouse/test_clickhouse_smoke.py`:

```python
"""Smoke check that the testcontainer + build_client wiring works end-to-end.

Phase-0 verification grows here in Task 10; this initial test only confirms the
client can answer SELECT 1.
"""

from __future__ import annotations


def test_build_client_can_run_select_one(ch_client):
    result = ch_client.query("SELECT 1 AS one")
    rows = list(result.named_results())
    assert rows == [{"one": 1}]
```

- [ ] **Step 9.3: Run, verify ImportError on `build_client` and `ensure_service_admin`**

```bash
uv run pytest tests/clickhouse/test_clickhouse_smoke.py -v
```
Expected: collection failure — `iris.clickhouse.client` and `iris.clickhouse.bootstrap` don't exist yet.

- [ ] **Step 9.4: Implement `build_client`**

Create `src/iris/clickhouse/client.py`:

```python
"""Construct a clickhouse-connect Client from ClickHouseSettings."""

from __future__ import annotations

from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from iris.clickhouse.config import ClickHouseSettings


def build_client(settings: ClickHouseSettings) -> Client:
    """Return a configured ``clickhouse_connect`` ``Client`` for ``settings``."""
    kwargs: dict[str, Any] = {
        "host": settings.host,
        "port": settings.port,
        "username": settings.user,
        "password": settings.password,
        "secure": settings.secure,
        "verify": settings.verify,
    }
    if settings.ca_cert_path:
        kwargs["ca_cert"] = settings.ca_cert_path
    return clickhouse_connect.get_client(**kwargs)
```

- [ ] **Step 9.5: Stub `ensure_service_admin` so collection succeeds**

Create `src/iris/clickhouse/bootstrap.py` (final implementation lands in Task 10):

```python
"""Startup-time provisioning for the service-admin role."""

from __future__ import annotations

from clickhouse_connect.driver.client import Client

from iris.clickhouse.config import ClickHouseSettings


def ensure_service_admin(client: Client, settings: ClickHouseSettings) -> None:
    """Idempotent: ensure the service-admin role exists and is granted to the configured user.

    Implementation lands in Task 10; this stub keeps imports resolvable.
    """
    raise NotImplementedError("Task 10")
```

- [ ] **Step 9.6: Run smoke test, verify it fails because `ensure_service_admin` raises**

```bash
uv run pytest tests/clickhouse/test_clickhouse_smoke.py -v
```
Expected: 1 failure — `NotImplementedError: Task 10`. The container started and `build_client` works; only the bootstrap stub trips. That's the expected handoff into Task 10.

- [ ] **Step 9.7: Commit**

```bash
git add src/iris/clickhouse/client.py src/iris/clickhouse/bootstrap.py tests/clickhouse/conftest.py tests/clickhouse/test_clickhouse_smoke.py
git commit -m "feat(clickhouse): build_client factory + testcontainer fixture"
```

---

## Task 10: ensure_service_admin

**Files:**
- Modify: `src/iris/clickhouse/bootstrap.py`
- Create: `tests/clickhouse/test_clickhouse_bootstrap.py`

- [ ] **Step 10.1: Write the failing tests**

Create `tests/clickhouse/test_clickhouse_bootstrap.py`:

```python
"""Tests for ensure_service_admin."""

from __future__ import annotations

from iris.clickhouse.bootstrap import ensure_service_admin


def test_ensure_service_admin_creates_role_and_grants_to_user(ch_settings, ch_client):
    # ch_client fixture has already invoked ensure_service_admin; verify state.
    rows = list(
        ch_client.query(
            "SELECT name FROM system.roles WHERE name = {r:String}",
            parameters={"r": ch_settings.service_admin_role},
        ).named_results()
    )
    assert rows == [{"name": ch_settings.service_admin_role}]

    grants = list(
        ch_client.query(
            "SELECT granted_role_name FROM system.role_grants "
            "WHERE user_name = {u:String} AND granted_role_name = {r:String}",
            parameters={
                "u": ch_settings.service_admin_user,
                "r": ch_settings.service_admin_role,
            },
        ).named_results()
    )
    assert grants == [{"granted_role_name": ch_settings.service_admin_role}]


def test_ensure_service_admin_is_idempotent(ch_settings, ch_client):
    # Running again should not raise.
    ensure_service_admin(ch_client, ch_settings)
    ensure_service_admin(ch_client, ch_settings)
    rows = list(
        ch_client.query(
            "SELECT count() AS n FROM system.roles WHERE name = {r:String}",
            parameters={"r": ch_settings.service_admin_role},
        ).named_results()
    )
    assert rows == [{"n": 1}]
```

- [ ] **Step 10.2: Run, verify they fail**

```bash
uv run pytest tests/clickhouse/test_clickhouse_bootstrap.py tests/clickhouse/test_clickhouse_smoke.py -v
```
Expected: failures — `NotImplementedError: Task 10`.

- [ ] **Step 10.3: Implement `ensure_service_admin`**

Replace the body of `src/iris/clickhouse/bootstrap.py` with:

```python
"""Startup-time provisioning for the service-admin role."""

from __future__ import annotations

from clickhouse_connect.driver.client import Client

from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.identifiers import quote_identifier


def ensure_service_admin(client: Client, settings: ClickHouseSettings) -> None:
    """Idempotent: ensure the service-admin role exists and is granted to the configured user.

    Presumes ``settings.service_admin_user`` already exists in ClickHouse — that's
    an operator concern, since iris must already authenticate as it. If the user
    does not exist, the GRANT will raise.
    """
    role = quote_identifier(settings.service_admin_role, kind="service_admin_role")
    user = quote_identifier(settings.service_admin_user, kind="service_admin_user")
    client.command(f"CREATE ROLE IF NOT EXISTS {role}")
    client.command(f"GRANT {role} TO {user}")
```

- [ ] **Step 10.4: Run all clickhouse tests, verify they pass**

```bash
uv run pytest tests/clickhouse/ -v
uv run ruff check
uv run basedpyright --level error
```
Expected: all passing; clean.

- [ ] **Step 10.5: Commit**

```bash
git add src/iris/clickhouse/bootstrap.py tests/clickhouse/test_clickhouse_bootstrap.py
git commit -m "feat(clickhouse): ensure_service_admin"
```

---

## Task 11: init_user_rights — user + per-user role

**Files:**
- Create: `src/iris/clickhouse/users.py`
- Create: `tests/clickhouse/test_clickhouse_users.py`

- [ ] **Step 11.1: Write the failing test**

Create `tests/clickhouse/test_clickhouse_users.py`:

```python
"""Tests for init_user_rights — staged across Tasks 11/12/13."""

from __future__ import annotations

from iris.clickhouse.users import (
    GROUP_ROLE_SUFFIX,
    USER_ROLE_SUFFIX,
    init_user_rights,
)


def test_init_user_rights_creates_user_and_per_user_role(ch_client, ch_settings, prefix):
    username = f"{prefix}_alice"
    init_user_rights(ch_client, username=username, groups=[], settings=ch_settings)

    users = list(
        ch_client.query(
            "SELECT name FROM system.users WHERE name = {u:String}",
            parameters={"u": username},
        ).named_results()
    )
    assert users == [{"name": username}]

    user_role = username + USER_ROLE_SUFFIX
    roles = list(
        ch_client.query(
            "SELECT name FROM system.roles WHERE name = {r:String}",
            parameters={"r": user_role},
        ).named_results()
    )
    assert roles == [{"name": user_role}]

    role_grants = list(
        ch_client.query(
            "SELECT granted_role_name FROM system.role_grants "
            "WHERE user_name = {u:String} AND granted_role_name = {r:String}",
            parameters={"u": username, "r": user_role},
        ).named_results()
    )
    assert role_grants == [{"granted_role_name": user_role}]


def test_init_user_rights_is_idempotent(ch_client, ch_settings, prefix):
    username = f"{prefix}_idem"
    init_user_rights(ch_client, username=username, groups=[], settings=ch_settings)
    init_user_rights(ch_client, username=username, groups=[], settings=ch_settings)

    user_role = username + USER_ROLE_SUFFIX
    n = list(
        ch_client.query(
            "SELECT count() AS n FROM system.role_grants "
            "WHERE user_name = {u:String} AND granted_role_name = {r:String}",
            parameters={"u": username, "r": user_role},
        ).named_results()
    )
    assert n == [{"n": 1}]


def test_init_user_rights_rejects_bad_username(ch_client, ch_settings):
    from iris.clickhouse.identifiers import InvalidIdentifierError
    import pytest

    with pytest.raises(InvalidIdentifierError):
        init_user_rights(ch_client, username="bad name", groups=[], settings=ch_settings)


def test_init_user_rights_rejects_bad_group(ch_client, ch_settings, prefix):
    from iris.clickhouse.identifiers import InvalidIdentifierError
    import pytest

    with pytest.raises(InvalidIdentifierError):
        init_user_rights(
            ch_client,
            username=f"{prefix}_u",
            groups=["good", "bad group"],
            settings=ch_settings,
        )


def test_user_role_suffix_constant():
    assert USER_ROLE_SUFFIX == "_USER"
    assert GROUP_ROLE_SUFFIX == "_GRP"
```

- [ ] **Step 11.2: Run, verify ImportError**

```bash
uv run pytest tests/clickhouse/test_clickhouse_users.py -v
```
Expected: collection failure — `iris.clickhouse.users` not found.

- [ ] **Step 11.3: Implement the user + per-user role half**

Create `src/iris/clickhouse/users.py`:

```python
"""Provisioning of per-user ClickHouse identities and group-derived role memberships."""

from __future__ import annotations

from clickhouse_connect.driver.client import Client

from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.identifiers import quote_identifier, validate_identifier

USER_ROLE_SUFFIX = "_USER"
GROUP_ROLE_SUFFIX = "_GRP"


def init_user_rights(
    client: Client,
    *,
    username: str,
    groups: list[str],
    settings: ClickHouseSettings,
) -> None:
    """Idempotently provision a CH user, their per-user role, group memberships, and the
    IMPERSONATE grant for the service admin.

    Steps 1–3 only in this task; group reconcile lands in Task 12, IMPERSONATE in Task 13.
    """
    validate_identifier(username, kind="username")
    for group in groups:
        validate_identifier(group, kind="group")

    user_q = quote_identifier(username, kind="username")
    user_role_q = quote_identifier(username + USER_ROLE_SUFFIX, kind="role")

    client.command(f"CREATE USER IF NOT EXISTS {user_q} IDENTIFIED WITH no_password")
    client.command(f"CREATE ROLE IF NOT EXISTS {user_role_q}")
    client.command(f"GRANT {user_role_q} TO {user_q}")
```

- [ ] **Step 11.4: Run, verify they pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_users.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: 5 passed; clean.

- [ ] **Step 11.5: Commit**

```bash
git add src/iris/clickhouse/users.py tests/clickhouse/test_clickhouse_users.py
git commit -m "feat(clickhouse): init_user_rights creates user + per-user role"
```

---

## Task 12: init_user_rights — group reconcile

**Files:**
- Modify: `src/iris/clickhouse/users.py`
- Modify: `tests/clickhouse/test_clickhouse_users.py`

- [ ] **Step 12.1: Add the failing tests**

Append to `tests/clickhouse/test_clickhouse_users.py`:

```python
def _grp_roles_for(client, username):
    rows = list(
        client.query(
            "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String}",
            parameters={"u": username},
        ).named_results()
    )
    return {
        row["granted_role_name"]
        for row in rows
        if row["granted_role_name"].endswith(GROUP_ROLE_SUFFIX)
    }


def test_init_user_rights_grants_group_roles(ch_client, ch_settings, prefix):
    username = f"{prefix}_g"
    init_user_rights(
        ch_client,
        username=username,
        groups=["sales", "ops"],
        settings=ch_settings,
    )
    assert _grp_roles_for(ch_client, username) == {"sales_GRP", "ops_GRP"}


def test_init_user_rights_revokes_groups_user_no_longer_has(ch_client, ch_settings, prefix):
    username = f"{prefix}_r"
    init_user_rights(
        ch_client,
        username=username,
        groups=["a", "b"],
        settings=ch_settings,
    )
    assert _grp_roles_for(ch_client, username) == {"a_GRP", "b_GRP"}

    init_user_rights(
        ch_client,
        username=username,
        groups=["b", "c"],
        settings=ch_settings,
    )
    assert _grp_roles_for(ch_client, username) == {"b_GRP", "c_GRP"}


def test_init_user_rights_does_not_touch_user_role_during_reconcile(
    ch_client, ch_settings, prefix
):
    """The per-user `_USER` role must stay granted regardless of `groups` content."""
    username = f"{prefix}_keep"
    init_user_rights(
        ch_client,
        username=username,
        groups=["x"],
        settings=ch_settings,
    )
    init_user_rights(
        ch_client,
        username=username,
        groups=[],
        settings=ch_settings,
    )
    user_role = username + USER_ROLE_SUFFIX
    rows = list(
        ch_client.query(
            "SELECT granted_role_name FROM system.role_grants "
            "WHERE user_name = {u:String} AND granted_role_name = {r:String}",
            parameters={"u": username, "r": user_role},
        ).named_results()
    )
    assert rows == [{"granted_role_name": user_role}]
```

- [ ] **Step 12.2: Run, verify they fail**

```bash
uv run pytest tests/clickhouse/test_clickhouse_users.py -v
```
Expected: 3 failures — group roles not granted (the implementation in Task 11 stops after step 3).

- [ ] **Step 12.3: Add group reconcile to `init_user_rights`**

Replace `init_user_rights` in `src/iris/clickhouse/users.py` with:

```python
def init_user_rights(
    client: Client,
    *,
    username: str,
    groups: list[str],
    settings: ClickHouseSettings,
) -> None:
    """Idempotently provision a CH user, their per-user role, group memberships, and the
    IMPERSONATE grant for the service admin.

    The per-user role (``<username>_USER``) is granted unconditionally and is *not*
    part of the group reconcile — it represents the user's own identity, distinct
    from group membership.
    """
    validate_identifier(username, kind="username")
    for group in groups:
        validate_identifier(group, kind="group")

    user_q = quote_identifier(username, kind="username")
    user_role_q = quote_identifier(username + USER_ROLE_SUFFIX, kind="role")

    client.command(f"CREATE USER IF NOT EXISTS {user_q} IDENTIFIED WITH no_password")
    client.command(f"CREATE ROLE IF NOT EXISTS {user_role_q}")
    client.command(f"GRANT {user_role_q} TO {user_q}")

    desired_grp = {g + GROUP_ROLE_SUFFIX for g in groups}
    rows = client.query(
        "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String}",
        parameters={"u": username},
    ).named_results()
    current_grp = {
        row["granted_role_name"]
        for row in rows
        if row["granted_role_name"].endswith(GROUP_ROLE_SUFFIX)
    }

    for role in current_grp - desired_grp:
        role_q = quote_identifier(role, kind="role")
        client.command(f"REVOKE {role_q} FROM {user_q}")

    for role in desired_grp - current_grp:
        role_q = quote_identifier(role, kind="role")
        client.command(f"CREATE ROLE IF NOT EXISTS {role_q}")
        client.command(f"GRANT {role_q} TO {user_q}")
    # Task 13: append IMPERSONATE grant.
```

- [ ] **Step 12.4: Run, verify they pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_users.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: 8 passed (5 from Task 11 + 3 new); clean.

- [ ] **Step 12.5: Commit**

```bash
git add src/iris/clickhouse/users.py tests/clickhouse/test_clickhouse_users.py
git commit -m "feat(clickhouse): init_user_rights reconciles group memberships"
```

---

## Task 13: init_user_rights — IMPERSONATE grant

**Files:**
- Modify: `src/iris/clickhouse/users.py`
- Modify: `tests/clickhouse/test_clickhouse_users.py`

- [ ] **Step 13.1: Add the failing test**

Append to `tests/clickhouse/test_clickhouse_users.py`:

```python
def test_init_user_rights_grants_impersonate_to_service_admin(
    ch_client, ch_settings, prefix
):
    username = f"{prefix}_imp"
    init_user_rights(ch_client, username=username, groups=[], settings=ch_settings)

    rows = list(
        ch_client.query(
            "SELECT access_type, user_name FROM system.grants "
            "WHERE user_name = {sa:String} AND access_type = 'IMPERSONATE'",
            parameters={"sa": ch_settings.service_admin_user},
        ).named_results()
    )
    # Each call to init_user_rights for a different username adds one IMPERSONATE
    # grant on the service admin user, so we just check the new one is present.
    impersonated = {
        # The exact column name for the impersonated user depends on CH's
        # surfacing of object_target — accept either of the common shapes.
        row.get("object_user") or row.get("user")
        for row in rows
    }
    # If neither key is available, fall back to grepping the raw rows for the username.
    assert username in {*impersonated, *(str(r) for r in rows)}, rows
```

(The exact column naming for IMPERSONATE targets in `system.grants` varies by CH version. The smoke task — Task 16 — pins this; if your CH puts the impersonated identifier somewhere other than `object_user`/`user`, simplify this test once Task 16's smoke output reveals the correct column.)

- [ ] **Step 13.2: Run, verify it fails**

```bash
uv run pytest tests/clickhouse/test_clickhouse_users.py::test_init_user_rights_grants_impersonate_to_service_admin -v
```
Expected: failure — no IMPERSONATE grant present.

- [ ] **Step 13.3: Add IMPERSONATE to `init_user_rights`**

Replace the trailing `# Task 13:` comment in `src/iris/clickhouse/users.py` with:

```python
    service_admin_q = quote_identifier(
        settings.service_admin_user, kind="service_admin_user"
    )
    client.command(f"GRANT IMPERSONATE ON {user_q} TO {service_admin_q}")
```

So the function ends with that block (after the desired/current loops above).

- [ ] **Step 13.4: Run, verify it passes**

```bash
uv run pytest tests/clickhouse/test_clickhouse_users.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: all 9 user tests pass; clean. If the IMPERSONATE statement is rejected by your CH version, the smoke task (Task 16) will surface the correct syntax; adjust the GRANT to the form `GRANT IMPERSONATE({user_q}) ON *.* TO {service_admin_q}` and rerun.

- [ ] **Step 13.5: Commit**

```bash
git add src/iris/clickhouse/users.py tests/clickhouse/test_clickhouse_users.py
git commit -m "feat(clickhouse): init_user_rights grants IMPERSONATE to service admin"
```

---

## Task 14: grant_select_to_database

**Files:**
- Create: `src/iris/clickhouse/grants.py`
- Create: `tests/clickhouse/test_clickhouse_grants.py`

- [ ] **Step 14.1: Write the failing test**

Create `tests/clickhouse/test_clickhouse_grants.py`:

```python
"""Tests for grant_select_to_database and grant_insert_update_to_table."""

from __future__ import annotations

from iris.clickhouse.grants import grant_select_to_database


def test_grant_select_to_database(ch_client, ch_settings, prefix):
    db = f"{prefix}_db"
    role = f"{prefix}_reader"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")

    grant_select_to_database(ch_client, database=db, role=role)

    rows = list(
        ch_client.query(
            "SELECT access_type FROM system.grants "
            "WHERE role_name = {r:String} AND database = {d:String} "
            "AND access_type = 'SELECT'",
            parameters={"r": role, "d": db},
        ).named_results()
    )
    assert rows == [{"access_type": "SELECT"}]


def test_grant_select_to_database_is_idempotent(ch_client, ch_settings, prefix):
    db = f"{prefix}_db_i"
    role = f"{prefix}_reader_i"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")

    grant_select_to_database(ch_client, database=db, role=role)
    grant_select_to_database(ch_client, database=db, role=role)

    n = list(
        ch_client.query(
            "SELECT count() AS n FROM system.grants "
            "WHERE role_name = {r:String} AND database = {d:String} "
            "AND access_type = 'SELECT'",
            parameters={"r": role, "d": db},
        ).named_results()
    )
    assert n == [{"n": 1}]
```

- [ ] **Step 14.2: Run, verify ImportError**

```bash
uv run pytest tests/clickhouse/test_clickhouse_grants.py -v
```
Expected: collection failure.

- [ ] **Step 14.3: Implement `grant_select_to_database`**

Create `src/iris/clickhouse/grants.py`:

```python
"""SQL grant operations on databases and tables."""

from __future__ import annotations

from clickhouse_connect.driver.client import Client

from iris.clickhouse.identifiers import quote_identifier


def grant_select_to_database(client: Client, *, database: str, role: str) -> None:
    """``GRANT SELECT ON <database>.* TO <role>``. Idempotent (CH no-ops on re-grant)."""
    db_q = quote_identifier(database, kind="database")
    role_q = quote_identifier(role, kind="role")
    client.command(f"GRANT SELECT ON {db_q}.* TO {role_q}")
```

- [ ] **Step 14.4: Run, verify they pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_grants.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: 2 passed; clean.

- [ ] **Step 14.5: Commit**

```bash
git add src/iris/clickhouse/grants.py tests/clickhouse/test_clickhouse_grants.py
git commit -m "feat(clickhouse): grant_select_to_database"
```

---

## Task 15: grant_insert_update_to_table

**Files:**
- Modify: `src/iris/clickhouse/grants.py`
- Modify: `tests/clickhouse/test_clickhouse_grants.py`

- [ ] **Step 15.1: Add the failing tests**

Append to `tests/clickhouse/test_clickhouse_grants.py`:

```python
from iris.clickhouse.grants import grant_insert_update_to_table


def test_grant_insert_update_to_table(ch_client, ch_settings, prefix):
    db = f"{prefix}_iu"
    table = "t"
    role = f"{prefix}_writer"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}` (id UInt64, region String) "
        "ENGINE = MergeTree ORDER BY id"
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")

    grant_insert_update_to_table(ch_client, database=db, table=table, role=role)

    rows = list(
        ch_client.query(
            "SELECT access_type FROM system.grants "
            "WHERE role_name = {r:String} AND database = {d:String} AND table = {t:String}",
            parameters={"r": role, "d": db, "t": table},
        ).named_results()
    )
    access_types = {row["access_type"] for row in rows}
    assert "INSERT" in access_types
    # ALTER UPDATE shows up either as 'ALTER UPDATE' or 'UPDATE' depending on CH version.
    assert any("UPDATE" in t for t in access_types), access_types


def test_grant_insert_update_to_table_is_idempotent(ch_client, ch_settings, prefix):
    db = f"{prefix}_iu2"
    table = "t"
    role = f"{prefix}_writer2"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}` (id UInt64) "
        "ENGINE = MergeTree ORDER BY id"
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")

    grant_insert_update_to_table(ch_client, database=db, table=table, role=role)
    grant_insert_update_to_table(ch_client, database=db, table=table, role=role)

    rows = list(
        ch_client.query(
            "SELECT access_type, count() AS n FROM system.grants "
            "WHERE role_name = {r:String} AND database = {d:String} AND table = {t:String} "
            "GROUP BY access_type",
            parameters={"r": role, "d": db, "t": table},
        ).named_results()
    )
    for row in rows:
        assert row["n"] == 1
```

- [ ] **Step 15.2: Run, verify they fail**

```bash
uv run pytest tests/clickhouse/test_clickhouse_grants.py -v
```
Expected: 2 failures — `grant_insert_update_to_table` not defined.

- [ ] **Step 15.3: Implement `grant_insert_update_to_table`**

Append to `src/iris/clickhouse/grants.py`:

```python
def grant_insert_update_to_table(
    client: Client, *, database: str, table: str, role: str
) -> None:
    """``GRANT INSERT`` and ``GRANT ALTER UPDATE`` on ``<database>.<table>`` to ``<role>``.
    Both grants are idempotent."""
    db_q = quote_identifier(database, kind="database")
    table_q = quote_identifier(table, kind="table")
    role_q = quote_identifier(role, kind="role")
    client.command(f"GRANT INSERT ON {db_q}.{table_q} TO {role_q}")
    client.command(f"GRANT ALTER UPDATE ON {db_q}.{table_q} TO {role_q}")
```

- [ ] **Step 15.4: Run, verify they pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_grants.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: 4 passed; clean.

- [ ] **Step 15.5: Commit**

```bash
git add src/iris/clickhouse/grants.py tests/clickhouse/test_clickhouse_grants.py
git commit -m "feat(clickhouse): grant_insert_update_to_table"
```

---

## Task 16: Phase-0 smoke verification

**Files:**
- Modify: `tests/clickhouse/test_clickhouse_smoke.py`

This task runs every DDL the module uses against the testcontainer in one place. If anything fails — particularly the `IMPERSONATE` syntax or the `system.*` query shapes — fix the relevant module function before continuing to row policies and audits.

- [ ] **Step 16.1: Replace the smoke test**

Overwrite `tests/clickhouse/test_clickhouse_smoke.py` with:

```python
"""Phase-0 surface verification: every DDL/audit query the module relies on,
exercised end-to-end against the testcontainer."""

from __future__ import annotations

import uuid


def _u():
    return "smoke_" + uuid.uuid4().hex[:8]


def test_smoke_full_ddl_surface(ch_client):
    user = _u()
    role = f"{user}_USER"
    grp = f"{user}_GRP"
    db = f"{user}_db"
    table = "t"
    admin = "smoke_admin_" + uuid.uuid4().hex[:6]

    # Users / roles
    ch_client.command(f"CREATE USER IF NOT EXISTS `{user}` IDENTIFIED WITH no_password")
    ch_client.command(f"CREATE USER IF NOT EXISTS `{admin}` IDENTIFIED WITH no_password")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{grp}`")
    ch_client.command(f"GRANT `{role}` TO `{user}`")
    ch_client.command(f"GRANT `{grp}` TO `{user}`")
    ch_client.command(f"REVOKE `{grp}` FROM `{user}`")

    # IMPERSONATE — the syntax our spec uses
    ch_client.command(f"GRANT IMPERSONATE ON `{user}` TO `{admin}`")

    # Database, table, row policies
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}` (id UInt64, region String) "
        "ENGINE = MergeTree ORDER BY id"
    )
    ch_client.command(f"GRANT SELECT ON `{db}`.* TO `{role}`")
    ch_client.command(f"GRANT INSERT ON `{db}`.`{table}` TO `{role}`")
    ch_client.command(f"GRANT ALTER UPDATE ON `{db}`.`{table}` TO `{role}`")
    ch_client.command(
        f"CREATE ROW POLICY IF NOT EXISTS `{user}_p1` ON `{db}`.`{table}` "
        f"FOR SELECT USING `region` = 'EU' TO `{role}`"
    )
    ch_client.command(
        f"CREATE ROW POLICY IF NOT EXISTS `{user}_wild` ON `{db}`.`{table}` "
        f"FOR SELECT USING 1 TO `{role}`"
    )
    ch_client.command(f"DROP ROW POLICY IF EXISTS `{user}_p1` ON `{db}`.`{table}`")

    # Every audit query the module uses
    rows = list(
        ch_client.query(
            "SELECT * FROM system.grants WHERE user_name = {u:String}",
            parameters={"u": admin},
        ).named_results()
    )
    assert any(r["access_type"] == "IMPERSONATE" for r in rows), rows

    rows = list(
        ch_client.query(
            "SELECT * FROM system.grants WHERE role_name = {r:String}",
            parameters={"r": role},
        ).named_results()
    )
    assert {row["access_type"] for row in rows} >= {"SELECT", "INSERT"}

    rows = list(
        ch_client.query(
            "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String}",
            parameters={"u": user},
        ).named_results()
    )
    granted = {r["granted_role_name"] for r in rows}
    assert role in granted
    assert grp not in granted

    rows = list(
        ch_client.query(
            "SELECT name FROM system.row_policies "
            "WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": db, "t": table},
        ).named_results()
    )
    names = {r["name"] for r in rows}
    assert any(n.endswith("_wild") for n in names)
    assert not any(n.endswith("_p1") for n in names)


def test_smoke_select_one_via_named_results(ch_client):
    rows = list(ch_client.query("SELECT 1 AS one").named_results())
    assert rows == [{"one": 1}]
```

- [ ] **Step 16.2: Run the smoke test**

```bash
uv run pytest tests/clickhouse/test_clickhouse_smoke.py -v
```
Expected: 2 passed.

If `GRANT IMPERSONATE ON {user} TO {admin}` is rejected by your CH version, replace it in the smoke test with `GRANT IMPERSONATE({user}) ON *.* TO {admin}` and update `init_user_rights` Step 13.3 to match.

- [ ] **Step 16.3: Commit**

```bash
git add tests/clickhouse/test_clickhouse_smoke.py
git commit -m "test(clickhouse): pin DDL surface against the testcontainer"
```

---

## Task 17: add_row_policy

**Files:**
- Create: `src/iris/clickhouse/policies.py`
- Create: `tests/clickhouse/test_clickhouse_policies.py`

- [ ] **Step 17.1: Write the failing tests**

Create `tests/clickhouse/test_clickhouse_policies.py`:

```python
"""Tests for add_row_policy and revoke_row_policy."""

from __future__ import annotations

import pytest

from iris.clickhouse.identifiers import policy_name
from iris.clickhouse.policies import add_row_policy


def _setup_table(ch_client, db, table, role):
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}` (id UInt64, region String) "
        "ENGINE = MergeTree ORDER BY id"
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")


def test_add_row_policy_creates_named_policy_and_wildcard(ch_client, ch_settings, prefix):
    db = f"{prefix}_pol"
    table = "t"
    role = f"{prefix}_writer_pol"
    _setup_table(ch_client, db, table, role)

    add_row_policy(
        ch_client,
        database=db,
        table=table,
        column="region",
        role=role,
        value="EU",
        settings=ch_settings,
    )

    expected_name = policy_name(db, table, role, "EU")
    expected_wildcard = f"{db}_{table}_{ch_settings.service_admin_role}"

    rows = list(
        ch_client.query(
            "SELECT name FROM system.row_policies "
            "WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": db, "t": table},
        ).named_results()
    )
    names = {r["name"] for r in rows}
    assert expected_name in names
    assert expected_wildcard in names


def test_add_row_policy_is_idempotent(ch_client, ch_settings, prefix):
    db = f"{prefix}_pol2"
    table = "t"
    role = f"{prefix}_writer_pol2"
    _setup_table(ch_client, db, table, role)

    add_row_policy(
        ch_client,
        database=db, table=table, column="region", role=role, value="EU",
        settings=ch_settings,
    )
    add_row_policy(
        ch_client,
        database=db, table=table, column="region", role=role, value="EU",
        settings=ch_settings,
    )

    n = list(
        ch_client.query(
            "SELECT count() AS n FROM system.row_policies "
            "WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": db, "t": table},
        ).named_results()
    )
    # exactly two policies: the named one and the wildcard.
    assert n == [{"n": 2}]


def test_add_row_policy_validates_inputs(ch_client, ch_settings):
    from iris.clickhouse.identifiers import InvalidIdentifierError

    with pytest.raises(InvalidIdentifierError):
        add_row_policy(
            ch_client,
            database="bad-db", table="t", column="c", role="r", value="v",
            settings=ch_settings,
        )
    with pytest.raises(InvalidIdentifierError):
        add_row_policy(
            ch_client,
            database="db", table="bad table", column="c", role="r", value="v",
            settings=ch_settings,
        )
    with pytest.raises(InvalidIdentifierError):
        add_row_policy(
            ch_client,
            database="db", table="t", column="bad column", role="r", value="v",
            settings=ch_settings,
        )
    with pytest.raises(InvalidIdentifierError):
        add_row_policy(
            ch_client,
            database="db", table="t", column="c", role="bad role", value="v",
            settings=ch_settings,
        )
```

- [ ] **Step 17.2: Run, verify ImportError**

```bash
uv run pytest tests/clickhouse/test_clickhouse_policies.py -v
```
Expected: collection failure.

- [ ] **Step 17.3: Implement `add_row_policy`**

Create `src/iris/clickhouse/policies.py`:

```python
"""Row-policy CRUD helpers."""

from __future__ import annotations

from clickhouse_connect.driver.client import Client

from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.identifiers import (
    policy_name,
    quote_identifier,
    quote_string,
    validate_identifier,
)


def add_row_policy(
    client: Client,
    *,
    database: str,
    table: str,
    column: str,
    role: str,
    value: str,
    settings: ClickHouseSettings,
) -> None:
    """Create a row policy ``<column> = <value>`` for ``<role>`` on ``<database>.<table>``.

    Also ensures a wildcard ``USING 1`` policy exists for ``settings.service_admin_role``
    so the service admin can read every row regardless of other policies. The wildcard
    name is the constant ``<database>_<table>_<service_admin_role>``; subsequent calls
    are no-ops thanks to ``IF NOT EXISTS``.
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(column, kind="column")
    validate_identifier(role, kind="role")

    db_q = quote_identifier(database, kind="database")
    table_q = quote_identifier(table, kind="table")
    column_q = quote_identifier(column, kind="column")
    role_q = quote_identifier(role, kind="role")

    name = policy_name(database, table, role, value)
    name_q = quote_identifier(name, kind="policy")
    client.command(
        f"CREATE ROW POLICY IF NOT EXISTS {name_q} ON {db_q}.{table_q} "
        f"FOR SELECT USING {column_q} = {quote_string(value)} TO {role_q}"
    )

    sa_role = settings.service_admin_role
    sa_role_q = quote_identifier(sa_role, kind="service_admin_role")
    sa_name = f"{database}_{table}_{sa_role}"
    sa_name_q = quote_identifier(sa_name, kind="policy")
    client.command(
        f"CREATE ROW POLICY IF NOT EXISTS {sa_name_q} ON {db_q}.{table_q} "
        f"FOR SELECT USING 1 TO {sa_role_q}"
    )
```

- [ ] **Step 17.4: Run, verify they pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_policies.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: 3 passed; clean.

- [ ] **Step 17.5: Commit**

```bash
git add src/iris/clickhouse/policies.py tests/clickhouse/test_clickhouse_policies.py
git commit -m "feat(clickhouse): add_row_policy with wildcard service-admin policy"
```

---

## Task 18: revoke_row_policy

**Files:**
- Modify: `src/iris/clickhouse/policies.py`
- Modify: `tests/clickhouse/test_clickhouse_policies.py`

- [ ] **Step 18.1: Add the failing tests**

Append to `tests/clickhouse/test_clickhouse_policies.py`:

```python
from iris.clickhouse.policies import revoke_row_policy


def test_revoke_row_policy_drops_named_policy(ch_client, ch_settings, prefix):
    db = f"{prefix}_rev"
    table = "t"
    role = f"{prefix}_writer_rev"
    _setup_table(ch_client, db, table, role)

    add_row_policy(
        ch_client,
        database=db, table=table, column="region", role=role, value="EU",
        settings=ch_settings,
    )
    revoke_row_policy(ch_client, database=db, table=table, role=role, value="EU")

    expected_name = policy_name(db, table, role, "EU")
    rows = list(
        ch_client.query(
            "SELECT name FROM system.row_policies "
            "WHERE database = {d:String} AND table = {t:String} AND name = {n:String}",
            parameters={"d": db, "t": table, "n": expected_name},
        ).named_results()
    )
    assert rows == []


def test_revoke_row_policy_does_not_drop_service_admin_wildcard(
    ch_client, ch_settings, prefix
):
    db = f"{prefix}_rev2"
    table = "t"
    role = f"{prefix}_writer_rev2"
    _setup_table(ch_client, db, table, role)

    add_row_policy(
        ch_client,
        database=db, table=table, column="region", role=role, value="EU",
        settings=ch_settings,
    )
    revoke_row_policy(ch_client, database=db, table=table, role=role, value="EU")

    wildcard = f"{db}_{table}_{ch_settings.service_admin_role}"
    rows = list(
        ch_client.query(
            "SELECT name FROM system.row_policies "
            "WHERE database = {d:String} AND table = {t:String} AND name = {n:String}",
            parameters={"d": db, "t": table, "n": wildcard},
        ).named_results()
    )
    assert rows == [{"name": wildcard}]


def test_revoke_row_policy_is_idempotent(ch_client, ch_settings, prefix):
    db = f"{prefix}_rev3"
    table = "t"
    role = f"{prefix}_writer_rev3"
    _setup_table(ch_client, db, table, role)

    add_row_policy(
        ch_client,
        database=db, table=table, column="region", role=role, value="EU",
        settings=ch_settings,
    )
    revoke_row_policy(ch_client, database=db, table=table, role=role, value="EU")
    # second call is a no-op.
    revoke_row_policy(ch_client, database=db, table=table, role=role, value="EU")
```

- [ ] **Step 18.2: Run, verify they fail**

```bash
uv run pytest tests/clickhouse/test_clickhouse_policies.py -v
```
Expected: 3 failures — `revoke_row_policy` not defined.

- [ ] **Step 18.3: Implement `revoke_row_policy`**

Append to `src/iris/clickhouse/policies.py`:

```python
def revoke_row_policy(
    client: Client,
    *,
    database: str,
    table: str,
    role: str,
    value: str,
) -> None:
    """Drop the named row policy created by ``add_row_policy(database, table, column, role, value)``.

    The wildcard service-admin policy is *not* dropped — it's a singleton per
    ``(database, table, service_admin_role)`` triple and may still apply to other
    policies on the same table.
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(role, kind="role")

    db_q = quote_identifier(database, kind="database")
    table_q = quote_identifier(table, kind="table")
    name_q = quote_identifier(policy_name(database, table, role, value), kind="policy")
    client.command(f"DROP ROW POLICY IF EXISTS {name_q} ON {db_q}.{table_q}")
```

- [ ] **Step 18.4: Run, verify they pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_policies.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: 6 passed; clean.

- [ ] **Step 18.5: Commit**

```bash
git add src/iris/clickhouse/policies.py tests/clickhouse/test_clickhouse_policies.py
git commit -m "feat(clickhouse): revoke_row_policy"
```

---

## Task 19: audit — user_grants and role_grants

**Files:**
- Create: `src/iris/clickhouse/audit.py`
- Create: `tests/clickhouse/test_clickhouse_audit.py`

- [ ] **Step 19.1: Write the failing tests**

Create `tests/clickhouse/test_clickhouse_audit.py`:

```python
"""Tests for audit functions."""

from __future__ import annotations

import pytest

from iris.clickhouse.audit import role_grants, user_grants
from iris.clickhouse.grants import grant_select_to_database
from iris.clickhouse.identifiers import InvalidIdentifierError
from iris.clickhouse.users import init_user_rights


def test_user_grants_lists_user_grants(ch_client, ch_settings, prefix):
    username = f"{prefix}_aud_u"
    init_user_rights(ch_client, username=username, groups=[], settings=ch_settings)

    rows = user_grants(ch_client, username=username)
    # The user has no direct grants yet (their per-user role does, not the user).
    # Just verify the call succeeds and returns a list.
    assert isinstance(rows, list)


def test_role_grants_lists_role_grants(ch_client, ch_settings, prefix):
    db = f"{prefix}_aud_db"
    role = f"{prefix}_aud_role"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    grant_select_to_database(ch_client, database=db, role=role)

    rows = role_grants(ch_client, role=role)
    select_grant = next(
        (r for r in rows if r["access_type"] == "SELECT" and r["database"] == db),
        None,
    )
    assert select_grant is not None, rows


def test_audit_validates_inputs(ch_client):
    with pytest.raises(InvalidIdentifierError):
        user_grants(ch_client, username="bad name")
    with pytest.raises(InvalidIdentifierError):
        role_grants(ch_client, role="bad role")
```

- [ ] **Step 19.2: Run, verify ImportError**

```bash
uv run pytest tests/clickhouse/test_clickhouse_audit.py -v
```
Expected: collection failure.

- [ ] **Step 19.3: Implement the first two audit functions**

Create `src/iris/clickhouse/audit.py`:

```python
"""Audit helpers — read-only queries over ClickHouse's RBAC system tables."""

from __future__ import annotations

from typing import Any

from clickhouse_connect.driver.client import Client

from iris.clickhouse.identifiers import validate_identifier


def user_grants(client: Client, *, username: str) -> list[dict[str, Any]]:
    """All direct grants on the named user (does not include grants inherited via roles)."""
    validate_identifier(username, kind="username")
    return list(
        client.query(
            "SELECT * FROM system.grants WHERE user_name = {u:String}",
            parameters={"u": username},
        ).named_results()
    )


def role_grants(client: Client, *, role: str) -> list[dict[str, Any]]:
    """All grants attached to the named role."""
    validate_identifier(role, kind="role")
    return list(
        client.query(
            "SELECT * FROM system.grants WHERE role_name = {r:String}",
            parameters={"r": role},
        ).named_results()
    )
```

- [ ] **Step 19.4: Run, verify they pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_audit.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: 3 passed; clean.

- [ ] **Step 19.5: Commit**

```bash
git add src/iris/clickhouse/audit.py tests/clickhouse/test_clickhouse_audit.py
git commit -m "feat(clickhouse): user_grants and role_grants audit"
```

---

## Task 20: audit — user_role_memberships

**Files:**
- Modify: `src/iris/clickhouse/audit.py`
- Modify: `tests/clickhouse/test_clickhouse_audit.py`

- [ ] **Step 20.1: Add the failing test**

Append to `tests/clickhouse/test_clickhouse_audit.py`:

```python
from iris.clickhouse.audit import user_role_memberships


def test_user_role_memberships(ch_client, ch_settings, prefix):
    username = f"{prefix}_mem"
    init_user_rights(
        ch_client,
        username=username,
        groups=["alpha", "beta"],
        settings=ch_settings,
    )

    rows = user_role_memberships(ch_client, username=username)
    granted = {r["granted_role_name"] for r in rows}
    assert f"{username}_USER" in granted
    assert "alpha_GRP" in granted
    assert "beta_GRP" in granted
```

- [ ] **Step 20.2: Run, verify it fails**

```bash
uv run pytest tests/clickhouse/test_clickhouse_audit.py -v
```
Expected: 1 failure — `user_role_memberships` not defined.

- [ ] **Step 20.3: Implement `user_role_memberships`**

Append to `src/iris/clickhouse/audit.py`:

```python
def user_role_memberships(client: Client, *, username: str) -> list[dict[str, Any]]:
    """All roles granted to the named user (per-user role + group roles)."""
    validate_identifier(username, kind="username")
    return list(
        client.query(
            "SELECT * FROM system.role_grants WHERE user_name = {u:String}",
            parameters={"u": username},
        ).named_results()
    )
```

- [ ] **Step 20.4: Run, verify they pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_audit.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: 4 passed; clean.

- [ ] **Step 20.5: Commit**

```bash
git add src/iris/clickhouse/audit.py tests/clickhouse/test_clickhouse_audit.py
git commit -m "feat(clickhouse): user_role_memberships audit"
```

---

## Task 21: audit — user_row_policies and role_row_policies

**Files:**
- Modify: `src/iris/clickhouse/audit.py`
- Modify: `tests/clickhouse/test_clickhouse_audit.py`

- [ ] **Step 21.1: Add the failing tests**

Append to `tests/clickhouse/test_clickhouse_audit.py`:

```python
from iris.clickhouse.audit import role_row_policies, user_row_policies
from iris.clickhouse.policies import add_row_policy


def _setup_policy_for_role(ch_client, ch_settings, prefix_db, role):
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{prefix_db}`")
    ch_client.command(
        f"CREATE TABLE IF NOT EXISTS `{prefix_db}`.`t` (id UInt64, region String) "
        "ENGINE = MergeTree ORDER BY id"
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    add_row_policy(
        ch_client,
        database=prefix_db, table="t", column="region", role=role, value="EU",
        settings=ch_settings,
    )


def test_role_row_policies(ch_client, ch_settings, prefix):
    db = f"{prefix}_rrp_db"
    role = f"{prefix}_rrp_role"
    _setup_policy_for_role(ch_client, ch_settings, db, role)

    rows = role_row_policies(ch_client, role=role)
    assert any(r["database"] == db for r in rows), rows


def test_user_row_policies(ch_client, ch_settings, prefix):
    db = f"{prefix}_urp_db"
    role = f"{prefix}_urp_role"
    user = f"{prefix}_urp_user"
    _setup_policy_for_role(ch_client, ch_settings, db, role)
    ch_client.command(f"CREATE USER IF NOT EXISTS `{user}` IDENTIFIED WITH no_password")
    ch_client.command(f"GRANT `{role}` TO `{user}`")

    rows = user_row_policies(ch_client, username=user)
    assert any(r["database"] == db for r in rows), rows
```

- [ ] **Step 21.2: Run, verify they fail**

```bash
uv run pytest tests/clickhouse/test_clickhouse_audit.py -v
```
Expected: 2 failures.

- [ ] **Step 21.3: Implement both audit functions**

Append to `src/iris/clickhouse/audit.py`:

```python
def role_row_policies(client: Client, *, role: str) -> list[dict[str, Any]]:
    """All row policies that apply to the named role.

    ``system.row_policies.apply_to_list`` is an ``Array(String)`` containing the
    grantee role/user names; ``has(...)`` filters rows where ``role`` is present.
    """
    validate_identifier(role, kind="role")
    return list(
        client.query(
            "SELECT * FROM system.row_policies WHERE has(apply_to_list, {r:String})",
            parameters={"r": role},
        ).named_results()
    )


def user_row_policies(client: Client, *, username: str) -> list[dict[str, Any]]:
    """All row policies that apply to the named user.

    Joins ``system.row_policies`` with the user's role memberships so policies
    granted via group roles are included alongside any policies attached
    directly to the username.
    """
    validate_identifier(username, kind="username")
    return list(
        client.query(
            """
            SELECT rp.*
            FROM system.row_policies AS rp
            ARRAY JOIN apply_to_list AS grantee
            WHERE grantee = {u:String}
               OR grantee IN (
                   SELECT granted_role_name FROM system.role_grants
                   WHERE user_name = {u:String}
               )
            """,
            parameters={"u": username},
        ).named_results()
    )
```

- [ ] **Step 21.4: Run, verify they pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_audit.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: 6 passed; clean.

- [ ] **Step 21.5: Commit**

```bash
git add src/iris/clickhouse/audit.py tests/clickhouse/test_clickhouse_audit.py
git commit -m "feat(clickhouse): user_row_policies and role_row_policies audit"
```

---

## Task 22: audit — table_row_policies

**Files:**
- Modify: `src/iris/clickhouse/audit.py`
- Modify: `tests/clickhouse/test_clickhouse_audit.py`

- [ ] **Step 22.1: Add the failing test**

Append to `tests/clickhouse/test_clickhouse_audit.py`:

```python
from iris.clickhouse.audit import table_row_policies


def test_table_row_policies(ch_client, ch_settings, prefix):
    db = f"{prefix}_trp_db"
    role = f"{prefix}_trp_role"
    _setup_policy_for_role(ch_client, ch_settings, db, role)

    rows = table_row_policies(ch_client, database=db, table="t")
    assert any(r["database"] == db and r["table"] == "t" for r in rows), rows
```

- [ ] **Step 22.2: Run, verify it fails**

```bash
uv run pytest tests/clickhouse/test_clickhouse_audit.py -v
```
Expected: 1 failure.

- [ ] **Step 22.3: Implement `table_row_policies`**

Append to `src/iris/clickhouse/audit.py`:

```python
def table_row_policies(
    client: Client, *, database: str, table: str
) -> list[dict[str, Any]]:
    """All row policies attached to the given table."""
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    return list(
        client.query(
            "SELECT * FROM system.row_policies "
            "WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": database, "t": table},
        ).named_results()
    )
```

- [ ] **Step 22.4: Run, verify it passes**

```bash
uv run pytest tests/clickhouse/test_clickhouse_audit.py -v
uv run ruff check
uv run basedpyright --level error
```
Expected: 7 passed; clean.

- [ ] **Step 22.5: Commit**

```bash
git add src/iris/clickhouse/audit.py tests/clickhouse/test_clickhouse_audit.py
git commit -m "feat(clickhouse): table_row_policies audit"
```

---

## Task 23: Public surface (`__init__.py`)

**Files:**
- Modify: `src/iris/clickhouse/__init__.py`

- [ ] **Step 23.1: Write the failing test**

Append to `tests/clickhouse/test_clickhouse_identifiers.py` (the file already exists; this is a placement-of-convenience for a single test that doesn't fit elsewhere):

```python
def test_public_surface_exports_named_symbols():
    import iris.clickhouse as ch

    expected = {
        "ClickHouseSettings",
        "build_client",
        "ensure_service_admin",
        "init_user_rights",
        "grant_select_to_database",
        "grant_insert_update_to_table",
        "add_row_policy",
        "revoke_row_policy",
        "user_grants",
        "role_grants",
        "user_role_memberships",
        "user_row_policies",
        "role_row_policies",
        "table_row_policies",
    }
    assert set(ch.__all__) == expected
    for name in expected:
        assert hasattr(ch, name), name
```

- [ ] **Step 23.2: Run, verify it fails**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py::test_public_surface_exports_named_symbols -v
```
Expected: failure — `__all__` is undefined or the docstring-only module exports nothing.

- [ ] **Step 23.3: Populate the public surface**

Replace `src/iris/clickhouse/__init__.py` with:

```python
"""ClickHouse provisioning and audit helpers.

Public surface — see ``CLAUDE.md`` for usage. The package is independent of
``iris.auth``: it takes plain-data inputs (usernames as strings, group names as
lists) and is invoked by future code that bridges auth → clickhouse.
"""

from iris.clickhouse.audit import (
    role_grants,
    role_row_policies,
    table_row_policies,
    user_grants,
    user_role_memberships,
    user_row_policies,
)
from iris.clickhouse.bootstrap import ensure_service_admin
from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.grants import (
    grant_insert_update_to_table,
    grant_select_to_database,
)
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
from iris.clickhouse.users import init_user_rights

__all__ = [
    "ClickHouseSettings",
    "build_client",
    "ensure_service_admin",
    "init_user_rights",
    "grant_select_to_database",
    "grant_insert_update_to_table",
    "add_row_policy",
    "revoke_row_policy",
    "user_grants",
    "role_grants",
    "user_role_memberships",
    "user_row_policies",
    "role_row_policies",
    "table_row_policies",
]
```

- [ ] **Step 23.4: Run, verify it passes**

```bash
uv run pytest tests/clickhouse/ -v
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: every clickhouse test passes; ruff clean; basedpyright clean at both levels.

- [ ] **Step 23.5: Commit**

```bash
git add src/iris/clickhouse/__init__.py tests/clickhouse/test_clickhouse_identifiers.py
git commit -m "feat(clickhouse): expose public surface in __init__.py"
```

---

## Task 24: CLAUDE.md — document the module

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 24.1: Append a `## ClickHouse` section**

Append the following section to `CLAUDE.md` (after the existing `## Authentication` section; before any later section, or at end-of-file if Authentication is last):

````markdown
## ClickHouse

The `iris.clickhouse` package provisions ClickHouse users, roles, grants, and row policies, and provides audit-query helpers. It is independent of `iris.auth` — operations take plain strings and lists, not `Session`/`User` objects.

### Public surface

```python
from iris.clickhouse import (
    ClickHouseSettings, build_client, ensure_service_admin,
    init_user_rights,
    grant_select_to_database, grant_insert_update_to_table,
    add_row_policy, revoke_row_policy,
    user_grants, role_grants, user_role_memberships,
    user_row_policies, role_row_policies, table_row_policies,
)
```

`build_client(settings)` returns a `clickhouse_connect.driver.client.Client`. Operations take that client as their first argument:

```python
settings = ClickHouseSettings.from_env()
client = build_client(settings)
ensure_service_admin(client, settings)               # idempotent startup
init_user_rights(client, username="alice", groups=["sales"], settings=settings)
add_row_policy(client, database="orders", table="lines",
               column="region", role="alice_USER", value="EU", settings=settings)
```

### Conventions

- Per-user role: `<username>_USER` (suffix is hardcoded at `users.USER_ROLE_SUFFIX`).
- Per-group role: `<group>_GRP` (suffix is hardcoded at `users.GROUP_ROLE_SUFFIX`).
- Row-policy name: `<database>_<table>_<role>_<slug>_<8charhash>` — slug strips non-`[a-zA-Z0-9_]`, hash disambiguates collisions like `EU/UK` vs `EU UK`.
- Wildcard service-admin policy per table: `<database>_<table>_<service_admin_role>` — `USING 1` applied to the role configured in `CLICKHOUSE_SERVICE_ADMIN_ROLE`. Created by `add_row_policy` if missing; *not* dropped by `revoke_row_policy`.
- All operations are idempotent: re-running is safe. `init_user_rights` reconciles group memberships (revokes `_GRP` roles no longer in the input, grants the new ones).

### DDL safety

`identifiers.py` is the single safety contract. External-source strings (usernames from auth, db/table/column names from callers) flow through `validate_identifier` (rejects anything outside `[a-zA-Z0-9_]+`) and `quote_identifier` (validates + backticks). Row-policy values use `quote_string` for SQL literal escaping. DDL is built from these helpers; `client.command()` runs it without parameter binding. DML (audit `SELECT`s) uses ClickHouse's native `{name:Type}` placeholder syntax via `client.query(..., parameters=...)`.

### Configuration

Env vars (loaded at `import` time via `python-dotenv` from `.env`):

```
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=iris_service          # CH login iris connects as
CLICKHOUSE_PASSWORD=replace-me
CLICKHOUSE_SECURE=true                # https
CLICKHOUSE_VERIFY=true                # TLS verification
# CLICKHOUSE_CA_CERT_PATH=/etc/ssl/certs/ca-bundle.crt

CLICKHOUSE_SERVICE_ADMIN_USER=iris_service       # IMPERSONATE grantee, normally = CLICKHOUSE_USER
CLICKHOUSE_SERVICE_ADMIN_ROLE=service_admin_role # wildcard-policy grantee; granted to admin user at startup
```

`ClickHouseSettings.from_env()` validates everything at app construction — missing required vars, typo'd booleans (`COOKIE_SECURE=ture` style), non-int ports, and bad identifier names all fail loudly.

### Tests

The test suite uses `testcontainers-python` to spin up `clickhouse/clickhouse-server` in Docker. The container is session-scoped (one instance per pytest run); per-test isolation comes from a UUID-derived `prefix` fixture that namespaces every entity name. Docker is required to run `tests/clickhouse/`.

The `chdb` library was originally trialed for in-process testing; `chdb==4.1.6`'s embedded server hardcodes `system.user_directories` to a read-only `users_xml` entry, blocking all RBAC DDL at runtime. See the design spec at `docs/superpowers/specs/2026-05-05-clickhouse-authz-design.md` for the verification.

### Deferred (v1.1+)

- HTTP routes that call these functions on behalf of authenticated users.
- Wiring between `iris.auth` and `iris.clickhouse` (the bridge that translates `Session.user`/`Session.roles` into `init_user_rights` calls).
- A runtime `execute_as(username, sql)` helper for impersonating queries.
- Connection pooling and multi-worker session sharing.
````

- [ ] **Step 24.2: Verify CLAUDE.md is well-formed**

```bash
uv run python -c "open('CLAUDE.md').read()"
```
(Trivial sanity check that the file is readable. The format itself is markdown — no test runner.)

- [ ] **Step 24.3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(clickhouse): document the iris.clickhouse module"
```

---

## Final verification

After all 24 tasks, run the full project test suite plus type and lint checks:

- [ ] **Final 1: Full test suite**

```bash
uv run pytest -v
```
Expected: every test passes (existing auth tests + new clickhouse tests). The clickhouse tests require Docker; if Docker is unavailable, the testcontainer fixture will fail at session start and every clickhouse test will error — that is expected outside CI.

- [ ] **Final 2: Lint and type-check**

```bash
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: clean output for all three.

- [ ] **Final 3: Inspect the diff**

```bash
git log --oneline main..HEAD
git diff main..HEAD --stat
```
Expected: ~24 commits, scoped to `src/iris/clickhouse/`, `tests/clickhouse/`, `pyproject.toml`, `uv.lock`, `.env`, `CLAUDE.md`. No incidental changes to `src/iris/auth/` or the existing `src/iris/app.py`.
