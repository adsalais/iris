"""Startup-time provisioning for the service-admin role."""

from __future__ import annotations

from clickhouse_connect.driver.client import Client

from iris.clickhouse.config import ClickHouseSettings


def ensure_service_admin(client: Client, settings: ClickHouseSettings) -> None:
    """Idempotent: ensure the service-admin role exists and is granted to the configured user.

    Implementation lands in Task 10; this stub keeps imports resolvable.
    """
    _ = client
    _ = settings
    raise NotImplementedError("Task 10")
