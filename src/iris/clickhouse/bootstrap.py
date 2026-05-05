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
