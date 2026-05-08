"""Wire iris.clickhouse into a FastAPI app.

Builds the shared clickhouse-connect Client and a shared httpx.AsyncClient for
impersonated queries (see iris.clickhouse.handle for why both are needed),
runs the CH-side bootstrap (creates iris_global_admin sentinel + optional
admin user/group roles from CLICKHOUSE_ADMIN_USER / CLICKHOUSE_ADMIN_GROUP),
stashes everything on app.state, and registers a post-login provisioning hook
so init_user_rights + derive_rights run once per real authentication.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from fastapi import FastAPI

from iris.auth.identity import User
from iris.auth.sessions import SessionStore
from iris.clickhouse.bootstrap import bootstrap_admin
from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.rights import derive_rights
from iris.clickhouse.users import init_user_rights

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
            init_user_rights,
            client,
            username=user.username,
            groups=list(user.groups),
            settings=settings,
        )
        rights = await asyncio.to_thread(
            derive_rights,
            client,
            username=user.username,
            groups=list(user.groups),
        )
        store: SessionStore = app.state.auth_session_store
        await store.set_rights(session_id, rights)
        logger.info(
            (
                "clickhouse: provisioned username=%s groups=%s "
                "rights=admin:%s creator:%s reader:%d writer:%d db_admin:%d"
            ),
            user.username,
            list(user.groups),
            rights.is_admin,
            rights.can_create_database,
            len(rights.db_reader),
            len(rights.db_writer),
            len(rights.db_admin),
        )

    if not hasattr(app.state, "post_login_hooks"):
        app.state.post_login_hooks = []
    app.state.post_login_hooks.append(_provision_on_login)
