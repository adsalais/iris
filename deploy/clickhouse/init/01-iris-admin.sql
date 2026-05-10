-- Provisions the iris service-admin user.
--
-- We define iris_admin via SQL (NOT via the image's CLICKHOUSE_USER env var)
-- so the user lives in `local_directory` access storage instead of `users_xml`.
-- iris's per-login provisioning issues `GRANT IMPERSONATE ON <username> TO
-- iris_admin`, which mutates the iris_admin record — and `users_xml` is
-- read-only at runtime (CH error code 495, ACCESS_STORAGE_READONLY).
--
-- This script runs on every container start (the compose sets
-- CLICKHOUSE_ALWAYS_RUN_INITDB_SCRIPTS=1) and must therefore be idempotent.

CREATE USER IF NOT EXISTS iris_admin
    IDENTIFIED WITH plaintext_password BY 'change-me-please'
    DEFAULT ROLE ALL;

-- ALL on *.* is the heavy hammer; equivalent to CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1
-- but issued via SQL so the grants live with the SQL user definition.
GRANT ALL ON *.* TO iris_admin WITH GRANT OPTION;

-- NAMED COLLECTION ADMIN is NOT included in `ALL`; iris's bootstrap_admin
-- needs it to GRANT ALL onwards to per-user/group admin roles.
GRANT NAMED COLLECTION ADMIN ON * TO iris_admin WITH GRANT OPTION;
