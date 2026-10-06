"""The FastAPI application factory (``PLAN-final-plan.md`` §4.2, A6; ``PLAN.md`` §6).

:func:`create_app` takes the daemon services (:class:`~fylgja.core.daemon.Daemon`) or builds them
from the configuration. The app's lifespan starts a daemon it finds stopped and stops it again on
shutdown; a daemon the caller already started is left to the caller.

* Every route is behind :class:`~fylgja.api.security.HostGuard`; no CORS middleware exists.
* The read routes (``/now``, ``/workspaces``, ``/sessions``, ``/projects``, ``/collectors``,
  ``/health`` and the ``/events`` change stream) need no authentication. The interactive docs and
  the OpenAPI document are off.
* Every route is ``async``: they read the live view, which lives on the event loop thread.

:func:`server_config` is the Uvicorn configuration ``fylgja serve`` runs with: it binds
``[api] host`` (which configuration validation pins to ``127.0.0.1``) and ``[api] port``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from fylgja.api import sse
from fylgja.api.routes import collectors, health, now, projects, sessions, workspaces
from fylgja.api.security import HostGuard
from fylgja.config import LOOPBACK_HOST, Config, load_config
from fylgja.core.daemon import Daemon

__all__ = ["create_app", "server_config"]

_ROUTERS = (
    now.router,
    workspaces.router,
    sessions.router,
    projects.router,
    collectors.router,
    health.router,
    sse.router,
)


def create_app(daemon: Daemon | None = None, *, config: Config | None = None) -> FastAPI:
    """The API over ``daemon``; without one, a daemon built from ``config`` (default: the user's
    ``config.toml`` over the packaged defaults)."""
    services = daemon if daemon is not None else Daemon(load_config() if config is None else config)

    events = sse.EventHub(services.view)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        owned = not services.started
        if owned:
            await services.start()
        try:
            yield
        finally:
            events.close()
            if owned:
                await services.stop()

    app = FastAPI(
        title="Fylgja",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.daemon = services
    app.state.events = events
    for router in _ROUTERS:
        app.include_router(router)
    app.add_middleware(HostGuard, port=services.config.api.port)
    return app


def server_config(app: FastAPI, config: Config) -> uvicorn.Config:
    """Uvicorn on ``127.0.0.1:<[api] port>``, with no proxy-header trust and no server header."""
    if config.api.host != LOOPBACK_HOST:  # pragma: no cover - configuration validation refuses it
        raise ValueError("the API binds to 127.0.0.1 only")
    return uvicorn.Config(
        app,
        host=LOOPBACK_HOST,
        port=config.api.port,
        lifespan="on",
        proxy_headers=False,
        server_header=False,
        access_log=False,
        log_config=None,
    )
