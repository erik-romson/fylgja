"""Attention alerts through a real daemon (R13–R16 of ``docs/decisions/0055``).

Synthetic collectors (``wm`` for windows, ``agents`` for sessions, ``pres`` for presence) feed
records through the ingest gate, so hook events reach the session state machine, the writer, the
live view and the notifier as in production. Time is a :class:`FakeClock` (monotonic and wall time
move together unless a test suspends the machine) and every command is a
:class:`FakeProcessRunner` script: no notification is ever posted. Only calls whose ``argv[0]`` is
the configured command count. The scenario numbers of the plan are in each test's docstring.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from api_support import (
    T0,
    Synthetic,
    Win,
    activity_snapshot,
    focus_event,
    make_daemon,
    session,
    session_snapshot,
    window_snapshot,
)

from fylgja.collectors.base import Capability, CollectorContext
from fylgja.core.daemon import Daemon
from fylgja.core.notify import NotifyStats, Snapshot
from fylgja.models import (
    FocusEvent,
    FocusPayload,
    HookEvent,
    HookEventPayload,
    PresenceEvent,
    PresencePayload,
    SessionActivity,
    SessionActivityEvent,
)
from fylgja.testing.fakes import FakeClock, FakeProcessRunner, settle

NOTIFY = "/usr/local/bin/notify"
PHONE = "/usr/local/bin/phone"
DESK = [NOTIFY, "{title}", "{message}", "{url}", "{key}"]
ENABLED: dict[str, Any] = {"notify": {"enabled": True, "desk_command": DESK}}
START = ["start:writer", "start:live_view", "start:state_engine", "start:supervisor"]
"""The start lifecycle of ``test_daemon_composition.py``."""

EDITOR = Win("7", "3", "Editor", 70, "notes.md")
OTHER = Win("8", "3", "Mail", 80, "Inbox")
KEY = "claude:a"
NORN = "norn:wf:1"


# --------------------------------------------------------------------------------------------
# The synthetic collectors (as in test_waiting_now.py)
# --------------------------------------------------------------------------------------------


def _windows(context: CollectorContext) -> None:
    now = context.clock.now()
    context.sink.emit(window_snapshot("wm", [EDITOR, OTHER], now))
    context.sink.emit(focus_event("wm", "8", "3", now))


def _sessions(context: CollectorContext) -> None:
    now = context.clock.now()
    context.sink.emit(
        session_snapshot(
            "agents", [session("claude", "a", pid=71, cwd="/work/notes", host_pid=70)], now
        )
    )
    context.sink.emit(activity_snapshot("agents", {KEY: SessionActivity(state="idle")}, now))


def _runner(*, returncode: int = 0, hang: bool = False, scripted: bool = True) -> FakeProcessRunner:
    runner = FakeProcessRunner()
    if scripted:
        runner.script(NOTIFY, returncode=returncode, hang=hang)
    return runner


def _daemon(
    clock: FakeClock,
    runner: FakeProcessRunner,
    *extra: Synthetic,
    user_config: dict[str, Any] | None = ENABLED,
    **options: Any,
) -> tuple[Daemon, Synthetic]:
    wm = Synthetic("wm", frozenset({Capability.WINDOWS}), emit=_windows)
    agents = Synthetic("agents", frozenset({Capability.SESSIONS}), emit=_sessions)
    daemon = make_daemon(
        wm, agents, *extra, user_config=user_config, clock=clock, runner=runner, **options
    )
    return daemon, agents


async def _settle(daemon: Daemon) -> None:
    """Commit what is queued, then let the notifier's task run."""
    await daemon.settle()
    await settle()


@asynccontextmanager
async def _running(daemon: Daemon) -> AsyncIterator[Daemon]:
    await daemon.start()
    try:
        await _settle(daemon)
        yield daemon
    finally:
        await daemon.stop()


def _notified(runner: FakeProcessRunner, command: str = NOTIFY) -> list[tuple[str, ...]]:
    return [call for call in runner.calls if call[0] == command]


