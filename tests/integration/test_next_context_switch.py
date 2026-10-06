"""Rule 5 of the ranking end to end (WP6.2): the way back to the workspace with the most focus
today that has an open todo and has gone quiet.

Focus events go through the real span engine and writer; the daemon's resolver refresh reads the
spans and hands the focus seconds of today to ``/now``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api_support import (
    T0,
    Win,
    client,
    focus_event,
    make_daemon,
    window_snapshot,
    windows_collector,
    workspace_snapshot,
)

from fylgja.core.daemon import Daemon
from fylgja.testing.fakes import FakeClock


@pytest.fixture(autouse=True)
def utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local time is UTC, so "today" starts at midnight UTC on the fake clock's day."""
    monkeypatch.setenv("TZ", "UTC")


async def _picture(daemon: Daemon, clock: FakeClock) -> None:
    """Forty minutes in workspace 6, then thirty in workspace 3 (still focused)."""
    wm = daemon.sink("wm")
    wm.emit(workspace_snapshot("wm", ["3", "6"], T0, focused="6"))
    wm.emit(window_snapshot("wm", [Win("30", "3", "Term", 300), Win("60", "6", "Term", 600)], T0))
    wm.emit(focus_event("wm", "60", "6", T0))
    await daemon.settle()
    await clock.advance(40 * 60)
    wm.emit(workspace_snapshot("wm", ["3", "6"], clock.now(), focused="3"))
    wm.emit(focus_event("wm", "30", "3", clock.now()))
    await daemon.settle()
    await clock.advance(30 * 60)
    await daemon.settle()


async def _next(daemon: Daemon) -> list[dict[str, Any]]:
    async with client(daemon) as http:
        response = await http.get("/next")
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def test_the_workspace_with_the_most_focus_that_has_an_open_todo_is_offered() -> None:
    clock = FakeClock(T0)
    daemon = make_daemon(windows_collector(), clock=clock)
    await daemon.start()
    try:
        await _picture(daemon, clock)
        assert await _next(daemon) == []  # no open todo anywhere yet
        await daemon.todos.add("pick this up", scope_kind="workspace", scope_ref="6")
        await daemon.settle()
        (item,) = await _next(daemon)
        assert (item["kind"], item["target_ref"], item["score"]) == ("context_switch", "6", 60.0)
        assert item["reason"] == (
            "workspace 6 has had the most focus today, holds an open todo "
            "and has been quiet for an hour"
        )
        assert daemon.state().focus_today["6"] == 40 * 60
    finally:
        await daemon.stop()


async def test_the_focused_workspace_is_not_a_way_back() -> None:
    clock = FakeClock(T0)
    daemon = make_daemon(windows_collector(), clock=clock)
    await daemon.start()
    try:
        await _picture(daemon, clock)
        await daemon.todos.add("here already", scope_kind="workspace", scope_ref="3")
        await daemon.settle()
        assert await _next(daemon) == []
        await clock.advance(60)
        daemon.sink("wm").emit(workspace_snapshot("wm", ["3", "6"], clock.now(), focused="6"))
        daemon.sink("wm").emit(focus_event("wm", "60", "6", clock.now()))
        await daemon.settle()
        await daemon.todos.add("there", scope_kind="workspace", scope_ref="6")
        await daemon.settle()
        assert [(i["kind"], i["target_ref"]) for i in await _next(daemon)] == [
            ("context_switch", "3")
        ]
    finally:
        await daemon.stop()
