# Remove `_grant_full_admin` testcontainer fallback — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the testcontainer-specific `NAMED COLLECTION ADMIN` accommodation out of `iris.clickhouse.bootstrap._grant_full_admin` and into `tests/clickhouse/conftest.py`. Production code becomes a clean `GRANT ALL ON *.* WITH GRANT OPTION` with no try/except.

**Architecture:** The conftest currently connects to the testcontainer as the XML-defined `test` user (which lacks `NAMED COLLECTION ADMIN`) and runs ~10 enumerated GRANTs on `iris_svc`, ending with `GRANT CURRENT GRANTS` as a catch-all that delegates only what `test` itself holds. Switch to connecting as `default` (the SQL-management superuser, enabled by `CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1`) and issue a single `GRANT ALL ON *.* TO iris_svc WITH GRANT OPTION`. iris_svc then holds the full privilege set, including `NAMED COLLECTION ADMIN`, so iris's `bootstrap_admin._grant_full_admin` succeeds with the plain `GRANT ALL` and no fallback is needed.

**Tech Stack:** Python 3.13, pytest, testcontainers (ClickHouse), clickhouse-connect.

**Spec:** `docs/superpowers/specs/2026-05-09-remove-grant-all-fallback-design.md`.

**Conventions you must respect:**
- `uv run pytest`, `uv run ruff check`, `uv run basedpyright --level error`, `uv run basedpyright --level warning` — ALL must be clean.
- The project's `reportImplicitStringConcatenation` rule forbids adjacent string literals on consecutive lines (see `CLAUDE.md`); collapse to a single string, explicit `+`, or hoisted variable when needed.
- Tests live under `tests/`. NO `__init__.py` files anywhere under `tests/`. Test file basenames must be unique across the suite.
- Each task = one commit. Use the exact commit messages specified.

---

## File map

| File | Change |
|---|---|
| `tests/clickhouse/conftest.py` | Replace the `test`-user-driven enumerated GRANTs with a `default`-user-driven single `GRANT ALL ON *.* TO iris_svc WITH GRANT OPTION`. The structural pieces (CREATE USER, the fixture itself) stay. |
| `tests/clickhouse/test_conftest_grants.py` *(new)* | One smoke test pinning iris_svc's `NAMED COLLECTION ADMIN` privilege so a future testcontainer regression fires loudly. |
| `src/iris/clickhouse/bootstrap.py` | Drop the try/except in `_grant_full_admin`; drop the now-unused `DatabaseError` import. |

---

## Task 1 — Switch conftest to the `default`-user superuser pattern + add smoke test

The conftest's `ch_container` fixture currently enumerates ~10 GRANTs as the `test` user. Replace that whole block with one connection-as-`default` plus one `GRANT ALL`. Add a smoke test that asserts the resulting iris_svc has `NAMED COLLECTION ADMIN`.

**Files:**
- Modify: `tests/clickhouse/conftest.py:29-129` (the `ch_container` fixture body)
- Create: `tests/clickhouse/test_conftest_grants.py`

- [ ] **Step 1: Write the smoke test (will fail until the conftest is rewritten)**

Create `tests/clickhouse/test_conftest_grants.py`:

```python
"""Pin the testcontainer's iris_svc privilege set.

If a future testcontainer image, env-var change, or conftest edit ever
strips iris_svc of NAMED COLLECTION ADMIN, iris's bootstrap_admin's
``GRANT ALL ON *.*`` will start failing with an opaque CH error. This
smoke test pins the privilege so the regression fires before any of the
bootstrap-using tests do.
"""
from __future__ import annotations


def test_iris_svc_has_named_collection_admin(ch_client):
    """iris_svc must hold NAMED COLLECTION ADMIN. The conftest grants it
    via ``GRANT ALL`` from the default-user superuser; if that ever
    stops working, this test fails before bootstrap_admin's GRANT ALL
    does."""
    rows = ch_client.query(
        "SELECT count() FROM system.grants WHERE user_name = 'iris_svc' AND access_type = 'NAMED COLLECTION ADMIN'"
    ).result_rows
    assert rows[0][0] >= 1, (
        "iris_svc lost NAMED COLLECTION ADMIN — bootstrap_admin will fail"
    )
```

