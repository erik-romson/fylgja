"""``shown_waiting`` is the ``waiting[]`` list of ``/now`` (R1; ``fylgja.core.shown``).

The notifier reads the core's list, not the API's, so the two must stay one definition: the same
entries, in the same order, with the same availability filter.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from api_support import (
    T0,
    Synthetic,
    activity_snapshot,
    focus_event,
    make_daemon,
    session,
    session_snapshot,
    sessions_collector,
    windows_collector,
)

from fylgja.api.now import build_now
from fylgja.core.daemon import Daemon
from fylgja.core.shown import shown_sessions, shown_waiting
from fylgja.models import HealthState, HookEvent, HookEventPayload, SessionActivity
from fylgja.testing.fakes import FakeClock

KEY = "claude:a"


def _hook(source: str, key: str, event: str, at_seconds: float, **fields: Any) -> HookEvent:
    return HookEvent(
        source=source,
        entity_id=key,
        observed_at=T0 + timedelta(seconds=at_seconds),
        payload=HookEventPayload(session_id=key.split(":", 1)[1], event=event, **fields),
    )


def _triples(entries: Any) -> list[tuple[str, str, Any]]:
    return [(e.kind, e.target, e.since) for e in entries]


async def _degrade(daemon: Daemon, spec: Synthetic, clock: FakeClock) -> None:
    spec.doctor_state = HealthState.DEGRADED
    await clock.advance(1.0)  # the supervisor's first doctor probe
    await daemon.settle()


def _seed(daemon: Daemon, source: str, *natives: str) -> None:
    sink = daemon.sink(source)
    sink.emit(
        session_snapshot(source, [session("claude", n, cwd=f"/work/{n}") for n in natives], T0)
    )
    sink.emit(
        activity_snapshot(
            source, {f"claude:{n}": SessionActivity(state="idle") for n in natives}, T0
        )
    )


async def test_a_permission_prompt_is_one_entry_equal_to_the_now_entry() -> None:
    daemon = make_daemon(windows_collector(), sessions_collector())
    await daemon.start()
    try:
        _seed(daemon, "agents", "a")
        await daemon.settle()
        assert shown_waiting(daemon.state()) == []
        daemon.sink("agents").emit(_hook("agents", KEY, "PermissionRequest", 2, tool_name="Bash"))
        await daemon.settle()
        state = daemon.state()
        (entry,) = shown_waiting(state) or []
        assert (entry.kind, entry.target, entry.target_kind) == ("permission", KEY, "session")
        (shown,) = build_now(state).waiting or []
        assert (shown.kind, shown.target, shown.since) == (entry.kind, entry.target, entry.since)
        assert entry.since is not None
    finally:
        await daemon.stop()


async def test_the_list_equals_the_now_list_and_leaves_out_an_ended_session() -> None:
    daemon = make_daemon(windows_collector(), sessions_collector())
    await daemon.start()
    try:
        _seed(daemon, "agents", "a", "b", "c")
        await daemon.settle()
        sink = daemon.sink("agents")
        sink.emit(_hook("agents", "claude:a", "PermissionRequest", 2, tool_name="Bash"))
        sink.emit(_hook("agents", "claude:b", "UserPromptSubmit", 3))
        sink.emit(_hook("agents", "claude:b", "Stop", 4))
        sink.emit(_hook("agents", "claude:c", "PermissionRequest", 5, tool_name="Bash"))
        await daemon.settle()
        # c ends: the next complete snapshot no longer lists it
        sink.emit(
            session_snapshot(
                "agents",
                [session("claude", n, cwd=f"/work/{n}") for n in ("a", "b")],
                T0 + timedelta(seconds=6),
            )
        )
        await daemon.settle()
        state = daemon.state()
        entries = shown_waiting(state)
        assert entries is not None
        now_waiting = build_now(state).waiting
        assert now_waiting is not None
        assert _triples(entries) == [(w.kind, w.target, w.since) for w in now_waiting]
        targets = {e.target for e in entries}
        assert "claude:a" in targets
        assert "claude:b" in targets  # turn_done waits on the person
        assert "claude:c" not in targets
        assert "claude:c" not in {s.session_key for s in shown_sessions(state)}
    finally:
        await daemon.stop()


async def test_a_degraded_collector_leaves_out_only_its_own_entries() -> None:
    clock = FakeClock(T0)
    agents, more = sessions_collector("agents"), sessions_collector("more")
    daemon = make_daemon(windows_collector(), agents, more, clock=clock)
    await daemon.start()
    try:
        _seed(daemon, "agents", "a")
        _seed(daemon, "more", "m")
        await daemon.settle()
        daemon.sink("agents").emit(_hook("agents", "claude:a", "PermissionRequest", 2))
        daemon.sink("more").emit(_hook("more", "claude:m", "PermissionRequest", 2))
        await daemon.settle()
        before = daemon.state()
        assert {e.target for e in shown_waiting(before) or []} == {"claude:a", "claude:m"}
        assert {s.session_key for s in shown_sessions(before)} == {"claude:a", "claude:m"}
        await _degrade(daemon, agents, clock)
        state = daemon.state()
        assert {e.target for e in shown_waiting(state) or []} == {"claude:m"}
        assert {s.session_key for s in shown_sessions(state)} == {"claude:m"}
        now_waiting = build_now(state).waiting or []
        assert [w.target for w in now_waiting] == ["claude:m"]
    finally:
        await daemon.stop()


async def test_no_session_window_or_project_collector_gives_none() -> None:
    daemon = make_daemon()
    await daemon.start()
    try:
        await daemon.settle()
        state = daemon.state()
        assert shown_waiting(state) is None
        assert shown_sessions(state) == []
        assert build_now(state).waiting is None
    finally:
        await daemon.stop()


async def test_a_windows_only_daemon_still_has_a_list() -> None:
    daemon = make_daemon(windows_collector())
    await daemon.start()
    try:
        daemon.sink("wm").emit(focus_event("wm", "1", "1", T0))
        await daemon.settle()
        assert shown_waiting(daemon.state()) == []
    finally:
        await daemon.stop()