def _hook(clock: FakeClock, event: str, key: str = KEY, **fields: Any) -> HookEvent:
    return HookEvent(
        source="agents",
        entity_id=key,
        observed_at=clock.now(),
        payload=HookEventPayload(session_id=key.partition(":")[2], event=event, **fields),
    )


async def _permission(daemon: Daemon, agents: Synthetic, clock: FakeClock) -> None:
    agents.contexts[0].sink.emit(_hook(clock, "PermissionRequest", tool_name="Bash"))
    await _settle(daemon)


def _task_names() -> set[str]:
    return {task.get_name() for task in asyncio.all_tasks()}


# --------------------------------------------------------------------------------------------
# Timing from since
# --------------------------------------------------------------------------------------------


async def test_a_permission_alerts_once_when_its_grace_has_passed() -> None:
    """Scenario 1 (F1): the alert comes 20 s after the permission, with every placeholder."""
    clock = FakeClock(T0)
    runner = _runner()
    daemon, agents = _daemon(clock, runner)
    async with _running(daemon):
        assert daemon.notifier is not None
        assert "fylgja-notify" in _task_names()
        await _permission(daemon, agents, clock)
        await clock.advance(19)
        assert _notified(runner) == []
        await clock.advance(1)
        (call,) = _notified(runner)
        assert call[1] == "Fylgja"
        assert call[2].startswith("Claude")
        assert "waiting for permission" in call[2]
        assert call[3] == "http://127.0.0.1:7319/#recall"
        assert call[4] == KEY
        assert runner.timeouts[runner.calls.index(call)] == 5.0
        stats = daemon.notify_stats()
        assert (stats.sent, stats.failed, stats.last_outcome, stats.open) == (1, 0, "sent", 1)


async def test_an_answer_within_the_grace_sends_nothing() -> None:
    """Scenario 2 (F5): the permission is answered at 5 s; the episode closes silently."""
    clock = FakeClock(T0)
    runner = _runner()
    daemon, agents = _daemon(clock, runner)
    async with _running(daemon):
        await _permission(daemon, agents, clock)
        await clock.advance(5)
        agents.contexts[0].sink.emit(_hook(clock, "PostToolUse", tool_name="Bash"))
        await _settle(daemon)
        await clock.advance(60)
        assert _notified(runner) == []
        assert daemon.notify_stats().open == 0


async def test_a_norn_run_waiting_for_input_alerts_with_its_key() -> None:
    """Scenario 3 (F3): a run in failure recovery, on the plugin path."""
    clock = FakeClock(T0)
    runner = _runner()
    daemon, agents = _daemon(clock, runner)
    async with _running(daemon):
        sink = agents.contexts[0].sink
        sink.emit(
            session_snapshot("agents", [session("norn", "wf:1", cwd="/work/flow")], clock.now())
        )
        sink.emit(
            SessionActivityEvent(
                source="agents",
                entity_id=NORN,
                observed_at=clock.now(),
                payload=SessionActivity(state="waiting_input", waiting_reason="failure recovery"),
            )
        )
        await _settle(daemon)
        await clock.advance(19)
        assert _notified(runner) == []
        await clock.advance(1)
        (call,) = _notified(runner)
        assert call[4] == NORN
        assert call[2].startswith("Norn")
        assert "failure recovery" in call[2]


# --------------------------------------------------------------------------------------------
# Delivery failures
# --------------------------------------------------------------------------------------------


async def test_a_failing_command_is_counted_and_only_the_reminder_tries_again() -> None:
    """Scenario 4 (R7, R13): no retry; one reminder 600 s after the first alert, then quiet."""
    clock = FakeClock(T0)
    runner = _runner(returncode=1)
    daemon, agents = _daemon(clock, runner)
    async with _running(daemon):
        await _permission(daemon, agents, clock)
        await clock.advance(20)
        assert len(_notified(runner)) == 1
        stats = daemon.notify_stats()
        assert (stats.failed, stats.sent) == (1, 0)
        assert stats.last_failure_kind == "command_failed"
        assert stats.last_outcome == "command_failed"
        assert stats.last_failure_at == T0 + timedelta(seconds=20)
        await clock.advance_to(619)
        assert len(_notified(runner)) == 1
        await clock.advance(1)
        calls = _notified(runner)
        assert len(calls) == 2
        assert calls[1][1] == "Fylgja: still waiting"
        await clock.advance_to(2000)
        assert len(_notified(runner)) == 2
        assert daemon.notify_stats().failed == 2


