"""The ``fylgja`` command line (Typer).

``serve`` runs the daemon and the API. The read commands (``now``, ``ws``, ``sessions`` and
``collectors list``) are HTTP clients of that daemon (A7): each fetches JSON from it, prints a
table or, with ``--json``, the JSON, and exits **3** with a clear message when the daemon is down.
``fylgja --help`` imports no daemon code and starts nothing.

Exit codes: 0 success, 1 the daemon answered with an error (or the item does not exist),
2 a configuration error or bad usage, 3 the daemon is not running.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from typing import Annotated, Any, NoReturn

import typer

from fylgja.cli import client, render

__all__ = ["app"]

EXIT_CONFIG: int = 2

app = typer.Typer(
    name="fylgja",
    help="Fylgja: where the work is. Run `fylgja serve`, then ask it what is going on.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
collectors_app = typer.Typer(
    help="The collector plugins.", no_args_is_help=True, pretty_exceptions_enable=False
)
app.add_typer(collectors_app, name="collectors")

JsonOption = Annotated[bool, typer.Option("--json", help="Print the daemon's JSON.")]


def _fail(message: str, code: int) -> NoReturn:
    typer.echo(f"fylgja: {message}", err=True)
    raise typer.Exit(code)


def _width() -> int:
    return shutil.get_terminal_size((render.DEFAULT_WIDTH, 24)).columns


def _fetch(path: str) -> Any:
    """GET ``path`` from the daemon, or leave with the exit code and message of the failure."""
    try:
        return client.get_json(path)
    except client.DaemonDown as exc:
        _fail(
            f"daemon not running ({exc.reason}) at {exc.url}; start it with `fylgja serve`",
            client.EXIT_DAEMON_DOWN,
        )
    except client.ApiError as exc:
        _fail(str(exc), client.EXIT_API_ERROR)
    except Exception as exc:
        # The configuration (and so the port) could not be read.
        from fylgja.config import ConfigError

        if isinstance(exc, ConfigError):
            _fail(str(exc), EXIT_CONFIG)
        raise


def _show(payload: Any, as_json: bool, text: Callable[[], str]) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False) if as_json else text())


@app.command()
def serve() -> None:
    """Run the daemon and the API on 127.0.0.1 until SIGTERM or SIGINT."""
    from fylgja.cli.serve import run

    raise typer.Exit(run())


@app.command()
def now(as_json: JsonOption = False) -> None:
    """The whole picture: workspaces, sessions and what is degraded."""
    payload = _fetch("/now")
    _show(payload, as_json, lambda: render.render_now(payload, _width()))


@app.command()
def ws(
    name: Annotated[
        str | None, typer.Argument(metavar="[N]", help="A workspace name; all when omitted.")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """The AeroSpace workspaces, or one workspace with its windows and sessions."""
    if name is None:
        workspaces = _fetch("/workspaces")
        _show(workspaces, as_json, lambda: render.render_workspaces(workspaces, _width()))
        return
    workspace = _fetch(f"/workspaces/{client.quote(name)}")
    if as_json:
        _show(workspace, True, str)
        return
    sessions = _fetch("/sessions") if workspace.get("sessions") else []
    typer.echo(render.render_workspace(workspace, sessions, _width()))


@app.command()
def sessions(as_json: JsonOption = False) -> None:
    """Every live agent session with its state and one-liner."""
    payload = _fetch("/sessions")
    _show(payload, as_json, lambda: render.render_sessions(payload, _width()))


@collectors_app.command("list")
def collectors_list(as_json: JsonOption = False) -> None:
    """The collector plugins, their capabilities and their health."""
    payload = _fetch("/collectors")
    _show(payload, as_json, lambda: render.render_collectors(payload, _width()))
