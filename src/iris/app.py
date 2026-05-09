from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from iris.middleware import SecurityHeadersMiddleware
from iris.templates import init_templates


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Subsystems register teardown callables in app.state.shutdown_hooks during
    # build_app(); this lifespan runs them in LIFO order on shutdown so a
    # subsystem's teardown sees its dependencies still alive.
    yield
    for hook in reversed(app.state.shutdown_hooks):
        await hook()


def build_app(*, install_clickhouse: bool = True) -> FastAPI:
    app = FastAPI(title="Iris", lifespan=_lifespan)
    shutdown_hooks: list[Callable[[], Awaitable[None]]] = []
    app.state.shutdown_hooks = shutdown_hooks

    from iris.auth.routes import install as install_auth
    install_auth(app)

    if install_clickhouse:
        from iris.clickhouse.install import install as install_clickhouse_fn
        install_clickhouse_fn(app)

    from iris.shell.install import install as install_shell
    install_shell(app)

    from iris.features.authorization.install import install as install_authorization
    install_authorization(app)

    # Build the templates loader once all subsystems have registered their dirs.
    app.state.templates = init_templates()

    app.add_middleware(SecurityHeadersMiddleware)

    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )

    return app
