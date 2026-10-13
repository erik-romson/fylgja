"""``fylgja items`` and ``fylgja rankers`` against a fake HTTP server (R-P11, A7).

The CLI is an HTTP client: the tests assert on the request the fake server saw (``q``, ``limit``,
and no ``rank`` for ``rankers``) and on the rendering of a fixed payload.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

import pytest
from test_cli_client import FakeDaemon, _configure_port, down_port  # noqa: F401
from typer.testing import CliRunner

from fylgja.cli import client, render
from fylgja.cli.app import app

runner = CliRunner()

ITEMS: dict[str, Any] = {
    "schema": "fylgja.items/1",
    "generated_at": "2026-10-05T12:00:00Z",
    "ranker": "frecency",
    "activity": {"working": True, "active_seconds_session": 60.0},
    "while_away": [],
    "items": [
        {
            "id": "aaaaaaaaaaaaaaaa",
            "key": "folder:/work/alpha/main",
            "kind": "folder",
            "name": "alpha/main",
            "tickets": ["CBS-1"],
            "score": 12.0,
            "explanation": "focus 300 · waiting +100",
            "parts": [],
            "places": [{"kind": "window", "ref": "1", "role": "ide", "label": "IntelliJ IDEA"}],
            "state": ["permission"],
            "seconds_today": 3900.0,
            "summary": "CBS-1 on feature, dirty",
        },
        {
            "id": "bbbbbbbbbbbbbbbb",
            "key": "app:com.example.app",
            "kind": "app",
            "name": "Example",
            "tickets": [],
            "score": 1.0,
            "explanation": "focus 20",
            "parts": [],
            "places": [],
            "state": [],
            "seconds_today": 20.0,
            "summary": "",
        },
    ],
    "days": [{"day": "2026-10-05", "items": ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"]}],
    "summaries_enabled": False,
    "total": 2,
    "shown": 2,
}

RANKERS: dict[str, Any] = {
    "schema": "fylgja.rankers/1",
    "default": "natural",
    "configured_default": "frecency",
    "rankers": [
        {
            "name": "frecency",
            "origin": "built-in",
            "enabled": True,
            "loaded": False,
            "error": "import_error: boom",
        },
        {"name": "recent", "origin": "built-in", "enabled": False, "loaded": False},
        {
            "name": "workspace",
            "origin": "external",
            "distribution": "fylgja-x",
            "version": "1.0",
            "description": "Workspace order",
            "enabled": True,
            "loaded": True,
        },
    ],
    "choices": [],
    "selected": "natural",
    "stale_choice": False,
}


def test_items_prints_the_day_groups_in_server_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "100")
    with FakeDaemon({"/items": ITEMS}) as daemon:
        _configure_port(daemon.port)
        result = runner.invoke(app, ["items"])
    assert result.exit_code == 0, result.output
    assert result.stdout == render.render_items(ITEMS, 100) + "\n"
    assert [path for path, _ in daemon.requests] == ["/items"]
    lines = result.stdout.splitlines()
    assert lines[0] == "ranker frecency · 2 of 2 items"
    assert lines[2] == "2026-10-05"
    assert lines.index(next(line for line in lines if "alpha/main" in line)) < lines.index(
        next(line for line in lines if "Example" in line)
    )


def test_items_json_is_the_answer_as_it_is() -> None:
    with FakeDaemon({"/items": ITEMS}) as daemon:
        _configure_port(daemon.port)
        result = runner.invoke(app, ["items", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == ITEMS


def test_search_limit_rank_and_days_are_sent_to_the_server() -> None:
    query = urllib.parse.urlencode({"rank": "recent", "days": "3", "q": "a b&c", "limit": "5"})
    with FakeDaemon({f"/items?{query}": ITEMS}) as daemon:
        _configure_port(daemon.port)
        result = runner.invoke(
            app,
            ["items", "--rank", "recent", "--days", "3", "--search", "a b&c", "--limit", "5"],
        )
    assert result.exit_code == 0, result.output
    path, _ = daemon.requests[0]
    parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    assert parsed == {"rank": ["recent"], "days": ["3"], "q": ["a b&c"], "limit": ["5"]}


def test_the_client_filters_nothing() -> None:
    answer = {**ITEMS, "query": "zzz"}
    with FakeDaemon({"/items?q=zzz": answer}) as daemon:
        _configure_port(daemon.port)
        result = runner.invoke(app, ["items", "--search", "zzz"])
    assert "alpha/main" in result.stdout  # whatever the server answered is shown
    assert "search 'zzz'" in result.stdout


def test_a_server_error_exits_1() -> None:
    with FakeDaemon({}) as daemon:
        _configure_port(daemon.port)
        result = runner.invoke(app, ["items", "--rank", "nope"])
    assert result.exit_code == client.EXIT_API_ERROR


def test_rankers_text_shows_a_disabled_built_in_and_a_load_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "100")
    with FakeDaemon({"/rankers": RANKERS}) as daemon:
        _configure_port(daemon.port)
        result = runner.invoke(app, ["rankers"])
    assert result.exit_code == 0, result.output
    assert result.stdout == render.render_rankers(RANKERS, 100) + "\n"
    assert [path for path, _ in daemon.requests] == ["/rankers"]  # no rank is sent
    assert "disabled" in result.stdout
    assert "import_error: boom" in result.stdout
    assert "default in effect: natural (configured: frecency, not loaded)" in result.stdout


def test_rankers_json_is_the_answer_as_it_is() -> None:
    with FakeDaemon({"/rankers": RANKERS}) as daemon:
        _configure_port(daemon.port)
        result = runner.invoke(app, ["rankers", "--json"])
    assert json.loads(result.stdout) == RANKERS


@pytest.mark.parametrize(
    "args", [["items"], ["items", "--json"], ["rankers"], ["rankers", "--json"]]
)
def test_a_daemon_that_is_down_exits_3(down_port: int, args: list[str]) -> None:  # noqa: F811
    result = runner.invoke(app, args)
    assert result.exit_code == client.EXIT_DAEMON_DOWN
    assert "daemon not running" in result.stderr
