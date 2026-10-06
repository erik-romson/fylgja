"""Golden renderings of the CLI tables from fixture payloads (WP1.10).

The goldens in ``tests/fixtures/cli/`` come from ``scripts/gen_cli_goldens.py``; these tests only
compare, and never write them.
"""

from __future__ import annotations

import json
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