async def test_a_missing_command_is_command_missing() -> None:
    """Scenario 5 (R13)."""
    clock = FakeClock(T0)
    runner = _runner(scripted=False)
    daemon, agents = _daemon(clock, runner)
    async with _running(daemon):
        await _permission(daemon, agents, clock)
        await clock.advance(20)
        assert len(_notified(runner)) == 1
        stats = daemon.notify_stats()
        assert (stats.last_failure_kind, stats.failed) == ("command_missing", 1)


async def test_a_hanging_command_times_out_on_the_daemon_clock_and_leaves_nothing_running() -> None:
    """Scenario 6 (R13): ``timeout_seconds`` (5 s) after the send starts."""
    clock = FakeClock(T0)
    runner = _runner(hang=True)
    daemon, agents = _daemon(clock, runner)
    async with _running(daemon):
        await _permission(daemon, agents, clock)
        await clock.advance(20)
        assert len(_notified(runner)) == 1
        assert runner.active == 1
        assert daemon.notify_stats().last_outcome is None  # still running
        await clock.advance(5)
        assert daemon.notify_stats().last_failure_kind == "command_timeout"
        assert runner.active == 0


# --------------------------------------------------------------------------------------------
# Off by default
# --------------------------------------------------------------------------------------------


async def test_the_default_daemon_has_no_notifier_and_starts_no_task() -> None:
    """Scenario 7 (R16): ``enabled = false`` builds nothing and runs nothing."""
    clock = FakeClock(T0)
    runner = _runner()
    daemon, agents = _daemon(clock, runner, user_config=None)
    assert daemon.notifier is None
    async with _running(daemon):
        assert daemon.lifecycle == START
        assert "fylgja-notify" not in _task_names()
        await _permission(daemon, agents, clock)
        await clock.advance(60)
        assert [call for call in runner.calls if call[0].startswith(NOTIFY)] == []
        assert daemon.notify_stats() == NotifyStats()


# --------------------------------------------------------------------------------------------
# What is on screen and whether the person is away
# --------------------------------------------------------------------------------------------


async def test_an_alert_is_held_while_its_window_has_focus() -> None:
    """Scenario 8 (R5, R6, D5): released by the focus change, not by a tick."""
    clock = FakeClock(T0)
    runner = _runner()
    daemon, agents = _daemon(clock, runner)
    async with _running(daemon):
        assert daemon.view.sessions[KEY].window_id == "7"
        wm = daemon.sink("wm")
        wm.emit(focus_event("wm", "7", "3", clock.now()))
        await _settle(daemon)
        await _permission(daemon, agents, clock)
        await clock.advance(60)
        assert _notified(runner) == []
        await clock.advance(10)  # between two ticks
        wm.emit(focus_event("wm", "8", "3", clock.now()))
        await _settle(daemon)
        assert len(_notified(runner)) == 1


async def test_a_private_window_holds_nothing_and_the_tick_releases_the_alert() -> None:
    """Scenario 9 (R5, fact 7): a privacy-ignored focus is never stored, so only the tick sees
    that the session's window lost focus."""
    clock = FakeClock(T0)
    runner = _runner()
    private = {**ENABLED, "privacy": {"ignore_title_patterns": ["(?i)secret"]}}
    daemon, agents = _daemon(clock, runner, user_config=private)
    async with _running(daemon):
        wm = daemon.sink("wm")
        wm.emit(focus_event("wm", "7", "3", clock.now()))
        await _settle(daemon)
        await _permission(daemon, agents, clock)
        await clock.advance(30)
        assert _notified(runner) == []
        wm.emit(
            FocusEvent(
                source="wm",
                entity_id="focus",
                observed_at=clock.now(),
                payload=FocusPayload(window_id="9", workspace="3", title="secret notes"),
            )
        )
        await clock.advance(30)  # one tick
        assert daemon.spans.tracker.focused is None
        assert daemon.view.focused is not None
        assert daemon.view.focused.window_id == "7"  # the stored focus is stale (fact 7)
        assert len(_notified(runner)) == 1


