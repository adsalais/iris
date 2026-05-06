"""First-install bootstrap for the authz tables.

install_authz_schema(conn, settings) detects whether authz_roles already
exists. If it doesn't, the function creates the schema AND seeds the
configured admin role with `clickhouse_admin` as an include and the
configured user as a member. If the table already exists, only the
schema is (idempotently) ensured — no content is touched.

The string "clickhouse_admin" is hardcoded here. It MUST match the
constant `CLICKHOUSE_ADMIN_ROLE` in `iris.clickhouse.deps`. The drift
check in tests/auth/authz/test_authz_bootstrap.py asserts equality.
"""
from __future__ import annotations

import sqlite3
from typing import Protocol

# Must match iris.clickhouse.deps.CLICKHOUSE_ADMIN_ROLE.
_CLICKHOUSE_ADMIN_ROLE = "clickhouse_admin"

_AUTHZ_SCHEMA = """
CREATE TABLE IF NOT EXISTS authz_roles (
    name TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS authz_role_groups (
    role_name  TEXT NOT NULL,
    group_name TEXT NOT NULL,
    PRIMARY KEY (role_name, group_name),
    FOREIGN KEY (role_name) REFERENCES authz_roles(name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS authz_role_users (
    role_name      TEXT NOT NULL,
    username_lower TEXT NOT NULL,
    PRIMARY KEY (role_name, username_lower),
    FOREIGN KEY (role_name) REFERENCES authz_roles(name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS authz_role_includes (
    role_name     TEXT NOT NULL,
    included_role TEXT NOT NULL,
    PRIMARY KEY (role_name, included_role),
    FOREIGN KEY (role_name)     REFERENCES authz_roles(name) ON DELETE CASCADE,
    FOREIGN KEY (included_role) REFERENCES authz_roles(name) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_authz_role_groups_group ON authz_role_groups(group_name);
CREATE INDEX IF NOT EXISTS idx_authz_role_users_user   ON authz_role_users(username_lower);
CREATE INDEX IF NOT EXISTS idx_authz_role_includes_inc ON authz_role_includes(included_role);
"""


class BootstrapSettings(Protocol):
    @property
    def bootstrap_role(self) -> str: ...
    @property
    def bootstrap_user(self) -> str | None: ...


def install_authz_schema(
    conn: sqlite3.Connection, settings: BootstrapSettings
) -> None:
    """Create the authz schema and (on first install) seed the bootstrap user.

    First install is detected by `authz_roles` not existing yet. After the
    schema runs, the table exists, so subsequent calls leave content alone.
    """
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='authz_roles'"
    ).fetchone() is not None

    conn.executescript(_AUTHZ_SCHEMA)

    if table_exists:
        return

    if not settings.bootstrap_user:
        return

    role = settings.bootstrap_role
    user_lower = settings.bootstrap_user.lower()

    conn.execute("INSERT INTO authz_roles(name) VALUES (?)", (role,))
    conn.execute(
        "INSERT INTO authz_roles(name) VALUES (?)", (_CLICKHOUSE_ADMIN_ROLE,)
    )
    conn.execute(
        "INSERT INTO authz_role_includes(role_name, included_role) VALUES (?, ?)",
        (role, _CLICKHOUSE_ADMIN_ROLE),
    )
    conn.execute(
        "INSERT INTO authz_role_users(role_name, username_lower) VALUES (?, ?)",
        (role, user_lower),
    )
