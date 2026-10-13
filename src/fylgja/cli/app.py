"""The ``fylgja`` command line (Typer).

``serve`` runs the daemon and the API. The read commands (``now``, ``ws``, ``sessions``,
``history``, ``next`` and ``collectors list``) are HTTP clients of that daemon (A7): each fetches
JSON from it, prints a table or, with ``--json``, the JSON, and exits **3** with a clear message
when the daemon is down.
``next --feedback good|bad`` rates the top item of the list ``next`` showed last today (the CLI
remembers its ``list_id`` in the state directory and posts to the daemon).
``todo add|done|rm|ls`` talk to the daemon as well (writes send the bearer secret; ``add`` without a
scope uses the focused workspace). ``label ws`` sets or clears a manual workspace label.
``session message`` posts to the daemon too: it asks first
unless ``--yes`` is given and exits 0 only when the message was submitted.
``story session <key>`` and ``story ws <n>`` ask the daemon for a summary (WP8.1): they post the
request, wait for ``summary.ready`` on ``/events`` (up to 60 s), then fetch and print it. The model
call runs in the daemon, never here.
``doctor`` and the ``install`` commands (``config``, ``launchd``, ``hooks``, ``statusline``,
``keybinding``, ``skill``, ``secret``) run locally (the A7 exceptions): doctor checks the setup
and changes nothing, and the installers print a diff and write only with ``--apply`` (``secret``
writes at once and prints nothing secret). ``fylgja --help`` imports no daemon code and starts
nothing.

Exit codes: 0 success, 1 the daemon answered with an error (or the item does not exist),
2 a configuration error or bad usage, 3 the daemon is not running.
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.parse
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from fylgja.cli import client, render

__all__ = ["app"]

EXIT_CONFIG: int = 2
NEXT_STATE_FILE = "next-last.json"

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
session_app = typer.Typer(
    help="Act on one live agent session.", no_args_is_help=True, pretty_exceptions_enable=False
)
app.add_typer(session_app, name="session")
todo_app = typer.Typer(
    help="Fylgja's own todos, per workspace, project or session.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(todo_app, name="todo")
label_app = typer.Typer(
    help="Name a workspace by hand; a manual label beats the inferred one until cleared.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(label_app, name="label")
story_app = typer.Typer(
    help="Summaries written by a local model in the daemon "
    "(off unless [summaries] enabled = true).",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(story_app, name="story")
install_app = typer.Typer(
    help="Install Fylgja's configuration into ~/dotfiles and its launchd agent. "
    "Without --apply every command only prints what it would change.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(install_app, name="install")

JsonOption = Annotated[bool, typer.Option("--json", help="Print the daemon's JSON.")]
ApplyOption = Annotated[bool, typer.Option("--apply", help="Write the changes (default: diff).")]


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
            f"daemon not running ({exc.reason}) at {exc.url}; start it with `fylgja serve` "
            "or `fylgja install launchd --apply`",
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


@label_app.command("ws")
def label_ws(
    name: Annotated[str, typer.Argument(metavar="NAME", help="The workspace name.")],
    text: Annotated[
        str | None, typer.Argument(metavar="TEXT", help="The label. Omit it with --clear.")
    ] = None,
    clear: Annotated[
        bool, typer.Option("--clear", help="Remove the manual label; inference shows again.")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Set the manual label of workspace NAME, or clear it with --clear."""
    if clear == (text is not None):
        _fail("give a label, or --clear (not both)", EXIT_CONFIG)
    path = f"/workspaces/{client.quote(name)}/label"
    workspace = _write("DELETE", path) if clear else _write("PUT", path, {"label": text})
    if not isinstance(workspace, dict):
        _fail(f"{path}: the daemon's answer is not a workspace", client.EXIT_API_ERROR)
    _show(
        workspace,
        as_json,
        lambda: f"{name}: {workspace.get('label', '—')} ({workspace.get('label_source', 'none')})",
    )


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


def _next_file() -> Path:
    from fylgja import paths

    return paths.state_dir() / NEXT_STATE_FILE


