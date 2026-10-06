"""WP6.2's done condition: ``fylgja next`` then ``fylgja next --feedback good`` against a running
in-process daemon, and the rating is on the top item of that list in ``suggestions_log``.

The daemon runs behind a real Uvicorn server on a free loopback port; the CLI runs in a worker
thread (it is a blocking ``urllib`` client) and keeps the last shown ``list_id`` in the state
directory.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import pytest
import uvicorn
from next_support import (
    FOCUSED,
    SECRET,
    SESSION_KEY,
    TODO_TEXT,
    WAITING_REASON,
    log_rows,
    world,
)
from typer.testing import CliRunner

from fylgja import paths
from fylgja.api.app import create_app, server_config
from fylgja.cli import client
from fylgja.cli.app import NEXT_STATE_FILE
from fylgja.cli.app import app as cli
from fylgja.core.daemon import Daemon


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Served:
    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon
        self.runner = CliRunner()

    async def fylgja(self, *args: str, code: int = 0) -> str:
        result = await asyncio.to_thread(self.runner.invoke, cli, list(args))
        assert result.exit_code == code, (args, result.output)
        return str(result.output)

    async def get(self, path: str) -> Any:
        return await asyncio.to_thread(client.get_json, path)


@pytest.fixture
async def served() -> AsyncIterator[Served]:
    port = free_port()
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(f"[api]\nport = {port}\n", encoding="utf-8")
    paths.api_secret_file().write_text(SECRET + "\n", encoding="utf-8")
    async with world(user_config={"api": {"port": port}}) as daemon:
        server = uvicorn.Server(server_config(create_app(daemon, secret=SECRET), daemon.config))
        task = asyncio.create_task(server.serve())
        while not server.started:  # noqa: ASYNC110 - Uvicorn offers no readiness event
            await asyncio.sleep(0.01)
        try:
            yield Served(daemon)
        finally:
            server.should_exit = True
            await task


async def test_next_then_feedback_good_is_found_on_the_top_item_in_the_log(served: Served) -> None:
    shown = await served.fylgja("next")
    listed_todo_id = (await served.get("/next"))["items"][1]["target_ref"]
    assert WAITING_REASON in shown
    assert f"todo marked next in workspace {FOCUSED}: {TODO_TEXT}" in shown
    assert listed_todo_id[:8] in shown
    assert shown.index(WAITING_REASON) < shown.index(TODO_TEXT)  # best first
    listed = await served.get("/next")

    answer = await served.fylgja("next", "--feedback", "good")
    assert "rated item 1" in answer
    assert "good" in answer

    await served.daemon.writer.flush()
    rows = log_rows(served.daemon, listed["list_id"])
    assert [(r["rank"], r["feedback"]) for r in rows] == [(1, "good"), (2, None)]
    assert rows[0]["target_ref"] == SESSION_KEY
    assert rows[0]["feedback_at"]
    assert {r["feedback"] for r in log_rows(served.daemon)} == {"good", None}


async def test_feedback_rates_the_list_the_cli_showed_not_a_newer_one(served: Served) -> None:
    await served.fylgja("next")
    shown = json.loads((paths.state_dir() / NEXT_STATE_FILE).read_text(encoding="utf-8"))
    assert shown["date"] == date.today().isoformat()
    await served.daemon.todos.add(
        "arrived later", scope_kind="workspace", scope_ref=FOCUSED, status="next"
    )
    await served.daemon.settle()
    newer = await served.get("/next")  # a client the CLI does not know about
    assert newer["list_id"] != shown["list_id"]

    await served.fylgja("next", "--feedback", "bad")
    await served.daemon.writer.flush()
    assert [r["feedback"] for r in log_rows(served.daemon, shown["list_id"])] == ["bad", None]
    assert {r["feedback"] for r in log_rows(served.daemon, newer["list_id"])} == {None}


async def test_the_last_list_replaces_the_one_before(served: Served) -> None:
    await served.fylgja("next")
    await served.daemon.todos.add("two", scope_kind="workspace", scope_ref=FOCUSED, status="next")
    await served.daemon.settle()
    await served.fylgja("next")
    current = await served.get("/next")
    stored = json.loads((paths.state_dir() / NEXT_STATE_FILE).read_text(encoding="utf-8"))
    assert stored["list_id"] == current["list_id"]


async def test_feedback_before_any_list_fails_clearly_and_posts_nothing(served: Served) -> None:
    output = await served.fylgja("next", "--feedback", "good", code=1)
    assert "run `fylgja next` first" in output
    await served.daemon.writer.flush()
    assert log_rows(served.daemon) == []


async def test_a_list_shown_on_another_day_is_not_rated(served: Served) -> None:
    await served.fylgja("next")
    target = paths.state_dir() / NEXT_STATE_FILE
    stored = json.loads(target.read_text(encoding="utf-8"))
    stored["date"] = (date.today() - timedelta(days=1)).isoformat()
    target.write_text(json.dumps(stored), encoding="utf-8")
    output = await served.fylgja("next", "--feedback", "good", code=1)
    assert "no list was shown today" in output
    await served.daemon.writer.flush()
    assert {r["feedback"] for r in log_rows(served.daemon)} == {None}


async def test_a_damaged_state_file_is_the_same_as_none(served: Served) -> None:
    target = paths.state_dir() / NEXT_STATE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    output = await served.fylgja("next", "--feedback", "good", code=1)
    assert "run `fylgja next` first" in output


async def test_a_bad_rating_is_a_usage_error(served: Served) -> None:
    await served.fylgja("next")
    output = await served.fylgja("next", "--feedback", "great", code=2)
    assert "good or bad" in output


async def test_a_list_the_daemon_never_logged_is_reported_by_the_daemon(served: Served) -> None:
    target = paths.state_dir() / NEXT_STATE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"date": date.today().isoformat(), "list_id": "nl-gone"}), encoding="utf-8"
    )
    output = await served.fylgja("next", "--feedback", "good", code=1)
    assert "404" in output


async def test_json_output_is_the_daemons_list(served: Served) -> None:
    printed = json.loads(await served.fylgja("next", "--json"))
    assert printed == await served.get("/next")
    rated = json.loads(await served.fylgja("next", "--feedback", "good", "--json"))
    assert (rated["list_id"], rated["rank"], rated["rating"]) == (printed["list_id"], 1, "good")


async def test_the_daemon_being_down_is_exit_3(tmp_path: Any) -> None:
    port = free_port()
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(f"[api]\nport = {port}\n", encoding="utf-8")
    paths.api_secret_file().write_text(SECRET + "\n", encoding="utf-8")
    runner = CliRunner()
    result = await asyncio.to_thread(runner.invoke, cli, ["next"])
    assert result.exit_code == 3
    assert "daemon not running" in result.output
    assert not (paths.state_dir() / NEXT_STATE_FILE).exists()
