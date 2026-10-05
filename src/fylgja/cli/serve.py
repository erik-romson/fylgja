"""``fylgja serve``: the daemon and the API under Uvicorn.

Imported only by the ``serve`` command, so ``fylgja --help`` and the read commands never load
FastAPI, Uvicorn or the daemon. SIGTERM and SIGINT stop the server gracefully: Uvicorn closes the
listener and the lifespan stops the daemon (the supervisor shuts the collectors down, then the
writer commits what is queued), all within :data:`STOP_TIMEOUT` seconds.
"""

from __future__ import annotations

import os
import signal
import sys
from typing import Final

import typer

from fylgja import logsetup, paths
from fylgja.config import ConfigError, load_config

__all__ = ["STOP_TIMEOUT", "run"]

STOP_TIMEOUT: Final = 5.0
"""Seconds a graceful shutdown may take."""
EXIT_CONFIG: Final = 2
LOG_LEVEL_ENV: Final = "FYLGJA_LOG_LEVEL"


def _ignore(_signum: int, _frame: object) -> None:
    """Placeholder handler, see :func:`run`."""


def run() -> int:
    """Serve until a stop signal; returns the process exit code."""
    import logging

    import uvicorn

    from fylgja.api.app import create_app, server_config
    from fylgja.core.daemon import Daemon

    try:
        config = load_config()
    except ConfigError as exc:
        typer.echo(f"fylgja: {exc}", err=True)
        return EXIT_CONFIG

    level = os.environ.get(LOG_LEVEL_ENV, "INFO").upper()
    logsetup.setup_logging(
        level=level if level in logging.getLevelNamesMapping() else "INFO",
        redact_patterns=config.privacy.redact_patterns,
    )
    log = logsetup.get_logger("fylgja.serve")
    daemon = Daemon(config)
    server_settings = server_config(create_app(daemon), config)
    server_settings.timeout_graceful_shutdown = 2  # open connections never hold the stop up
    server = uvicorn.Server(server_settings)

    # Uvicorn re-raises a captured signal once it has shut down, so that the parent sees how the
    # process ended. With the default SIGTERM action that would turn a clean stop into a kill, so
    # the handlers Uvicorn restores are inert ones: it exits normally with code 0.
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, _ignore)

    typer.echo(
        f"fylgja: serving on http://{config.api.host}:{config.api.port} (log: {paths.log_file()})",
        err=True,
    )
    log.info("serving on %s:%d", config.api.host, config.api.port)
    try:
        server.run()
    except Exception:
        log.exception("the server stopped with an error")
        raise
    finally:
        log.info("stopped")
        logsetup.teardown_logging()
    if not server.started:
        # Uvicorn could not start (for example the port is taken); it has already said why.
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