async def test_while_away_the_alert_goes_to_the_away_command_with_the_kind_only() -> None:
    """Scenario 10 (R8, F6): idle fills ``suspended``; ``away_detail = "kind"``."""
    clock = FakeClock(T0)
    runner = _runner()
    runner.script(PHONE)
    away = {"notify": {**ENABLED["notify"], "away_command": [PHONE, "{message}"]}}
    pres = Synthetic("pres", frozenset({Capability.CONTEXT}))
    daemon, agents = _daemon(clock, runner, pres, user_config=away)
    async with _running(daemon):
        daemon.sink("pres").emit(
            PresenceEvent(
                source="pres",
                entity_id="idle",
                observed_at=clock.now(),
                payload=PresencePayload(state="idle"),
            )
        )
        await _settle(daemon)
        assert daemon.spans.tracker.suspended
        await _permission(daemon, agents, clock)
        await clock.advance(20)
        assert _notified(runner, PHONE) == [(PHONE, "Claude needs permission")]
        assert _notified(runner) == []


# --------------------------------------------------------------------------------------------
# Restart, timers and sleep
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("restart_at", "after"), [(100.0, 5.0), (12.0, 8.0)])
async def test_after_a_restart_a_waiting_entry_is_announced_once(
    tmp_path: Path, restart_at: float, after: float
) -> None:
    """Scenario 11 (R3, F7): grace counts from the stored ``since``; an entry already past it is
    due after the 5 s floor, a younger one at the end of its remaining grace (20 s after
    ``since``)."""
    clock = FakeClock(T0)
    database = tmp_path / "fylgja.sqlite"
    runner = _runner()
    first, agents = _daemon(clock, runner, database=database)
    async with _running(first):
        await _permission(first, agents, clock)
        await clock.advance(10)
    assert _notified(runner) == []

    await clock.advance_to(restart_at)
    second, _ = _daemon(clock, runner, database=database)
    async with _running(second):
        assert second.view.sessions[KEY].state == "waiting_permission"
        await clock.advance(after - 1)
        assert _notified(runner) == []
        await clock.advance(1)
        assert len(_notified(runner)) == 1
        await clock.advance(60)
        assert len(_notified(runner)) == 1


async def test_the_alert_does_not_wait_for_the_daemon_tick() -> None:
    """Scenario 12 (R15, fact 5): the notifier keeps its own deadline."""
    clock = FakeClock(T0)
    runner = _runner()
    daemon, agents = _daemon(clock, runner, tick=3600.0)
    async with _running(daemon):
        await _permission(daemon, agents, clock)
        await clock.advance(19)
        assert _notified(runner) == []
        await clock.advance(1)
        assert len(_notified(runner)) == 1


async def test_sleep_does_not_count_towards_the_grace() -> None:
    """Scenario 13 (F8): the grace runs on the monotonic clock."""
    clock = FakeClock(T0)
    runner = _runner()
    daemon, agents = _daemon(clock, runner)
    async with _running(daemon):
        await _permission(daemon, agents, clock)
        await clock.advance(5)
        clock.suspend(3600)
        await clock.advance(10)
        assert _notified(runner) == []
        await clock.advance(5)
        assert len(_notified(runner)) == 1


# --------------------------------------------------------------------------------------------
# A hanging send, stop, coalescing
# --------------------------------------------------------------------------------------------


async def test_a_hanging_send_does_not_block_the_engine() -> None:
    """Scenario 14 (R15): the notifier runs on its own task."""
    clock = FakeClock(T0)
    runner = _runner(hang=True)
    daemon, agents = _daemon(clock, runner)
    async with _running(daemon):
        await _permission(daemon, agents, clock)
        await clock.advance(20)
        assert runner.active == 1
        new = Win("10", "5", "Terminal", 100, "build")
        daemon.sink("wm").emit(window_snapshot("wm", [EDITOR, OTHER, new], clock.now()))
        await asyncio.wait_for(daemon.settle(), 5)
        assert "10" in daemon.view.windows
        assert runner.active == 1


