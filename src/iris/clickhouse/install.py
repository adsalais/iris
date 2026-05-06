"""Wire iris.clickhouse into a FastAPI app.

Builds the shared clickhouse-connect Client, runs ensure_service_admin (idempotent),
stashes client + settings on app.state, and registers a post-login provisioning
hook so init_user_rights fires once per real authentication.

The caller normally calls iris.auth.install(app) first so app.state.post_login_hooks
is already a list; if it isn't, install() creates it.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from iris.auth.identity import User
from iris.clickhouse.bootstrap import ensure_service_admin
from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.users import init_user_rights

logger = logging.getLogger("iris.clickhouse")


def install(app: FastAPI) -> None:
    settings = ClickHouseSettings.from_env()
    client = build_client(settings)
    ensure_service_admin(client, settings)

    app.state.clickhouse_client = client
    app.state.clickhouse_settings = settings

    async def _provision_on_login(user: User) -> None:
        await asyncio.to_thread(
            init_user_rights,
            client,
            username=user.username,
            groups=list(user.groups),
            settings=settings,
        )
        logger.info(
            "clickhouse: provisioned user=%s groups=%s",
            user.username,
            list(user.groups),
        )

    if not hasattr(app.state, "post_login_hooks"):
        app.state.post_login_hooks = []
    app.state.post_login_hooks.append(_provision_on_login)
