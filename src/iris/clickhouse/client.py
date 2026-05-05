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
    return clickhouse_connect.get_client(**kwargs)  # pyright: ignore[reportUnknownMemberType]
