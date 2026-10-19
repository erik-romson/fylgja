"""``fylgja journal`` against a fake HTTP server (J17; ``docs/api/journal.md``).

The CLI is an HTTP client: the tests assert on the request the fake server saw, on the rendering of
a fixed payload and on the exit codes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_cli_client import FakeDaemon, _configure_port, down_port  # noqa: F401
from typer.testing import CliRunner

from fylgja.cli import client, render
from fylgja.cli.app import app

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "cli"
THREAD: dict[str, Any] = json.loads((FIXTURES / "journal.json").read_text(encoding="utf-8"))
LISTING: dict[str, Any] = {
    "schema": "fylgja.journal/1",
    "enabled": True,
    "threads": [
        {
            "id": "0123456789abcdef",
            "name": "fylgja",
            "folder": "work/fylgja",
            "branch": "develop",
            "last_at": "2026-10-18T17:48:00Z",
            "next": [],
            "remember": [],
            "blocked": [],
            "open": 5,
        }
    ],
}
OFF = "fylgja: the journal is off; set [journal] enabled = true"

runner = CliRunner()


@pytest.fixture(autouse=True)
def utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("COLUMNS", "100")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


def invoke(routes: dict[str, Any], *args: str) -> tuple[Any, FakeDaemon]:
    with FakeDaemon(routes) as daemon:
        _configure_port(daemon.port)
        return runner.invoke(app, ["journal", *args]), daemon


def paths(daemon: FakeDaemon) -> list[str]:
    return [path for path, _ in daemon.requests]


def rendered(payload: dict[str, Any]) -> str:
    # The command renders for the real today; a day far from the fixture's gives its dates.
    return render.render_journal(payload, 100) + "\n"


def test_journal_prints_the_render_of_the_focused_checkout() -> None:
    result, daemon = invoke({"/journal/focused": THREAD})
    assert result.exit_code == 0, result.output
    assert result.stdout == rendered(THREAD)
    assert paths(daemon) == ["/journal/focused"]
    assert result.stdout.startswith("fylgja · develop\n")
    assert "Do next\n- Add the focused route" in result.stdout


def test_json_prints_the_answer_as_it_is() -> None:
    result, _ = invoke({"/journal/focused": THREAD}, "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == THREAD


def test_here_sends_the_quoted_current_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = tmp_path / "a b&c"
    folder.mkdir()
    monkeypatch.chdir(folder)
    expected = f"/journal/checkout?path={client.quote(str(Path.cwd()))}"
    result, daemon = invoke({expected: THREAD}, "--here")
    assert result.exit_code == 0, result.output
    assert paths(daemon) == [expected]
    assert result.stdout == rendered(THREAD)


def test_a_thread_id_reads_that_thread() -> None:
    result, daemon = invoke({"/journal/threads/0123456789abcdef": THREAD}, "0123456789abcdef")
    assert result.exit_code == 0, result.output
    assert paths(daemon) == ["/journal/threads/0123456789abcdef"]


def test_list_prints_the_threads_and_sends_days() -> None:
    result, daemon = invoke(
        {"/journal/threads": LISTING, "/journal/threads?days=30": LISTING}, "--list"
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == render.render_journal_list(LISTING, 100) + "\n"
    assert "fylgja" in result.stdout
    assert paths(daemon) == ["/journal/threads"]
    result, daemon = invoke({"/journal/threads?days=30": LISTING}, "--list", "--days", "30")
    assert result.exit_code == 0, result.output
    assert paths(daemon) == ["/journal/threads?days=30"]


def test_a_404_prints_the_daemons_detail_and_exits_1() -> None:
    with FakeDaemon({}) as daemon:
        daemon.raw["/journal/focused"] = (404, b'{"detail":"nothing is focused"}')
        _configure_port(daemon.port)
        result = runner.invoke(app, ["journal"])
    assert result.exit_code == client.EXIT_API_ERROR
    assert "fylgja: nothing is focused" in result.output


def test_a_409_says_the_journal_is_off_and_exits_1() -> None:
    with FakeDaemon({}) as daemon:
        daemon.raw["/journal/focused"] = (409, b'{"detail":"the journal is off"}')
        _configure_port(daemon.port)
        result = runner.invoke(app, ["journal"])
    assert result.exit_code == client.EXIT_API_ERROR
    assert OFF in result.output


def test_the_list_of_a_journal_that_is_off_says_so_and_exits_1() -> None:
    off = {"schema": "fylgja.journal/1", "enabled": False, "threads": []}
    result, _ = invoke({"/journal/threads": off}, "--list")
    assert result.exit_code == client.EXIT_API_ERROR
    assert OFF in result.output


def test_other_failures_keep_the_clients_message() -> None:
    with FakeDaemon({}) as daemon:
        daemon.raw["/journal/focused"] = (500, b"{}")
        _configure_port(daemon.port)
        result = runner.invoke(app, ["journal"])
    assert result.exit_code == client.EXIT_API_ERROR
    assert "500" in result.output


def test_two_selectors_are_a_usage_error() -> None:
    result, daemon = invoke({}, "--here", "--list")
    assert result.exit_code == 2
    assert daemon.requests == []


def test_days_without_list_is_a_usage_error() -> None:
    result, daemon = invoke({}, "--days", "30")
    assert result.exit_code == 2
    assert "--days needs --list" in result.output
    assert daemon.requests == []


def test_a_daemon_that_is_down_exits_3(down_port: int) -> None:  # noqa: F811
    result = runner.invoke(app, ["journal"])
    assert result.exit_code == client.EXIT_DAEMON_DOWN
    assert "daemon not running" in result.output
