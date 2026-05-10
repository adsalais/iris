"""Settings for the ClickHouse module, loaded from the process environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

from iris.envtools import get_bool as _get_bool, required as _required


@dataclass(frozen=True, slots=True)
class ClickHouseSettings:
    host: str
    port: int
    user: str
    password: str
    secure: bool
    verify: bool
    ca_cert_path: str | None

    @classmethod
    def from_env(cls) -> "ClickHouseSettings":
        host = _required("CLICKHOUSE_HOST")
        port_raw = _required("CLICKHOUSE_PORT")
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ValueError(
                f"CLICKHOUSE_PORT must be an integer, got {port_raw!r}"
            ) from exc
        user = _required("CLICKHOUSE_USER")
        password = _required("CLICKHOUSE_PASSWORD")
        secure = _get_bool("CLICKHOUSE_SECURE")
        verify = _get_bool("CLICKHOUSE_VERIFY")
        ca_cert_path = os.environ.get("CLICKHOUSE_CA_CERT_PATH", "").strip() or None

        return cls(
            host=host,
            port=port,
            user=user,
            password=password,
            secure=secure,
            verify=verify,
            ca_cert_path=ca_cert_path,
        )