async def test_stopping_during_a_hanging_send_leaves_nothing_running() -> None:
    """Scenario 15 (F9): teardown cancels the notifier and its command."""
    clock = FakeClock(T0)
    runner = _runner(hang=True)
    daemon, agents = _daemon(clock, runner)
    await daemon.start()
    try:
        await _settle(daemon)
        await _permission(daemon, agents, clock)
        await clock.advance(20)
        assert runner.active == 1
    finally:
        await asyncio.wait_for(daemon.stop(), 10)
    assert runner.active == 0
    assert "fylgja-notify" not in _task_names()


async def test_two_sessions_due_together_give_one_alert() -> None:
    """Scenario 16 (R9): one call for two families; ``{key}`` is empty."""
    clock = FakeClock(T0)
    runner = _runner()
    daemon, agents = _daemon(clock, runner)
    async with _running(daemon):
        sink = agents.contexts[0].sink
        sink.emit(
            session_snapshot(
                "agents",
                [
                    session("claude", "a", pid=71, cwd="/work/notes", host_pid=70),
                    session("claude", "b", cwd="/work/other"),
                ],
                clock.now(),
            )
        )
        await _settle(daemon)
        sink.emit(_hook(clock, "PermissionRequest", tool_name="Bash"))
        sink.emit(_hook(clock, "PermissionRequest", "claude:b", tool_name="Edit"))
        await _settle(daemon)
        await clock.advance(20)
        (call,) = _notified(runner)
        assert call[2].startswith("2 sessions wait for you")
        assert call[4] == ""


# --------------------------------------------------------------------------------------------
# Logs and a failing evaluation
# --------------------------------------------------------------------------------------------


async def test_the_logs_carry_the_outcome_and_the_key_but_no_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scenario 17 (R14): the command fails, so both the INFO and the WARNING record exist."""
    caplog.set_level(logging.INFO, logger="fylgja.notify")
    clock = FakeClock(T0)
    runner = _runner(returncode=1)
    daemon, agents = _daemon(clock, runner)
    async with _running(daemon):
        project = daemon.view.sessions[KEY].project_path
        assert project is not None
        folder = Path(project).name
        await _permission(daemon, agents, clock)
        await clock.advance(20)
        (call,) = _notified(runner)
    assert folder in call[2]  # the desk message names the folder; the logs must not
    records = [r for r in caplog.records if r.name == "fylgja.notify"]
    assert {r.levelno for r in records} == {logging.INFO, logging.WARNING}
    text = "\n".join(r.getMessage() for r in records)
    assert "command_failed" in text
    assert KEY in text
    for secret in (call[2], folder, project, "waiting for permission", NOTIFY, call[3], "Fylgja"):
        assert secret not in text, secret
    assert str(list(call)) not in text


async def test_an_evaluation_that_raises_is_logged_once_and_the_loop_goes_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Scenario 18 (index §9.3): one record per exception type, without the exception's text;
    a later evaluation that works still sends its alert."""
    caplog.set_level(logging.INFO, logger="fylgja.notify")
    original = Daemon._attention
    broken = [True]

    def attention(self: Daemon) -> Snapshot:
        if broken[0]:
            raise RuntimeError("a path such as /work/notes")
        return original(self)

    monkeypatch.setattr(Daemon, "_attention", attention)
    clock = FakeClock(T0)
    runner = _runner()
    daemon, agents = _daemon(clock, runner)
    async with _running(daemon):
        await _permission(daemon, agents, clock)
        await clock.advance(60)  # the start, the permission and two ticks all failed
        await _settle(daemon)  # and whatever the tick's resolver refresh woke
        assert _notified(runner) == []
        records = [r for r in caplog.records if r.name == "fylgja.notify"]
        assert len(records) == 1
        assert "RuntimeError" in records[0].getMessage()
        assert "/work/notes" not in records[0].getMessage()
        broken[0] = False
        assert daemon.notifier is not None
        daemon.notifier.wake()
        await _settle(daemon)  # first seen 60 s after since: due after the 5 s floor
        await clock.advance(4)
        assert _notified(runner) == []
        await clock.advance(1)
        assert len(_notified(runner)) == 1