def _remember_list(list_id: str) -> None:
    """Remember the list just shown, for today, so that ``next --feedback`` rates it."""
    target = _next_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"date": date.today().isoformat(), "list_id": list_id})
        scratch = target.with_name(target.name + ".tmp")
        scratch.write_text(text + "\n", encoding="utf-8")
        scratch.replace(target)
    except OSError as exc:
        typer.echo(
            f"fylgja: could not remember this list for --feedback ({exc.strerror})", err=True
        )


def _last_list() -> str:
    """The ``list_id`` of the most recent list ``fylgja next`` showed today, or leave."""
    try:
        stored = json.loads(_next_file().read_text(encoding="utf-8"))
        list_id = str(stored["list_id"])
        shown = str(stored["date"])
    except (OSError, ValueError, KeyError, TypeError):
        shown = list_id = ""
    if not list_id or shown != date.today().isoformat():
        _fail("no list was shown today; run `fylgja next` first", client.EXIT_API_ERROR)
    return list_id


@app.command("next")
def next_(
    feedback: Annotated[
        str | None,
        typer.Option(
            "--feedback",
            metavar="good|bad",
            help="Rate the top item of the list `fylgja next` last showed today.",
        ),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """What to do next, best first, with the reason for each. `--feedback good|bad` rates the
    top item of the list shown last."""
    if feedback is not None:
        if feedback not in ("good", "bad"):
            _fail("--feedback takes good or bad", EXIT_CONFIG)
        answer = _write("POST", "/next/feedback", {"list_id": _last_list(), "rating": feedback})
        _show(
            answer,
            as_json,
            lambda: f"rated item {answer.get('rank', '?')} of the last list {feedback}",
        )
        return
    payload = _fetch("/next")
    if isinstance(payload, dict) and payload.get("list_id"):
        _remember_list(str(payload["list_id"]))
    _show(payload, as_json, lambda: render.render_next(payload, _width()))


@app.command()
def sessions(as_json: JsonOption = False) -> None:
    """Every live agent session with its state and one-liner."""
    payload = _fetch("/sessions")
    _show(payload, as_json, lambda: render.render_sessions(payload, _width()))


STORY_WAIT: float = 60.0
"""Seconds ``fylgja story`` waits for ``summary.ready`` by default."""
STORY_DISABLED = (
    "summaries are disabled: set [summaries] enabled = true in config.toml and restart the daemon"
)
WaitOption = Annotated[
    float,
    typer.Option("--wait", min=0, metavar="SECONDS", help="How long to wait for the summary."),
]


@app.command()
def history(
    since: Annotated[
        str,
        typer.Option(
            "--since", metavar="WHEN", help="today, yesterday, 2h, 3d or an ISO 8601 date or time."
        ),
    ] = "today",
    until: Annotated[
        str | None, typer.Option("--until", metavar="WHEN", help="The end; default now.")
    ] = None,
    spans: Annotated[
        bool, typer.Option("--spans/--no-spans", help="List every focus span, or the totals only.")
    ] = True,
    story: Annotated[
        bool,
        typer.Option(
            "--story",
            help="Tell the story of one day in a paragraph (needs [summaries] enabled = true).",
        ),
    ] = False,
    day: Annotated[
        str | None,
        typer.Option("--date", metavar="YYYY-MM-DD", help="With --story: the day; default today."),
    ] = None,
    wait: WaitOption = STORY_WAIT,
    as_json: JsonOption = False,
) -> None:
    """Where your attention went: focus time per workspace, project and app, and the spans.

    With --story it tells the story of one day (--date, default today) as one paragraph written
    by a local model. A past day's story is made once and only rewritten when its inputs change.
    """
    from fylgja.cli.when import parse_when

    if day is not None and not story:
        _fail("--date goes with --story", EXIT_CONFIG)
    if story:
        _history_story(day, wait, as_json)
        return
    try:
        query = {"since": parse_when(since).isoformat()}
        if until is not None:
            query["until"] = parse_when(until).isoformat()
    except ValueError as exc:
        _fail(str(exc), EXIT_CONFIG)
    payload = _fetch("/history?" + urllib.parse.urlencode(query))
    _show(payload, as_json, lambda: render.render_history(payload, _width(), spans=spans))


@app.command()
def items(
    rank: Annotated[
        str | None,
        typer.Option("--rank", metavar="NAME", help="The ranker; default: the one in effect."),
    ] = None,
    days: Annotated[
        int | None, typer.Option("--days", metavar="N", help="How many days back to group.")
    ] = None,
    search: Annotated[
        str | None,
        typer.Option(
            "--search", metavar="TEXT", help="Only items matching TEXT (the daemon searches)."
        ),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", metavar="N", help="At most N items (the daemon caps).")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Folders and apps you worked in, ranked by the daemon, grouped by day."""
    query = {
        key: str(value)
        for key, value in (("rank", rank), ("days", days), ("q", search), ("limit", limit))
        if value is not None
    }
    payload = _fetch("/items" + (f"?{urllib.parse.urlencode(query)}" if query else ""))
    _show(payload, as_json, lambda: render.render_items(payload, _width()))


@app.command()
def rankers(as_json: JsonOption = False) -> None:
    """The installed rankers: enabled, loaded or why not, and the default in effect."""
    payload = _fetch("/rankers")
    _show(payload, as_json, lambda: render.render_rankers(payload, _width()))


@collectors_app.command("list")
def collectors_list(as_json: JsonOption = False) -> None:
    """The collector plugins, their capabilities and their health."""
    payload = _fetch("/collectors")
    _show(payload, as_json, lambda: render.render_collectors(payload, _width()))


def _walk(sessions: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for session in sessions if isinstance(sessions, list) else []:
        if isinstance(session, dict):
            found.append(session)
            found.extend(_walk(session.get("children")))
    return found


def _session_key(given: str) -> str:
    """``given`` as a session key: a key as it is, else the one live session whose native id
    equals or starts with it."""
    if ":" in given:
        return given
    keys = [str(s.get("key")) for s in _walk(_fetch("/sessions")) if s.get("key")]
    exact = [key for key in keys if key.partition(":")[2] == given]
    matches = exact or [key for key in keys if key.partition(":")[2].startswith(given)]
    if not matches:
        _fail(f"no live session matches {given!r} (see `fylgja sessions`)", client.EXIT_API_ERROR)
    if len(set(matches)) > 1:
        _fail(
            f"{given!r} matches {len(set(matches))} sessions; give more of the id or the full key",
            client.EXIT_API_ERROR,
        )
    return matches[0]


SENT_STATES = frozenset({"submitted", "observed-enqueued"})


@session_app.command("message")
def session_message(
    session: Annotated[
        str,
        typer.Argument(
            metavar="ID", help="A session key (kind:id), a native session id or a unique prefix."
        ),
    ],
    text: Annotated[str, typer.Argument(help="The plain text to send.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Send without asking first.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Send a short plain-text message to a live local session (off unless
    [actions.message] enabled = true).

    Asks first unless --yes is given. The message is written once and never retried, and the
    receipt says only what Fylgja knows: "submitted" means written to the session's socket, not
    delivered. Exits 0 when submitted, else 1 (3 when the daemon is down).
    """
    key = _session_key(session)
    if not yes and not typer.confirm(
        f"Send {len(text)} characters to {key}? It is written once and never retried",
        default=False,
    ):
        _fail("not sent", client.EXIT_API_ERROR)
    path = f"/sessions/{client.quote(key)}/message"
    try:
        receipt = client.send_json("POST", path, {"text": text})
    except client.DaemonDown as exc:
        _fail(
            f"daemon not running ({exc.reason}) at {exc.url}; nothing was sent",
            client.EXIT_DAEMON_DOWN,
        )
    except client.ApiError as exc:
        _fail(f"{exc}; nothing was sent", client.EXIT_API_ERROR)
    if not isinstance(receipt, dict):
        _fail(f"{path}: the daemon's answer is not a receipt", client.EXIT_API_ERROR)
    state = str(receipt.get("state"))
    _show(
        receipt,
        as_json,
        lambda: f"{state}: {receipt.get('detail', '')}\nmessage id: {receipt.get('message_id')}",
    )
    raise typer.Exit(0 if state in SENT_STATES else client.EXIT_API_ERROR)


# --- stories ----------------------------------------------------------------------------------


def _ready_for(scope: str, ref: str) -> Callable[[str, Any], bool]:
    def match(name: str, data: Any) -> bool:
        return (
            name == "summary.ready"
            and isinstance(data, dict)
            and data.get("scope") == scope
            and data.get("ref") == ref
        )

    return match


def _down(exc: client.DaemonDown) -> NoReturn:
    _fail(
        f"daemon not running ({exc.reason}) at {exc.url}; start it with `fylgja serve` "
        "or `fylgja install launchd --apply`",
        client.EXIT_DAEMON_DOWN,
    )


def _request_story(scope: str, ref: str, wait: float) -> dict[str, Any]:
    """Post the request with the change stream already open (so ``summary.ready`` cannot be
    missed), wait for it when the summary is being written, then fetch it."""
    path = f"/summaries/{scope}/{client.quote(ref)}"
    try:
        stream = client.open_events()
    except client.DaemonDown as exc:
        _down(exc)
    except client.ApiError as exc:
        _fail(str(exc), client.EXIT_API_ERROR)
    try:
        try:
            answer = client.send_json("POST", path)
        except client.DaemonDown as exc:
            _down(exc)
        except client.ApiError as exc:
            _fail(STORY_DISABLED if exc.status == 409 else str(exc), client.EXIT_API_ERROR)
        if isinstance(answer, dict) and answer.get("state") == "pending":
            stream.wait_for(_ready_for(scope, ref), wait)
            answer = _fetch(path)
    finally:
        stream.close()
    if not isinstance(answer, dict):
        _fail(f"{path}: the daemon's answer is not a summary", client.EXIT_API_ERROR)
    return answer


def _story(scope: str, ref: str, wait: float, as_json: bool) -> None:
    answer = _request_story(scope, ref, wait)
    state = answer.get("state")
    if state == "pending":
        _fail(
            f"no summary after {wait:g} s; it is still being written, run the command again later",
            client.EXIT_API_ERROR,
        )
    if state == "disabled":
        _fail(STORY_DISABLED, client.EXIT_API_ERROR)
    _show(answer, as_json, lambda: render.render_story(answer, _width()))
    if state == "failed":
        raise typer.Exit(client.EXIT_API_ERROR)


def _history_story(day: str | None, wait: float, as_json: bool) -> None:
    """``fylgja history --story``: the day's paragraph (today unless ``--date`` says another)."""
    ref = date.today().isoformat() if day is None else day
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", ref) is None:
            raise ValueError
        date.fromisoformat(ref)
    except ValueError:
        _fail(f"--date {ref!r} is not a day: write it YYYY-MM-DD", EXIT_CONFIG)
    _story("day", ref, wait, as_json)


@story_app.command("session")
def story_session(
    session: Annotated[
        str,
        typer.Argument(
            metavar="KEY", help="A session key (kind:id), a native session id or a unique prefix."
        ),
    ],
    wait: WaitOption = STORY_WAIT,
    as_json: JsonOption = False,
) -> None:
    """ARC and NOW of a live agent session: what it has been about, and what it is doing now.

    Exits 0 with a summary, 1 when summaries are disabled, the summary failed or it did not
    arrive within --wait seconds, and 3 when the daemon is not running.
    """
    _story("session", _session_key(session), wait, as_json)


@story_app.command("ws")
def story_ws(
    name: Annotated[str, typer.Argument(metavar="N", help="The workspace name.")],
    wait: WaitOption = STORY_WAIT,
    as_json: JsonOption = False,
) -> None:
    """One short paragraph on what a workspace is for and what is going on in it.

    Exits like `fylgja story session`.
    """
    _story("workspace", name, wait, as_json)


# --- todos ------------------------------------------------------------------------------------

WsOption = Annotated[str | None, typer.Option("--ws", help="A workspace name.")]
ProjectOption = Annotated[
    str | None, typer.Option("--project", help="A project path, or its name when unique.")
]
SessionOption = Annotated[
    str | None,
    typer.Option("--session", help="A session key, a native session id or a unique prefix."),
]
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _write(method: str, path: str, body: Any = None) -> Any:
    """Send a guarded write to the daemon, or leave with the failure's exit code and message."""
    try:
        return client.send_json(method, path, body)
    except client.DaemonDown as exc:
        _fail(
            f"daemon not running ({exc.reason}) at {exc.url}; start it with `fylgja serve` "
            "or `fylgja install launchd --apply`",
            client.EXIT_DAEMON_DOWN,
        )
    except client.ApiError as exc:
        _fail(str(exc), client.EXIT_API_ERROR)
    except Exception as exc:
        from fylgja.config import ConfigError

        if isinstance(exc, ConfigError):
            _fail(str(exc), EXIT_CONFIG)
        raise


def _project_path(given: str) -> str:
    """``given`` as a project path: an absolute path as it is, else the one live project whose
    name or last path segment equals it."""
    if given.startswith("/"):
        return given
    projects = [p for p in _fetch("/projects") if isinstance(p, dict) and p.get("path")]
    paths_found = {
        str(p["path"])
        for p in projects
        if given in (p.get("name"), str(p["path"]).rstrip("/").rsplit("/", 1)[-1])
    }
    if not paths_found:
        _fail(f"no live project is called {given!r} (give its path)", client.EXIT_API_ERROR)
    if len(paths_found) > 1:
        _fail(f"{given!r} names {len(paths_found)} projects; give the path", client.EXIT_API_ERROR)
    return paths_found.pop()


def _scope_query(ws: str | None, project: str | None, session: str | None) -> dict[str, str]:
    """The scope flags as the API's ``ws``, ``project`` or ``session`` field; at most one."""
    if sum(value is not None for value in (ws, project, session)) > 1:
        _fail("give at most one of --ws, --project and --session", EXIT_CONFIG)
    if ws is not None:
        return {"ws": ws}
    if project is not None:
        return {"project": _project_path(project)}
    if session is not None:
        return {"session": _session_key(session)}
    return {}


def _todo_id(given: str) -> str:
    """``given`` as a todo id: a full id as it is, else the one todo whose id starts with it."""
    if _UUID.match(given):
        return given
    todos = _fetch("/todos")
    ids = sorted({str(t.get("id")) for t in todos if isinstance(t, dict) and t.get("id")})
    matches = [i for i in ids if given and i.startswith(given.lower())]
    if not matches:
        _fail(f"no todo has an id starting with {given!r} (see `fylgja todo ls`)", 1)
    if len(matches) > 1:
        _fail(f"{given!r} matches {len(matches)} todos; give more of the id", 1)
    return matches[0]


@todo_app.command("add")
def todo_add(
    text: Annotated[str, typer.Argument(help="What to do.")],
    ws: WsOption = None,
    project: ProjectOption = None,
    session: SessionOption = None,
    next_: Annotated[
        bool, typer.Option("--next", help="Mark it `next` instead of `open`.")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Add a todo and print its id. Without --ws, --project or --session it belongs to the
    focused workspace."""
    body: dict[str, Any] = {"text": text, **_scope_query(ws, project, session)}
    if next_:
        body["status"] = "next"
    todo = _write("POST", "/todos", body)
    if not isinstance(todo, dict) or not todo.get("id"):
        _fail("/todos: the daemon's answer is not a todo", client.EXIT_API_ERROR)
    _show(todo, as_json, lambda: str(todo["id"]))


def _change(
    given: str, method: str, body: Any, as_json: bool, verb: str
) -> None:  # shared by done and rm
    todo_id = _todo_id(given)
    answer = _write(method, f"/todos/{client.quote(todo_id)}", body)
    _show(answer if answer is not None else {"id": todo_id}, as_json, lambda: f"{verb} {todo_id}")


@todo_app.command("done")
def todo_done(
    todo: Annotated[str, typer.Argument(metavar="ID", help="A todo id or a unique prefix.")],
    as_json: JsonOption = False,
) -> None:
    """Mark a todo done (doing it again changes nothing)."""
    _change(todo, "PATCH", {"status": "done"}, as_json, "done")


@todo_app.command("rm")
def todo_rm(
    todo: Annotated[str, typer.Argument(metavar="ID", help="A todo id or a unique prefix.")],
    as_json: JsonOption = False,
) -> None:
    """Delete a todo (it is kept, hidden, and never listed again)."""
    _change(todo, "DELETE", None, as_json, "removed")


@todo_app.command("ls")
def todo_ls(
    ws: WsOption = None,
    project: ProjectOption = None,
    session: SessionOption = None,
    all_: Annotated[bool, typer.Option("--all", help="Include done todos.")] = False,
    as_json: JsonOption = False,
) -> None:
    """List open and next todos, of one scope when given."""
    query = _scope_query(ws, project, session)
    if not all_:
        query["status"] = "open,next"
    path = "/todos" + (f"?{urllib.parse.urlencode(query)}" if query else "")
    todos = _fetch(path)
    _show(todos, as_json, lambda: render.render_todos(todos, _width()))


@app.command()
def doctor(
    as_json: Annotated[bool, typer.Option("--json", help="Print the checks as JSON.")] = False,
) -> None:
    """Check the setup: config, plugins, paths, daemon, port, hook commands and dotfiles.

    Runs locally and changes nothing. Exits 0 when no check fails (warnings are allowed), else 1.
    """
    import asyncio

    from fylgja.cli import doctor as checks_module
    from fylgja.core.system import SubprocessRunner

    checks = asyncio.run(checks_module.run_doctor(runner=SubprocessRunner()))
    typer.echo(
        checks_module.render_json(checks)
        if as_json
        else checks_module.render_checks(checks, _width())
    )
    raise typer.Exit(checks_module.exit_code(checks))


@install_app.command("config")
def install_config(apply: ApplyOption = False) -> None:
    """Create config.toml in ~/dotfiles, list it in dotfiles.conf and link it into place."""
    import asyncio

    from fylgja.cli import install
    from fylgja.core.system import SubprocessRunner

    code = asyncio.run(
        install.run_install_config(apply=apply, runner=SubprocessRunner(), echo=typer.echo)
    )
    raise typer.Exit(code)


@install_app.command("launchd")
def install_launchd(apply: ApplyOption = False) -> None:
    """Write the LaunchAgent plist for `fylgja serve` and bootstrap it with launchctl."""
    import asyncio

    from fylgja.cli import install
    from fylgja.core.system import SubprocessRunner

    code = asyncio.run(
        install.run_install_launchd(apply=apply, runner=SubprocessRunner(), echo=typer.echo)
    )
    raise typer.Exit(code)


@install_app.command("hooks")
def install_hooks(apply: ApplyOption = False) -> None:
    """Register Fylgja's async hooks in ~/.claude/settings.json (the file in ~/dotfiles)."""
    import asyncio

    from fylgja.cli import install
    from fylgja.core.system import SubprocessRunner

    code = asyncio.run(
        install.run_install_hooks(apply=apply, runner=SubprocessRunner(), echo=typer.echo)
    )
    raise typer.Exit(code)


@install_app.command("statusline")
def install_statusline(apply: ApplyOption = False) -> None:
    """Add the Fylgja block to statusline.sh (the file in ~/dotfiles)."""
    import asyncio

    from fylgja.cli import install
    from fylgja.core.system import SubprocessRunner

    code = asyncio.run(
        install.run_install_statusline(apply=apply, runner=SubprocessRunner(), echo=typer.echo)
    )
    raise typer.Exit(code)


@install_app.command("keybinding")
def install_keybinding(
    apply: ApplyOption = False,
    key: Annotated[
        str, typer.Option("--key", help="The AeroSpace key to bind, such as alt-shift-t.")
    ] = "alt-shift-t",
) -> None:
    """Add the quick-todo binding to ~/.aerospace.toml (the file in ~/dotfiles)."""
    import asyncio

    from fylgja.cli import install
    from fylgja.core.system import SubprocessRunner

    code = asyncio.run(
        install.run_install_keybinding(
            apply=apply, runner=SubprocessRunner(), key=key, echo=typer.echo
        )
    )
    raise typer.Exit(code)


@install_app.command("skill")
def install_skill(apply: ApplyOption = False) -> None:
    """Install the fylgja-todo Claude skill into ~/dotfiles and link it into ~/.claude/skills."""
    import asyncio

    from fylgja.cli import install
    from fylgja.core.system import SubprocessRunner

    code = asyncio.run(
        install.run_install_skill(apply=apply, runner=SubprocessRunner(), echo=typer.echo)
    )
    raise typer.Exit(code)


@install_app.command("secret")
def install_secret(
    rotate: Annotated[bool, typer.Option("--rotate", help="Replace an existing secret.")] = False,
    show: Annotated[bool, typer.Option("--show", help="Print the secret.")] = False,
) -> None:
    """Create the API secret (~/.config/fylgja/api-secret, mode 0600); never printed by default."""
    from fylgja.cli import install

    raise typer.Exit(install.run_install_secret(rotate=rotate, show=show, echo=typer.echo))