- [ ] **Step 2: Run the smoke test to verify it fails (today's iris_svc lacks the privilege)**

Run: `uv run pytest tests/clickhouse/test_conftest_grants.py -v 2>&1 | tail -10`
Expected: FAIL — assertion fires because iris_svc currently inherits via `CURRENT GRANTS` from `test`, and `test` doesn't hold NAMED COLLECTION ADMIN.

- [ ] **Step 3: Rewrite the `ch_container` fixture in `tests/clickhouse/conftest.py`**

Replace the entire `ch_container` fixture body (the whole `with container as ch:` block from `host = ch.get_container_host_ip()` through `yield ch`) with the simpler default-user setup. The credentials constants and the fixture shell stay; only the inner setup changes.

The full new fixture body:

```python
@pytest.fixture(scope="session")
def ch_container():
    """One ClickHouse server per test session.

    Connects as ``default`` (the SQL access-management superuser, enabled
    by ``CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1``) to create a SQL-managed
    ``iris_svc`` user with full privileges via a single ``GRANT ALL``.

    iris_svc must hold ``NAMED COLLECTION ADMIN`` (and every other
    server-scope privilege ``GRANT ALL`` covers) so that iris's
    ``bootstrap_admin._grant_full_admin`` can run a plain
    ``GRANT ALL ON *.*`` against iris's admin role with no fallback.
    """
    container = ClickHouseContainer("clickhouse/clickhouse-server:26.3").with_env(
        "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT", "1"
    )
    with container as ch:
        host = ch.get_container_host_ip()
        port = int(ch.get_exposed_port(8123))
        # Connect as ``default`` (no password). With access management
        # enabled, ``default`` can issue any GRANT, including server-scope
        # rarities like NAMED COLLECTION ADMIN that the XML-defined
        # ``test`` user lacks.
        admin = clickhouse_connect.get_client(
            host=host,
            port=port,
            username="default",
            password="",
            secure=False,
            verify=False,
        )
        try:
            admin.command(
                f"CREATE USER IF NOT EXISTS {_SVC_USER} IDENTIFIED BY '{_SVC_PASSWORD}'"
            )
            admin.command(
                f"GRANT ALL ON *.* TO {_SVC_USER} WITH GRANT OPTION"
            )
        finally:
            admin.close()
        yield ch
```

The unchanged surrounding pieces in this file: the docstring at the top, the `_SVC_USER`/`_SVC_PASSWORD` constants, the `ch_settings`/`ch_client`/`prefix` fixtures further down. Don't touch those.

- [ ] **Step 4: Run the smoke test to verify it now passes**

Run: `uv run pytest tests/clickhouse/test_conftest_grants.py -v 2>&1 | tail -5`
Expected: PASS.

- [ ] **Step 5: Run the full ClickHouse test suite to confirm no regression from the simpler grants**

Run: `uv run pytest tests/clickhouse/ -x 2>&1 | tail -5`
Expected: every test passes. The replaced enumerated GRANTs are all subsumed by `GRANT ALL`.

- [ ] **Step 6: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add tests/clickhouse/conftest.py tests/clickhouse/test_conftest_grants.py
git commit -m "test(conftest): grant iris_svc full privileges via the default superuser"
```

---

## Task 2 — Drop the testcontainer fallback in `_grant_full_admin`

With iris_svc now holding `NAMED COLLECTION ADMIN`, iris's `_grant_full_admin` can issue a clean `GRANT ALL` and never hit the fallback. Remove the try/except. Remove the `DatabaseError` import (no longer used in this file).

**Files:**
- Modify: `src/iris/clickhouse/bootstrap.py:23` (the import) and `:54-64` (the function)

- [ ] **Step 1: Remove the try/except + the unused import**

In `src/iris/clickhouse/bootstrap.py`, replace the imports block + the `_grant_full_admin` function. The before/after:

Before (lines 17-27 + 54-64):
```python
from __future__ import annotations

import logging
from typing import cast

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import DatabaseError

from iris.clickhouse.identifiers import quote_identifier
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX

# ... (rest of file unchanged) ...

def _grant_full_admin(client: Client, *, role_q: str) -> None:
    """``GRANT ALL ON *.* WITH GRANT OPTION``, with a ``CURRENT GRANTS`` fallback
    for the testcontainer's missing NAMED COLLECTION ADMIN privilege."""
    try:
        client.command(f"GRANT ALL ON *.* TO {role_q} WITH GRANT OPTION")
    except DatabaseError as err:
        if "NAMED COLLECTION ADMIN" not in str(err):
            raise
        client.command(
            f"GRANT CURRENT GRANTS ON *.* TO {role_q} WITH GRANT OPTION"
        )
```

After:
```python
from __future__ import annotations

import logging
from typing import cast

from clickhouse_connect.driver.client import Client

from iris.clickhouse.identifiers import quote_identifier
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX

# ... (rest of file unchanged) ...

def _grant_full_admin(client: Client, *, role_q: str) -> None:
    """``GRANT ALL ON *.* WITH GRANT OPTION`` to the iris admin role.

    Requires the connecting client to hold every privilege ``GRANT ALL``
    expands to — including server-scope rarities like ``NAMED COLLECTION
    ADMIN`` — with grant option. The iris service identity in production
    is configured with this; in tests the conftest's default-user setup
    grants it via ``GRANT ALL ON *.* TO iris_svc WITH GRANT OPTION``.
    """
    client.command(f"GRANT ALL ON *.* TO {role_q} WITH GRANT OPTION")
```

- [ ] **Step 2: Run the bootstrap-using tests**

Run: `uv run pytest tests/clickhouse/test_bootstrap_admin.py tests/clickhouse/test_install.py -v 2>&1 | tail -10`
Expected: all tests pass. With Task 1's iris_svc holding NAMED COLLECTION ADMIN, the plain `GRANT ALL` now succeeds — no fallback needed.

- [ ] **Step 3: Run the full pytest suite to confirm no regression**

Run: `uv run pytest -x 2>&1 | tail -5`
Expected: all tests pass.

- [ ] **Step 4: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean. The unused-import gate would have flagged the stale `DatabaseError` import had we not removed it.

- [ ] **Step 5: Confirm no other test-specific accommodations remain in `bootstrap.py`**

Quick visual scan: `grep -nE "test|fallback|except|HACK|workaround" src/iris/clickhouse/bootstrap.py`
Expected output: nothing matching `except|fallback|HACK|workaround` (the only "test" hit should be in unrelated docstring text or the GLOBAL_ADMIN_ROLE constant context).

- [ ] **Step 6: Commit**

```bash
git add src/iris/clickhouse/bootstrap.py
git commit -m "refactor(bootstrap): drop testcontainer-specific GRANT ALL fallback"
```

---

## Final verification

After both tasks land:

- [ ] **Run the entire suite from clean.**

```bash
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
uv run pytest
```

Expected: all clean, all green.

- [ ] **Skim `git log --oneline main..HEAD`** — should be 2 commits, each with a descriptive subject.

- [ ] **Sanity-check the production code shape:**

```bash
grep -A 5 "def _grant_full_admin" src/iris/clickhouse/bootstrap.py
```

You should see a one-line body (`client.command(...)`) under the docstring. No try/except.

---

## Self-review notes

| Spec section | Tasks |
|---|---|
| Move testcontainer accommodation to conftest | Task 1 |
| Use the `default`-user superuser path | Task 1 |
| Single `GRANT ALL` replaces the enumerated grants | Task 1 |
| Smoke test pinning NAMED COLLECTION ADMIN | Task 1 |
| Drop the try/except in `_grant_full_admin` | Task 2 |
| Drop the `DatabaseError` import | Task 2 |
| Risk: `default` lacks NAMED COLLECTION ADMIN | Mitigated by Task 1 Step 4 — smoke test must pass before Task 2 lands; if it doesn't, escalate (the spec calls out the fallback strategy) |

No placeholders. No "TBD". `_grant_full_admin`, `iris_svc`, `GRANT ALL ON *.* WITH GRANT OPTION`, and the `default` user are referenced consistently. Tasks are ordered so the conftest change ships first (with its own smoke test passing) before the production code starts depending on it.
