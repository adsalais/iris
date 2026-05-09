"""Wire iris.clickhouse into a FastAPI app.

Builds the shared clickhouse-connect Client and a shared httpx.AsyncClient
for impersonated queries (``EXECUTE AS`` cannot use clickhouse-connect's
binary protocol, so user-scoped queries go through the HTTP endpoint
directly), runs the CH-side bootstrap (creates iris_global_admin sentinel
plus optional admin user/group roles from CLICKHOUSE_ADMIN_USER /
CLICKHOUSE_ADMIN_GROUP), stashes everything on app.state, and registers a
post-login provisioning hook so provision_user + derive_capabilities run once
per real authentication.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from fastapi import FastAPI

from iris.auth.identity import User
from iris.auth.store import SessionStore
from iris.clickhouse.bootstrap import bootstrap_admin
from iris.clickhouse.capabilities import derive_capabilities
from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.users import provision_user

logger = logging.getLogger("iris.clickhouse")


def install(app: FastAPI) -> None:
    settings = ClickHouseSettings.from_env()
    client = build_client(settings)

    admin_user = os.environ.get("CLICKHOUSE_ADMIN_USER", "").strip() or None
    admin_group = os.environ.get("CLICKHOUSE_ADMIN_GROUP", "").strip() or None
    bootstrap_admin(client, admin_user=admin_user, admin_group=admin_group)

    scheme = "https" if settings.secure else "http"
    base_url = f"{scheme}://{settings.host}:{settings.port}"
    verify: bool | str = settings.ca_cert_path if settings.ca_cert_path else settings.verify
    http_client = httpx.AsyncClient(
        base_url=base_url,
        auth=(settings.user, settings.password),
        verify=verify,
        timeout=httpx.Timeout(30.0),
    )

    app.state.clickhouse_client = client
    app.state.clickhouse_settings = settings
    app.state.clickhouse_http_client = http_client

    async def _close_http() -> None:
        await http_client.aclose()

    if not hasattr(app.state, "shutdown_hooks"):
        app.state.shutdown_hooks = []
    app.state.shutdown_hooks.append(_close_http)

    async def _provision_on_login(user: User, session_id: str) -> None:
        await asyncio.to_thread(
            provision_user,
            client,
            username=user.username,
            groups=list(user.groups),
            settings=settings,
        )
        capabilities = await asyncio.to_thread(
            derive_capabilities,
            client,
            username=user.username,
            groups=list(user.groups),
        )
        store: SessionStore = app.state.auth_session_store
        await store.set_capabilities(session_id, capabilities)
        logger.info(
            (
                "clickhouse: provisioned username=%s groups=%s "
                "capabilities=admin:%s creator:%s reader:%d writer:%d db_admin:%d"
            ),
            user.username,
            list(user.groups),
            capabilities.is_admin,
            capabilities.can_create_database,
            len(capabilities.db_reader),
            len(capabilities.db_writer),
            len(capabilities.db_admin),
        )

    if not hasattr(app.state, "post_login_hooks"):
        app.state.post_login_hooks = []
    app.state.post_login_hooks.append(_provision_on_login)
