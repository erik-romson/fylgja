"""Golden renderings of the CLI tables from fixture payloads (WP1.10).

The goldens in ``tests/fixtures/cli/`` come from ``scripts/gen_cli_goldens.py``; these tests only
compare, and never write them.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from fylgja.cli import render

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "cli"


def _json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _golden(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8").removesuffix("\n")


NOW = _json("now.json")
COLLECTORS = _json("collectors.json")


@pytest.mark.parametrize("width", [100, 60])
def test_goldens(width: int) -> None:
    assert render.render_now(NOW, width) == _golden(f"now-{width}.txt")
    assert render.render_workspaces(NOW["workspaces"], width) == _golden(f"workspaces-{width}.txt")
    assert render.render_workspace(NOW["workspaces"][1], NOW["sessions"], width) == _golden(
        f"workspace-2-{width}.txt"
    )
    assert render.render_sessions(NOW["sessions"], width) == _golden(f"sessions-{width}.txt")
    assert render.render_collectors(COLLECTORS, width) == _golden(f"collectors-{width}.txt")


def test_the_focused_workspace_and_window_are_marked() -> None:
    text = render.render_workspaces(NOW["workspaces"])
    assert "2*" in text
    assert "1*" not in text
    assert "3*" not in text
    assert "101*" in render.render_workspace(NOW["workspaces"][1], [])


def test_the_state_shows_the_waiting_reason_only_for_waiting_states() -> None:
    sessions = [
        {"key": "a:1", "kind": "a", "state": "working", "waiting_reason": "stale"},
        {"key": "a:2", "kind": "a", "state": "waiting_input", "waiting_reason": "question"},
    ]
    lines = render.render_sessions(sessions).splitlines()
    assert "working" in lines[1]
    assert "stale" not in lines[1]
    assert "waiting_input (question)" in lines[2]


def test_a_missing_section_is_said_not_hidden() -> None:
    text = render.render_now({"generated_at": "2026-10-05T12:00:00Z", "collectors": []})
    assert "unavailable: no collector reports windows" in text
    assert "unavailable: no collector reports sessions" in text
    assert "focused" not in text
    assert "degraded" not in text


def test_empty_lists_say_so() -> None:
    assert render.render_workspaces([]) == "no workspaces"
    assert render.render_sessions([]) == "no sessions"
    assert render.render_collectors([]) == "no collectors"
    text = render.render_now({"workspaces": [], "sessions": []})
    assert "no workspaces" in text
    assert "no sessions" in text


def test_newlines_and_tabs_never_break_a_row() -> None:
    sessions = [{"key": "a:1", "kind": "a", "one_liner": "first\nsecond\tthird"}]
    lines = render.render_sessions(sessions).splitlines()
    assert len(lines) == 2
    assert "first second third" in lines[1]


def test_a_cell_that_does_not_fit_ends_in_an_ellipsis() -> None:
    sessions = [{"key": "a:1", "kind": "a", "one_liner": "x" * 200}]
    line = render.render_sessions(sessions, 50).splitlines()[1]
    assert line.endswith("…")
    assert len(line) <= 50


def test_a_workspace_without_windows_or_placed_sessions() -> None:
    workspace = {"name": "9", "label": "—", "one_liner": "—", "windows": [], "sessions": ["x:1"]}
    text = render.render_workspace(workspace, [])
    assert "no windows" in text
    assert "sessions: x:1" in text


def test_the_project_falls_back_to_the_cwd_and_a_dash() -> None:
    sessions = [
        {"key": "a:1", "kind": "a", "cwd": "/somewhere/else/"},
        {"key": "a:2", "kind": "a"},
    ]
    lines = render.render_sessions(sessions).splitlines()
    assert "else" in lines[1]
    assert "—" in lines[2]


def test_collectors_without_a_distribution_or_with_odd_values() -> None:
    rows = [
        {
            "name": "x",
            "origin": "built-in",
            "state": "enabled",
            "api_version": 1,
            "capabilities": [],
            "health": {"state": "ok"},
        },
        {
            "name": "y",
            "origin": "external",
            "distribution": "dist",
            "state": "disabled",
            "api_version": "not_loaded",
            "capabilities": "not_loaded",
            "health": "not_loaded",
        },
    ]
    lines = render.render_collectors(rows).splitlines()
    assert "built-in" in lines[1]
    assert "(" not in lines[1]
    assert "external (dist)" in lines[2]


def test_next_lists_the_items_best_first_with_short_targets() -> None:
    text = render.render_next(
        {
            "list_id": "nl-1",
            "items": [
                {"rank": 1, "kind": "input", "target_ref": "claude:a", "reason": "waiting"},
                {"rank": 2, "kind": "todo", "target_ref": "0123456789abcdef", "reason": "t"},
                {"rank": 3, "kind": "build_failed", "target_ref": "/work/repo", "reason": "b"},
                {"rank": 4, "kind": "unread", "reason": "u"},
            ],
        },
        100,
    )
    lines = text.splitlines()
    assert lines[0].split() == ["#", "WHAT", "TARGET", "WHY"]
    assert [line.split()[:3] for line in lines[1:]] == [
        ["1", "input", "claude:a"],
        ["2", "todo", "01234567"],
        ["3", "build_failed", "repo"],
        ["4", "unread", "\u2014"],
    ]


def test_next_with_nothing_says_so() -> None:
    assert render.render_next({"list_id": "nl-1", "items": []}) == "nothing is waiting for you"
    assert render.render_next({}) == "nothing is waiting for you"


def test_render_items_groups_by_day_and_flags_a_fallback() -> None:
    payload = {
        "ranker": "recent",
        "ranker_fallback": "natural",
        "shown": 2,
        "total": 5,
        "query": "alpha",
        "items": [
            {
                "id": "a",
                "name": "alpha/main",
                "tickets": ["CBS-1", "CBS-2"],
                "seconds_today": 3900,
                "places": [{"label": "IDE"}, {"label": "IDE"}, {"label": "Terminal"}],
                "state": ["permission", "dirty"],
                "summary": "on feature\nbranch",
                "explanation": "focus 300",
            },
            {"id": "b", "name": "Example", "seconds_today": 0},
        ],
        "days": [
            {"day": "2026-10-05", "items": ["a", "missing"]},
            {"day": "2026-10-04", "items": ["b"]},
        ],
    }
    lines = render.render_items(payload, 80).splitlines()
    assert lines[0] == "ranker recent (failed; natural answered) · 2 of 5 items · search 'alpha'"
    assert lines[2] == "2026-10-05"
    assert lines[3] == "  alpha/main · CBS-1,CBS-2 · 1h05m · IDE,Terminal · permission,dirty"
    assert lines[4] == "    on feature branch"
    assert lines[5] == "    · focus 300"
    assert lines[7] == "2026-10-04"
    assert lines[8] == "  Example · — · 0s · — · —"


def test_render_items_strips_control_characters() -> None:
    payload = {
        "ranker": "natural",
        "items": [
            {
                "id": "a",
                "name": "re\x9bpo\x1b]52;c;cm0gLXJm\x07",
                "places": [{"label": "IDE\x1b[2J"}],
                "summary": "on\x1b[31m main",
            }
        ],
        "days": [{"day": "2026-10-05", "items": ["a"]}],
    }
    lines = render.render_items(payload, 100).splitlines()
    assert lines[3] == "  repo]52;c;cm0gLXJm · — · 0s · IDE[2J · —"
    assert lines[4] == "    on[31m main"


def test_render_items_without_items_says_so() -> None:
    assert render.render_items({"ranker": "natural", "items": [], "days": []}).endswith("no items")


def test_render_rankers_marks_the_default_and_the_reason_a_ranker_is_off() -> None:
    payload = {
        "default": "recent",
        "configured_default": "recent",
        "rankers": [
            {"name": "recent", "origin": "built-in", "enabled": True, "loaded": True},
            {
                "name": "x",
                "origin": "external",
                "distribution": "d",
                "version": "2",
                "enabled": True,
                "loaded": False,
            },
        ],
    }
    lines = render.render_rankers(payload).splitlines()
    assert lines[1].startswith("recent*")
    assert "external (d@2)" in lines[2]
    assert "not loaded" in lines[2]
    assert lines[-1] == "default in effect: recent"
    assert render.render_rankers({"default": "natural", "rankers": []}) == (
        "no rankers\ndefault in effect: natural"
    )


# --- the work journal ---------------------------------------------------------------------------

JOURNAL = _json("journal.json")
JOURNAL_TODAY = date(2026, 10, 18)


@pytest.fixture
def utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Local time is UTC, as ``scripts/gen_cli_goldens.py`` renders the journal golden."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


@pytest.mark.usefixtures("utc")
def test_relative_day_says_today_yesterday_or_the_date() -> None:
    assert render.relative_day("2026-10-18T17:48:00Z", JOURNAL_TODAY) == "today at 17:48"
    assert render.relative_day("2026-10-17T09:10:00Z", JOURNAL_TODAY) == "yesterday at 09:10"
    assert render.relative_day("2026-10-13T10:00:00Z", JOURNAL_TODAY) == "Tue 13 Oct at 10:00"
    assert render.relative_day("not a time", JOURNAL_TODAY) == "—"


@pytest.mark.usefixtures("utc")
def test_the_journal_golden() -> None:
    assert render.render_journal(JOURNAL, 100, JOURNAL_TODAY) == _golden("journal-100.txt")


@pytest.mark.usefixtures("utc")
def test_empty_journal_sections_are_left_out() -> None:
    empty = {
        "name": "repo",
        "branch": "main",
        "where": {"state": "missing"},
        "open": {"next": [], "remember": [], "blocked": []},
        "done_recent": [],
        "entries": [],
    }
    assert render.render_journal(empty, 100, JOURNAL_TODAY) == "repo · main"


@pytest.mark.usefixtures("utc")
def test_blocked_shows_its_items_and_no_more_line_when_all_are_shown() -> None:
    payload = {
        "name": "repo",
        "branch": "main",
        "open": {
            "next": [],
            "remember": [],
            "blocked": [{"text": "Waiting for the key", "shown": True}],
        },
    }
    assert render.render_journal(payload, 100, JOURNAL_TODAY) == (
        "repo · main\n\nBlocked\n- Waiting for the key"
    )


@pytest.mark.usefixtures("utc")
def test_control_characters_never_reach_the_terminal() -> None:
    osc52 = "\x1b]52;c;cm0gLXJm\x07"
    payload = {
        "name": f"re\x9bpo{osc52}",
        "branch": "main",
        "where": {"text": f"Fixing{osc52} the parser"},
        "open": {
            "next": [{"text": "Step\x1b[2J one", "shown": True}],
            "remember": [],
            "blocked": [],
        },
    }
    shown = render.render_journal(payload, 100, JOURNAL_TODAY)
    assert (
        shown
        == "repo]52;c;cm0gLXJm · main\n\nWhere you are\nFixing]52;c;cm0gLXJm the parser\n\n"
        + ("Do next\n- Step[2J one")
    )
    listed = render.render_journal_list({"threads": [{"name": f"a{osc52}", "branch": "b\x1b"}]})
    assert not any(char in listed for char in "\x1b\x07\x9b")


@pytest.mark.usefixtures("utc")
def test_the_thread_list_has_one_line_per_thread() -> None:
    payload = {
        "threads": [
            {
                "id": "0123456789abcdef",
                "name": "repo",
                "folder": "w/repo",
                "branch": "main",
                "last_at": "2026-10-18T17:48:00Z",
                "open": 4,
            }
        ]
    }
    lines = render.render_journal_list(payload, 100, JOURNAL_TODAY).splitlines()
    assert lines[0].split() == ["NAME", "BRANCH", "FOLDER", "LAST", "WORKED", "OPEN"]
    assert lines[1].split() == ["repo", "main", "w/repo", "today", "at", "17:48", "4"]
    assert render.render_journal_list({"threads": []}, 100) == "no journal threads"
