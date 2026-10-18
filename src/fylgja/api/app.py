"""The FastAPI application factory (``PLAN-final-plan.md`` §4.2, A6; ``PLAN.md`` §6).

:func:`create_app` takes the daemon services (:class:`~fylgja.core.daemon.Daemon`) or builds them
from the configuration. The app's lifespan starts a daemon it finds stopped and stops it again on
shutdown; a daemon the caller already started is left to the caller.

* Every route is behind :class:`~fylgja.api.security.HostGuard`; no CORS middleware exists. Every
  request with a mutating method is also behind :class:`~fylgja.api.security.MutationGuard`
  (bearer, Origin, JSON content type, body limit), whichever route it addresses.
* ``GET /`` serves the minimal page with the secret in a ``<meta>`` tag (WP4.4).
* The read routes (``/now``, ``/workspaces``, ``/sessions``, ``/projects``, ``/todos``,
  ``/collectors``, ``/health``, ``/history``, ``/next``, ``GET /summaries/{scope}/{ref}``,
  ``/view/attention``, ``/view/overview``, ``/items``, ``/rankers``, ``GET /journal/threads``,
  ``GET /journal/threads/{id}``, ``GET /journal/focused``, ``GET /journal/checkout``,
  ``GET /journal/items/{id}/how``,
  ``/inspect/*`` and the ``/events`` change stream) need no authentication. The interactive docs
  and the OpenAPI document are off.
* The journal's writes (``PATCH /journal/items/{id}``, ``POST /journal/threads/{id}/items``,
  ``POST /journal/items/{id}/todo``, ``POST /journal/threads/{id}/refresh``,
  ``POST /journal/items/{id}/how``) are mutating routes
  behind the same guard; they are registered while the journal is off and answer **409** then.
* A finished summary job publishes ``summary.ready`` (``{scope, ref, state}``) on ``/events``.
* While the journal is enabled, every journal write publishes ``journal.changed`` (``{thread}``,
  the thread id and never text) on ``/events``. It does not touch the live view, so ``/now`` is
  unchanged.
* Every route is ``async``: they read the live view, which lives on the event loop thread.

:func:`server_config` is the Uvicorn configuration ``fylgja serve`` runs with: it binds
``[api] host`` (which configuration validation pins to ``127.0.0.1``) and ``[api] port``.
"""

from __future__ import annotations

import weakref
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from fylgja.api import sse
from fylgja.api.routes import (
    collectors,
    focus,
    health,
    history,
    inspect,
    items,
    journal,
    messages,
    now,
    page,
    projects,
    sessions,
    summaries,
    todos,
    views,
    workspaces,
)
from fylgja.api.routes import (
    next as next_routes,
)
from fylgja.api.security import HostGuard, MutationGuard, load_secret
from fylgja.config import LOOPBACK_HOST, Config, load_config
from fylgja.core.daemon import Daemon
from fylgja.logsetup import register_secret

__all__ = ["create_app", "server_config"]

_ROUTERS = (
    page.router,
    now.router,
    workspaces.router,
    sessions.router,
    projects.router,
    collectors.router,
    health.router,
    history.router,
    focus.router,
    messages.router,
    next_routes.router,
    summaries.router,
    todos.router,
    views.router,
    items.router,
    journal.router,
    inspect.router,
    sse.router,
)


def create_app(
    daemon: Daemon | None = None, *, config: Config | None = None, secret: str | None = None
) -> FastAPI:
    """The API over ``daemon``; without one, a daemon built from ``config`` (default: the user's
    ``config.toml`` over the packaged defaults).

    ``secret`` is the bearer secret of the mutating routes; by default it is read once, here, from
    ``paths.api_secret_file()`` (``fylgja install secret``). Without any secret every mutation is
    refused with 401.
    """
    services = daemon if daemon is not None else Daemon(load_config() if config is None else config)

    events = sse.EventHub(services.view)
    services.summaries.add_listener(_summary_ready(events))
    if services.journal is not None:
        services.journal.add_listener(_journal_changed(events))

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
    bearer = load_secret() if secret is None else secret
    app.state.secret = bearer
    for router in _ROUTERS:
        app.include_router(router)
    if bearer:
        register_secret(bearer)
    # The last middleware added is the outermost: the Host check runs first, then the mutation
    # guard.
    app.add_middleware(
        MutationGuard, secret=bearer, allowed_origins=services.config.api.allowed_origins
    )
    app.add_middleware(HostGuard, port=services.config.api.port)
    return app


def _summary_ready(events: sse.EventHub) -> Callable[[str, str, str], None]:
    """The listener that publishes ``summary.ready`` on ``events`` for as long as the hub (and
    so its app) exists; the daemon may outlive an app."""
    hub = weakref.ref(events)

    def publish(scope: str, ref: str, state: str) -> None:
        found = hub()
        if found is not None:
            found.publish("summary.ready", {"scope": scope, "ref": ref, "state": state})

    return publish


def _journal_changed(events: sse.EventHub) -> Callable[[str], None]:
    """The listener that publishes ``journal.changed`` on ``events`` for as long as the hub (and
    so its app) exists, as :func:`_summary_ready` does."""
    hub = weakref.ref(events)

    def publish(thread: str) -> None:
        found = hub()
        if found is not None:
            found.publish("journal.changed", {"thread": thread})

    return publish


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
