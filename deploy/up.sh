#!/usr/bin/env bash
# deploy/up.sh — declare the required env vars and launch the local stack.
#
# Usage:
#   ./up.sh                       # docker compose up -d (default)
#   ./up.sh logs -f keycloak      # any subcommand is forwarded to docker compose
#   ./up.sh down
#
# Edit the variables below before the first run. This file is checked into
# git: do NOT commit a real password change here. If you need different
# values per machine, copy this file to up.local.sh (gitignored) and run
# that instead.
set -euo pipefail

# === Required variables =====================================================

# Where persistent state lives (Keycloak H2 + sessions, ClickHouse data +
# logs). MUST be an absolute path OUTSIDE the project tree.
# `docker compose down` does NOT delete this — you delete it yourself when
# you want to wipe state.
DATA_DIR="${HOME}/.iris-data"

# Keycloak bootstrap admin. Honored ONLY on first boot; afterward the user
# lives in ${DATA_DIR}/keycloak and changes go through the admin console.
KC_ADMIN_USERNAME="admin"
KC_ADMIN_PASSWORD="admin"

# ClickHouse service-admin — the account iris connects as. NOT a human user;
# iris's bootstrap creates separate _USER / _GRP roles for human admins.
# If you change CLICKHOUSE_USER, also edit the matching tag in
# deploy/clickhouse/users.d/iris-overlay.xml — CH does not expand env vars
# inside XML config files.
CLICKHOUSE_USER="iris_admin"
CLICKHOUSE_PASSWORD="change-me-please"

# === End config =============================================================

cd "$(dirname "$0")"

if [[ -z "${DATA_DIR}" ]]; then
    echo "DATA_DIR is empty — edit $(basename "$0") before running." >&2
    exit 1
fi
if [[ "${DATA_DIR:0:1}" != "/" ]]; then
    echo "DATA_DIR must be an absolute path (got: ${DATA_DIR})." >&2
    exit 1
fi

mkdir -p "${DATA_DIR}/keycloak" \
         "${DATA_DIR}/clickhouse/data" \
         "${DATA_DIR}/clickhouse/logs"

# Keycloak runs as in-container uid 1000 and must own its data dir to write
# H2 files. Soft-fail when we can't chown (e.g. the dir is already correct,
# or we don't have permission) so the script still runs in the happy path.
chown -R 1000:1000 "${DATA_DIR}/keycloak" 2>/dev/null || true

export DATA_DIR KC_ADMIN_USERNAME KC_ADMIN_PASSWORD CLICKHOUSE_USER CLICKHOUSE_PASSWORD

if [[ $# -eq 0 ]]; then
    exec docker compose up -d
else
    exec docker compose "$@"
fi
