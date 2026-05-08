"""Wire iris.clickhouse into a FastAPI app.

Builds the shared clickhouse-connect Client and a shared httpx.AsyncClient for
impersonated queries (see iris.clickhouse.handle for why both are needed),
runs ensure_service_admin, optionally seeds the bootstrap admin from
``IRIS_BOOTSTRAP_USER``, stashes everything on app.state, and registers a
post-login provisioning hook so init_user_rights + derive_rights run once per
real authentication.

The httpx.AsyncClient is closed on app shutdown via app.state.clickhouse_close_http,
which iris.app:_lifespan invokes.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import FastAPI

from iris.auth.bootstrap import bootstrap_admin
from iris.auth.identity import User
from iris.auth.sessions import SessionStore
from iris.clickhouse.bootstrap import ensure_service_admin
from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.rights import derive_rights
from iris.clickhouse.users import init_user_rights

logger = logging.getLogger("iris.clickhouse")


def install(app: FastAPI) -> None:
    settings = ClickHouseSettings.from_env()
    client = build_client(settings)
    ensure_service_admin(client, settings)

    # Bootstrap option β: seed the first iris admin in CH if the env var is set
    # and no admin yet exists. Idempotent — re-running with an existing admin
    # is a no-op. Reads the bootstrap username from the auth install (which has
    # already run by this point and stashed it on app.state).
    bootstrap_user = getattr(app.state, "auth_bootstrap_user", None)
    if bootstrap_user:
        bootstrap_admin(client, username=bootstrap_user)

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

    app.state.clickhouse_close_http = _close_http

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
                "clickhouse: provisioned user=%s groups=%s "
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
