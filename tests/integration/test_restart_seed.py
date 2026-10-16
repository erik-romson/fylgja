"""A restart re-applies a stored hook state only to sessions that hooks spoke for (D7, fact 14).

Each scenario runs two daemons on one database file: the first stores the state, the second
starts on it and its collector reports again.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from api_support import (
    T0,
    Synthetic,
    Win,
    activity_snapshot,
    client,
    focus_event,
    make_daemon,
    session,
    session_snapshot,
    window_snapshot,
)

from fylgja.collectors.base import Capability, CollectorContext
from fylgja.core.daemon import Daemon
from fylgja.models import (
    HookEvent,
    HookEventPayload,
    SessionActivity,
    SessionActivityEvent,
)
from fylgja.testing.fakes import FakeClock

EDITOR = Win("7", "3", "Editor", 70, "notes.md")
OTHER = Win("8", "3", "Mail", 80, "Inbox")
CLAUDE = "claude:a"
NORN = "norn:wf:1"


def _windows(context: CollectorContext) -> None:
    now = context.clock.now()
    context.sink.emit(window_snapshot("wm", [EDITOR, OTHER], now))
    context.sink.emit(focus_event("wm", "8", "3", now))


def _claude(state: str) -> Callable[[CollectorContext], None]:
    def emit(context: CollectorContext) -> None:
        now = context.clock.now()
        payload = session("claude", "a", pid=71, cwd="/work/notes", host_pid=70)
        context.sink.emit(session_snapshot("agents", [payload], now))
        context.sink.emit(activity_snapshot("agents", {CLAUDE: SessionActivity(state=state)}, now))

    return emit


def _norn(state: str | None) -> Callable[[CollectorContext], None]:
    """The norn run, with an activity of ``state`` when it is given (reported as an event)."""

    def emit(context: CollectorContext) -> None:
        now = context.clock.now()
        payload = session("norn", "wf:1", cwd="/work/flow")
        context.sink.emit(session_snapshot("agents", [payload], now))
        if state is not None:
            context.sink.emit(
                SessionActivityEvent(
                    source="agents",
                    entity_id=NORN,
                    observed_at=now + timedelta(seconds=5),
                    payload=SessionActivity(state=state, waiting_reason="the agent asked"),
                )
            )

    return emit


def _daemon(
    clock: FakeClock, emit: Callable[[CollectorContext], None], database: Path
) -> tuple[Daemon, Synthetic]:
    wm = Synthetic("wm", frozenset({Capability.WINDOWS}), emit=_windows)
    agents = Synthetic("agents", frozenset({Capability.SESSIONS}), emit=emit)
    return make_daemon(wm, agents, clock=clock, database=database), agents


async def _now(daemon: Daemon) -> dict[str, Any]:
    async with client(daemon) as http:
        response = await http.get("/now")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _state(body: dict[str, Any], key: str) -> str:
    entries = [s for s in body["sessions"] if s["key"] == key]
    assert len(entries) == 1, body["sessions"]
    state: str = entries[0]["state"]
    return state


async def _first_run(
    clock: FakeClock, emit: Callable[[CollectorContext], None], database: Path, then: str
) -> list[dict[str, Any]]:
    """Run a first daemon, make the session wait, stop it. Returns its ``waiting[]``."""
    daemon, agents = _daemon(clock, emit, database)
    await daemon.start()
    try:
        await daemon.settle()
        if then == "hook":
            agents.contexts[0].sink.emit(
                HookEvent(
                    source="agents",
                    entity_id=CLAUDE,
                    observed_at=clock.now(),
                    payload=HookEventPayload(
                        session_id="a", event="PermissionRequest", tool_name="Bash"
                    ),
                )
            )
            await daemon.settle()
        waiting: list[dict[str, Any]] = (await _now(daemon))["waiting"]
        assert len(waiting) == 1, waiting
        return waiting
    finally:
        await daemon.stop()


async def test_a_norn_run_that_resumes_after_a_restart_no_longer_waits(tmp_path: Path) -> None:
    """Scenario 1: the stored ``waiting_input`` is not pinned; the collector's state wins."""
    clock = FakeClock(T0)
    database = tmp_path / "fylgja.sqlite"
    (before,) = await _first_run(clock, _norn("waiting_input"), database, "activity")
    assert (before["kind"], before["target"]) == ("input", NORN)

    await clock.advance(60)
    second, _ = _daemon(clock, _norn("running"), database)
    await second.start()
    try:
        await second.settle()
        body = await _now(second)
        assert _state(body, NORN) == "running"
        assert body["waiting"] == []
    finally:
        await second.stop()


async def test_a_claude_session_keeps_its_hook_state_across_a_restart(tmp_path: Path) -> None:
    """Scenario 2: hooks spoke for it, so its stored state comes back over the collector's."""
    clock = FakeClock(T0)
    database = tmp_path / "fylgja.sqlite"
    await _first_run(clock, _claude("idle"), database, "hook")

    await clock.advance(60)
    second, _ = _daemon(clock, _claude("idle"), database)
    await second.start()
    try:
        await second.settle()
        body = await _now(second)
        assert _state(body, CLAUDE) == "waiting_permission"
        (entry,) = body["waiting"]
        assert (entry["kind"], entry["target"]) == ("permission", CLAUDE)
    finally:
        await second.stop()


async def test_a_norn_run_that_still_waits_keeps_its_since(tmp_path: Path) -> None:
    """Scenario 3: the same state reported again does not move ``since``."""
    clock = FakeClock(T0)
    database = tmp_path / "fylgja.sqlite"
    (before,) = await _first_run(clock, _norn("waiting_input"), database, "activity")

    await clock.advance(60)
    second, _ = _daemon(clock, _norn("waiting_input"), database)
    await second.start()
    try:
        await second.settle()
        (entry,) = (await _now(second))["waiting"]
        assert (entry["kind"], entry["target"]) == ("input", NORN)
        assert entry["since"] == before["since"]
    finally:
        await second.stop()
