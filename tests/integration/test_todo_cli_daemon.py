"""WP4.1's done condition: ``fylgja todo`` against a running in-process daemon.

The daemon runs behind a real Uvicorn server on a free loopback port, over a fake live view whose
focused workspace is known. The CLI runs in a worker thread (it is a blocking ``urllib`` client).
``todo add "x"``, ``todo ls``, ``todo done <id>`` and ``todo rm <id>`` are each checked against
``GET /todos`` and ``/now``.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import uvicorn
from api_support import (
    T0,
    Win,
    activity_snapshot,
    make_daemon,
    session,
    session_snapshot,
    sessions_collector,
    window_snapshot,
    windows_collector,
    workspace_snapshot,
)
from typer.testing import CliRunner

from fylgja import paths
from fylgja.api.app import create_app, server_config
from fylgja.cli import client
from fylgja.cli.app import app as cli
from fylgja.core.daemon import Daemon
from fylgja.models import SessionActivity

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"
FOCUSED = "3"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Served:
    def __init__(self, daemon: Daemon, port: int) -> None:
        self.daemon = daemon
        self.port = port
        self.runner = CliRunner()

    async def fylgja(self, *args: str, code: int = 0) -> str:
        result = await asyncio.to_thread(self.runner.invoke, cli, list(args))
        assert result.exit_code == code, (args, result.output)
        return str(result.output)

    async def get(self, path: str) -> Any:
        return await asyncio.to_thread(client.get_json, path)

    async def now_todos(self, workspace: str = FOCUSED) -> list[dict[str, Any]]:
        now = await self.get("/now")
        found = next(w for w in now["workspaces"] if w["name"] == workspace)
        return list(found.get("todos", []))


@pytest.fixture
async def served() -> AsyncIterator[Served]:
    port = free_port()
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(f"[api]\nport = {port}\n", encoding="utf-8")
    paths.api_secret_file().write_text(SECRET + "\n", encoding="utf-8")
    daemon = make_daemon(
        windows_collector(), sessions_collector(), user_config={"api": {"port": port}}
    )
    await daemon.start()
    wm, agents = daemon.sink("wm"), daemon.sink("agents")
    wm.emit(workspace_snapshot("wm", ["1", "2", FOCUSED], T0, focused=FOCUSED))
    wm.emit(window_snapshot("wm", [Win("100", FOCUSED, "Term", 500)], T0))
    agents.emit(
        session_snapshot(
            "agents",
            [session("claude", "abc-123", pid=501, cwd="/work/alpha", host_pid=500)],
            T0,
        )
    )
    agents.emit(activity_snapshot("agents", {"claude:abc-123": SessionActivity(state="idle")}, T0))
    await daemon.settle()
    server = uvicorn.Server(server_config(create_app(daemon, secret=SECRET), daemon.config))
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - Uvicorn offers no readiness event
        await asyncio.sleep(0.01)
    try:
        yield Served(daemon, port)
    finally:
        server.should_exit = True
        await task
        await daemon.stop()


async def test_add_ls_done_rm_round_trip(served: Served) -> None:
    # add "x": the new id is printed, and the item is scoped to the focused workspace
    added = await served.fylgja("todo", "add", "x")
    todo_id = added.strip()
    assert len(todo_id) == 36
    assert added.count("\n") == 1
    over_http = await served.get("/todos")
    assert [(t["id"], t["text"], t["status"]) for t in over_http] == [(todo_id, "x", "open")]
    assert (over_http[0]["scope_kind"], over_http[0]["scope_ref"]) == ("workspace", FOCUSED)
    assert [t["id"] for t in await served.now_todos()] == [todo_id]
    assert await served.now_todos("1") == []

    # ls: a table, and --json
    table = await served.fylgja("todo", "ls")
    for part in (todo_id[:8], "open", f"ws {FOCUSED}", " x"):
        assert part in table
    listed = json.loads(await served.fylgja("todo", "ls", "--json"))
    assert [t["id"] for t in listed] == [todo_id]

    # done <id>: gone from the default listing and from /now, still in GET /todos
    assert (await served.fylgja("todo", "done", todo_id)).strip() == f"done {todo_id}"
    assert (await served.fylgja("todo", "ls")).strip() == "no todos"
    assert "x" in await served.fylgja("todo", "ls", "--all")
    assert [(t["id"], t["status"]) for t in await served.get("/todos")] == [(todo_id, "done")]
    assert [t["id"] for t in await served.get("/todos?status=open,next")] == []
    assert await served.now_todos() == []
    done_at = (await served.get("/todos"))[0]["done_at"]
    await served.fylgja("todo", "done", todo_id)  # idempotent
    assert (await served.get("/todos"))[0]["done_at"] == done_at

    # rm <id>: soft delete, gone everywhere
    assert (await served.fylgja("todo", "rm", todo_id)).strip() == f"removed {todo_id}"
    assert await served.get("/todos") == []
    assert await served.get("/todos?status=done") == []
    assert await served.now_todos() == []
    await served.fylgja("todo", "rm", todo_id, code=1)
    await served.daemon.writer.flush()
    assert served.daemon.view.todos == {}


async def test_each_step_is_visible_in_now_as_it_happens(served: Served) -> None:
    first = (await served.fylgja("todo", "add", "first")).strip()
    second = (await served.fylgja("todo", "add", "second", "--next")).strip()
    assert [(t["id"], t["status"]) for t in await served.now_todos()] == [
        (second, "next"),
        (first, "open"),
    ]
    await served.fylgja("todo", "done", first)
    assert [t["id"] for t in await served.now_todos()] == [second]
    await served.fylgja("todo", "rm", second)
    assert await served.now_todos() == []
    assert len(await served.get("/todos")) == 1  # the done one remains


async def test_scoping_flags_and_prefixes(served: Served) -> None:
    ws = (await served.fylgja("todo", "add", "on two", "--ws", "2")).strip()
    session_ = (await served.fylgja("todo", "add", "on session", "--session", "abc")).strip()
    project = (await served.fylgja("todo", "add", "on project", "--project", "/work/alpha")).strip()
    by_name = (await served.fylgja("todo", "add", "by name", "--project", "alpha")).strip()
    focused = (await served.fylgja("todo", "add", "on focus")).strip()
    todos = {t["id"]: t for t in await served.get("/todos")}
    assert [(todos[i]["scope_kind"], todos[i]["scope_ref"]) for i in (ws, session_, project)] == [
        ("workspace", "2"),
        ("session", "claude:abc-123"),
        ("project", "/work/alpha"),
    ]
    assert todos[by_name]["scope_ref"] == "/work/alpha"
    assert todos[focused]["scope_ref"] == FOCUSED

    def ids(items: list[dict[str, Any]]) -> list[str]:
        return [t["id"] for t in items]

    assert ids(json.loads(await served.fylgja("todo", "ls", "--ws", "2", "--json"))) == [ws]
    assert ids(json.loads(await served.fylgja("todo", "ls", "--session", "abc", "--json"))) == [
        session_
    ]
    assert ids(json.loads(await served.fylgja("todo", "ls", "--project", "alpha", "--json"))) == [
        project,
        by_name,
    ]
    now = await served.get("/now")
    assert [t["id"] for t in now["sessions"][0]["todos"]] == [session_]
    assert [t["id"] for t in now["projects"][0]["todos"]] == [project, by_name]

    # an id prefix is enough while it is unique
    await served.fylgja("todo", "done", ws[:10])
    assert {t["id"]: t["status"] for t in await served.get("/todos")}[ws] == "done"
    await served.fylgja("todo", "done", "zz-no-such", code=1)


async def test_the_cli_reports_what_the_daemon_refuses(served: Served) -> None:
    await served.fylgja("todo", "add", "x", "--ws", "1", "--project", "/p", code=2)
    await served.fylgja("todo", "add", "x", "--project", "no-such-project", code=1)
    await served.fylgja("todo", "add", "x", "--session", "no-such-session", code=1)
    await served.fylgja("todo", "add", "", code=1)
    assert await served.get("/todos") == []


async def test_without_focus_add_fails_with_a_hint(served: Served) -> None:
    served.daemon.sink("wm").emit(workspace_snapshot("wm", ["1", "2", FOCUSED], T0, focused=None))
    await served.daemon.settle()
    assert served.daemon.todos.focused_workspace() is None
    output = await served.fylgja("todo", "add", "x", code=1)
    assert "no workspace is focused" in output
    assert await served.get("/todos") == []


async def test_a_missing_secret_stops_a_write_but_not_a_read(served: Served) -> None:
    paths.api_secret_file().unlink()
    output = await served.fylgja("todo", "add", "x", "--ws", "1", code=1)
    assert "fylgja install secret" in output
    assert await served.fylgja("todo", "ls") == "no todos\n"


def test_the_cli_exits_3_when_the_daemon_is_down(isolated_env: Any) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(f"[api]\nport = {free_port()}\n", encoding="utf-8")
    paths.api_secret_file().write_text(SECRET + "\n", encoding="utf-8")
    runner = CliRunner()
    calls: list[Callable[[], Any]] = [
        lambda: runner.invoke(cli, ["todo", "ls"]),
        lambda: runner.invoke(cli, ["todo", "add", "x", "--ws", "1"]),
        lambda: runner.invoke(cli, ["todo", "done", "0" * 8 + "-0000-0000-0000-" + "0" * 12]),
        lambda: runner.invoke(cli, ["todo", "rm", "0" * 8 + "-0000-0000-0000-" + "0" * 12]),
    ]
    for call in calls:
        result = call()
        assert result.exit_code == 3, result.output
        assert "daemon not running" in result.output
