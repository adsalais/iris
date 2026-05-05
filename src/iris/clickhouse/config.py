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
