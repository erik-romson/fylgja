"""The session state machine (WP2.3, A11): the reducer, the engine and the recorded sequences.

* **table-driven**: every row of the WP2.3 state table, from every starting state;
* **ordering**: hook events sort by ``received_at_ns``, then ``observed_at``; every permutation of a
  sequence ends in the state of its newest event;
* **fallback**: the registry's state applies only before the first hook;
* **seen**: ``turn_done`` becomes ``seen`` when the window gains focus after the Stop, not before;
* **sequences** (``tests/fixtures/hooks/``, built by ``scripts/gen_hook_fixtures.py``): every
  ratatosk scenario and a synthesized one, driven through the real normalizer and the engine;
* **A11**: a ``/clear`` rotation links the predecessor;
* **the store**: derived records give the writer the state, ``state_since``, ``waiting_reason`` and
  the one-liner.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from inbox_support import make_envelope

from fylgja import paths
from fylgja.collectors.claude.normalize import normalize, parse_envelope
from fylgja.core import sessions
from fylgja.core.sessions import (
    FocusSignal,
    HookSignal,
    RegistrySignal,
    SessionState,
    SessionStateEngine,
    activity_of,
    ns_of,
    reduce,
    signal_of,
)
from fylgja.core.writer import Writer
from fylgja.models import (
    FocusEvent,
    FocusPayload,
    HookEvent,
    HookEventPayload,
    Record,
    SessionActivity,
    SessionActivityEvent,
    SessionActivitySnapshot,
    SessionEvent,
    SessionPayload,
    SessionSnapshot,
    SnapshotItem,
    TodoEvent,
    TodoPayload,
)
from fylgja.store import db

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "hooks"
SCENARIOS = sorted(path.stem for path in FIXTURES.glob("*.json"))


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def hook(event: str, n: float = 1, **fields: Any) -> HookSignal:
    """A hook signal received ``n`` seconds after ``T0``."""
    return HookSignal(event=event, received_at_ns=ns_of(at(n)), observed_at=at(n), **fields)


def fold(events: list[HookSignal], start: SessionState | None = None) -> SessionState:
    state = start
    for event in events:
        state = reduce(state, event)
    assert state is not None
    return state


# --------------------------------------------------------------------------------------------
# The reducer, row by row
# --------------------------------------------------------------------------------------------

TABLE: list[tuple[str, dict[str, Any], str, str | None, str | None, str | None]] = [
    # (event, fields, state, tool, target, waiting_reason)
    ("UserPromptSubmit", {}, "working", None, None, None),
    ("PreToolUse", {"tool_name": "Bash"}, "running_tool", "Bash", None, None),
    (
        "PreToolUse",
        {"tool_name": "Write", "target": "a.txt"},
        "running_tool",
        "Write",
        "a.txt",
        None,
    ),
    (
        "PermissionRequest",
        {"tool_name": "Write", "target": "a.txt"},
        "waiting_permission",
        "Write",
        "a.txt",
        "approve Write",
    ),
    ("PermissionRequest", {}, "waiting_permission", None, None, "approve"),
    (
        "Notification",
        {"notification_type": "permission_prompt"},
        "waiting_permission",
        None,
        None,
        "approve",
    ),
    (
        "Notification",
        {"notification_type": "agent_needs_input"},
        "waiting_input",
        None,
        None,
        "agent needs input",
    ),
    (
        "Notification",
        {"notification_type": "elicitation_dialog"},
        "waiting_input",
        None,
        None,
        "question dialog",
    ),
    ("Stop", {}, "turn_done", None, None, None),
    ("Notification", {"notification_type": "idle_prompt"}, "idle", None, None, None),
    ("StopFailure", {}, "error", None, None, None),
    ("SessionEnd", {}, "ended", None, None, None),
    # the tool ran, failed or was refused: the agent carries on
    ("PostToolUse", {"tool_name": "Bash"}, "working", None, None, None),
    ("PostToolUseFailure", {"tool_name": "Bash"}, "working", None, None, None),
    ("PermissionDenied", {"tool_name": "Bash"}, "working", None, None, None),
]

STARTS: dict[str, SessionState | None] = {
    "unknown": None,
    "registry-idle": reduce(None, RegistrySignal("idle", at(-5))),
    "working": fold([hook("UserPromptSubmit", -4)]),
    "running_tool": fold([hook("PreToolUse", -4, tool_name="Bash")]),
    "waiting_permission": fold([hook("PermissionRequest", -4, tool_name="Bash")]),
    "waiting_input": fold([hook("Notification", -4, notification_type="elicitation_dialog")]),
    "turn_done": fold([hook("Stop", -4)]),
    "seen": reduce(fold([hook("Stop", -4)]), FocusSignal(at(-3))),
    "error": fold([hook("StopFailure", -4)]),
    "ended": fold([hook("SessionEnd", -4)]),
}


@pytest.mark.parametrize("start", STARTS)
@pytest.mark.parametrize(("event", "fields", "state", "tool", "target", "reason"), TABLE)
def test_every_row_of_the_state_table(
    start: str,
    event: str,
    fields: dict[str, Any],
    state: str,
    tool: str | None,
    target: str | None,
    reason: str | None,
) -> None:
    before = STARTS[start]
    after = reduce(before, hook(event, 1, **fields))
    if state == "idle" and start == "turn_done":
        assert after.state == "turn_done"  # see test_a_turn_done_is_not_overwritten_by_idleness
        return
    if state == "waiting_permission" and "tool_name" not in fields:
        # a permission prompt without a tool keeps the call it interrupts
        carried = before is not None and before.state in ("running_tool", "waiting_permission")
        tool = before.current_tool if carried and before is not None else None
        target = before.current_target if carried and before is not None else None
        reason = f"approve {tool}" if tool else "approve"
    assert (after.state, after.current_tool, after.current_target, after.waiting_reason) == (
        state,
        tool,
        target,
        reason,
    )
    assert after.hooked
    restarted = before is None or before.state != state
    assert after.since == (at(1) if restarted else before.since if before else None)


def test_a_turn_done_is_not_overwritten_by_idleness() -> None:
    done = fold([hook("Stop", 1)])
    after = reduce(done, hook("Notification", 2, notification_type="idle_prompt"))
    assert (after.state, after.since) == ("turn_done", at(1))
    assert after.order == hook("Notification", 2).order  # the event was consumed
    # ...but once the turn has been seen, idleness applies
    seen = reduce(done, FocusSignal(at(3)))
    assert seen.state == "seen"
    assert reduce(seen, hook("Notification", 4, notification_type="idle_prompt")).state == "idle"


def test_a_permission_prompt_keeps_the_tool_call_it_interrupts() -> None:
    running = fold([hook("PreToolUse", 1, tool_name="Write", target="a.txt")])
    asked = reduce(running, hook("Notification", 2, notification_type="permission_prompt"))
    assert (asked.state, asked.current_tool, asked.current_target) == (
        "waiting_permission",
        "Write",
        "a.txt",
    )
    assert asked.waiting_reason == "approve Write"
    again = reduce(asked, hook("PermissionRequest", 3, tool_name="Write", target="a.txt"))
    assert again.since == at(2)  # the state did not change, so it did not restart
    other = reduce(again, hook("PermissionRequest", 4, tool_name="Bash"))
    assert (other.current_tool, other.current_target) == ("Bash", None)


def test_the_tool_does_not_leak_into_other_states() -> None:
    running = fold([hook("PreToolUse", 1, tool_name="Bash", target="x")])
    for after in ("PostToolUse", "Stop", "UserPromptSubmit", "SessionEnd", "StopFailure"):
        state = reduce(running, hook(after, 2))
        assert (state.current_tool, state.current_target) == (None, None), after


@pytest.mark.parametrize(
    "event",
    [
        hook("SessionStart"),
        hook("SubagentStart"),
        hook("SubagentStop"),
        hook("TaskCreated"),
        hook("PreCompact"),
        hook("CwdChanged"),
        hook("StatusLine"),
        hook("Notification"),
        hook("Notification", notification_type="auth_success"),
        hook("Notification", notification_type="agent_completed"),
    ],
)
def test_events_that_name_no_state_change_nothing(event: HookSignal) -> None:
    for start in (None, STARTS["working"], STARTS["registry-idle"]):
        before = start
        after = reduce(before, event)
        if before is None:
            assert after == SessionState()  # nothing known, nothing claimed
            assert not after.hooked
        else:
            assert after is before


# --------------------------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------------------------

SEQUENCE = [
    hook("UserPromptSubmit", 1),
    hook("PreToolUse", 2, tool_name="Bash"),
    hook("PermissionRequest", 3, tool_name="Bash"),
    hook("PostToolUse", 4, tool_name="Bash"),
    hook("Stop", 5),
]


@pytest.mark.parametrize("order", list(itertools.permutations(range(len(SEQUENCE)))))
def test_every_arrival_order_ends_in_the_state_of_the_newest_event(order: tuple[int, ...]) -> None:
    final = fold([SEQUENCE[i] for i in order])
    assert final.state == "turn_done"
    assert final.since == at(5)
    assert final.order == SEQUENCE[-1].order


def test_a_late_event_never_moves_the_state_backwards() -> None:
    done = fold([hook("Stop", 5)])
    for late in (hook("UserPromptSubmit", 4), hook("PreToolUse", 3, tool_name="Bash")):
        assert reduce(done, late) is done


def test_events_sort_by_received_at_ns_then_observed_at() -> None:
    first = HookSignal("UserPromptSubmit", received_at_ns=10, observed_at=at(2))
    same_ns_earlier_time = HookSignal("Stop", received_at_ns=10, observed_at=at(1))
    later_ns = HookSignal("PreToolUse", received_at_ns=11, observed_at=at(0), tool_name="Bash")
    assert sorted([later_ns, first, same_ns_earlier_time], key=lambda e: e.order) == [
        same_ns_earlier_time,
        first,
        later_ns,
    ]
    # received_at_ns decides first: 11 beats 10 although its observed_at is older
    assert fold([first, later_ns]).state == "running_tool"
    assert fold([later_ns, first]).state == "running_tool"
    # equal received_at_ns: observed_at decides
    assert fold([first, same_ns_earlier_time]).state == "working"
    # the same key twice (a replay): the later arrival applies, and it is idempotent
    once = fold([first])
    assert fold([first], once) == once


# --------------------------------------------------------------------------------------------
# The registry fallback
# --------------------------------------------------------------------------------------------


def test_the_registry_state_applies_only_before_the_first_hook() -> None:
    state = reduce(None, RegistrySignal("idle", at(1)))
    assert (state.state, state.since, state.hooked) == ("idle", at(1), False)
    waiting = reduce(
        state,
        RegistrySignal("waiting", at(2), waiting_reason="approve", current_tool="Bash"),
    )
    assert (waiting.state, waiting.since, waiting.waiting_reason, waiting.current_tool) == (
        "waiting",
        at(2),
        "approve",
        "Bash",
    )
    same = reduce(waiting, RegistrySignal("waiting", at(3)))
    assert same.since == at(2)  # not restarted by an unchanged state
    assert reduce(waiting, RegistrySignal(None, at(4))) is waiting  # an unknown status says nothing

    hooked = reduce(waiting, hook("UserPromptSubmit", 5))
    assert hooked.state == "working"
    assert reduce(hooked, RegistrySignal("idle", at(6))) is hooked
    assert reduce(hooked, RegistrySignal("waiting", at(7))) is hooked


def test_an_event_that_names_no_state_does_not_end_the_fallback() -> None:
    state = reduce(None, RegistrySignal("busy", at(1)))
    state = reduce(state, hook("SessionStart", 2))
    assert not state.hooked
    assert reduce(state, RegistrySignal("idle", at(3))).state == "idle"


def test_a_session_with_hooks_only_has_a_state() -> None:
    state = fold([hook("UserPromptSubmit", 1), hook("PreToolUse", 2, tool_name="Read")])
    assert (state.state, state.current_tool, state.hooked) == ("running_tool", "Read", True)


# --------------------------------------------------------------------------------------------
# turn_done -> seen
# --------------------------------------------------------------------------------------------


def test_turn_done_becomes_seen_when_the_window_gains_focus_after_the_stop() -> None:
    done = fold([hook("Stop", 5)])
    seen = reduce(done, FocusSignal(at(6)))
    assert (seen.state, seen.since) == ("seen", at(6))
    assert seen.hooked
    assert reduce(seen, FocusSignal(at(7))) is seen  # a second focus changes nothing


def test_focus_before_or_at_the_stop_does_not_mark_it_seen() -> None:
    done = fold([hook("Stop", 5)])
    assert reduce(done, FocusSignal(at(4))) is done
    assert reduce(done, FocusSignal(at(5))) is done


def test_focus_means_nothing_in_any_other_state() -> None:
    for name in (
        "working",
        "running_tool",
        "waiting_permission",
        "waiting_input",
        "error",
        "ended",
    ):
        state = STARTS[name]
        assert reduce(state, FocusSignal(at(100))) is state
    registry = STARTS["registry-idle"]
    assert reduce(registry, FocusSignal(at(100))) is registry
    assert reduce(None, FocusSignal(at(100))) == SessionState()


def test_a_new_turn_after_seen_starts_the_cycle_again() -> None:
    seen = reduce(fold([hook("Stop", 5)]), FocusSignal(at(6)))
    assert reduce(seen, hook("UserPromptSubmit", 7)).state == "working"
    again = reduce(reduce(seen, hook("Stop", 8)), FocusSignal(at(9)))
    assert (again.state, again.since) == ("seen", at(9))


# --------------------------------------------------------------------------------------------
# activity_of and signal_of
# --------------------------------------------------------------------------------------------


def test_activity_of_lays_the_state_over_the_registry_activity() -> None:
    base = SessionActivity(
        state="working",
        current_tool="Bash",
        current_target="rm -rf build",
        title="Fix",
        open_task="clean",
        facts={"turns": 3},
    )
    asked = fold([hook("PermissionRequest", 1, tool_name="Bash")])
    merged = activity_of(asked, base)
    assert (merged.state, merged.waiting_reason) == ("waiting_permission", "approve Bash")
    assert merged.current_target == "rm -rf build"  # the registry knows more about the same tool
    assert (merged.title, merged.open_task, merged.facts) == ("Fix", "clean", {"turns": 3})
    other = activity_of(fold([hook("PermissionRequest", 1, tool_name="Edit")]), base)
    assert (other.current_tool, other.current_target) == ("Edit", None)
    done = activity_of(fold([hook("Stop", 1)]), base)
    assert (done.state, done.current_tool, done.current_target) == ("turn_done", None, None)
    assert activity_of(done).state == "turn_done"  # no base: just the state


def test_signal_of_reads_the_normalized_event() -> None:
    record = HookEvent(
        source="claude",
        entity_id="claude:s",
        observed_at=at(3.25),
        payload=HookEventPayload(
            session_id="s",
            event="PreToolUse",
            tool_name="Edit",
            edited_paths=("/work/src/module.py",),
        ),
    )
    signal = signal_of(record)
    assert (signal.event, signal.tool_name, signal.target) == ("PreToolUse", "Edit", "module.py")
    assert signal.received_at_ns == ns_of(at(3.25))
    assert ns_of(T0) == 1_791_201_600_000_000_000
    assert ns_of(at(0.000001)) - ns_of(T0) == 1000


# --------------------------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------------------------

KEY = "claude:s-1"


class Rig:
    """An engine whose derived records land in a list and whose windows are a dict."""

    def __init__(self, windows: dict[str, str] | None = None) -> None:
        self.emitted: list[Record] = []
        self.windows = {} if windows is None else windows
        self.engine = SessionStateEngine(self.emitted.append, self.windows.get)

    def apply(self, record: Record) -> Record:
        return self.engine.apply(record)

    def activities(self) -> list[SessionActivityEvent]:
        return [r for r in self.emitted if isinstance(r, SessionActivityEvent)]

    def announced(self) -> list[SessionEvent]:
        return [r for r in self.emitted if isinstance(r, SessionEvent)]


def hook_record(
    event: str,
    n: float = 1,
    *,
    key: str = KEY,
    pid: int | None = None,
    cwd: str | None = None,
    **fields: Any,
) -> HookEvent:
    kind, _, native_id = key.partition(":")
    return HookEvent(
        source=kind,
        entity_id=key,
        observed_at=at(n),
        payload=HookEventPayload(session_id=native_id, event=event, pid=pid, cwd=cwd, **fields),
    )


def registry_snapshot(
    n: float,
    activity: SessionActivity,
    *,
    key: str = KEY,
    payload: SessionPayload | None = None,
) -> SessionActivitySnapshot:
    return SessionActivitySnapshot(
        source="claude",
        entity_id="all",
        observed_at=at(n),
        complete=True,
        items=(SnapshotItem(entity_id=key, payload=activity),),
    )


def held(rig: Rig, key: str = KEY) -> SessionState:
    """The engine's state of ``key``, which must exist."""
    state = rig.engine.state_of(key)
    assert state is not None
    return state


def payloads_of(record: Record) -> list[SessionActivity]:
    assert isinstance(record, SessionActivitySnapshot)
    return [item.payload for item in record.items]


def focus(n: float, window_id: str | None) -> FocusEvent:
    return FocusEvent(
        source="aerospace",
        entity_id="focus",
        observed_at=at(n),
        payload=FocusPayload(window_id=window_id, workspace="1"),
    )


def test_a_hook_event_is_stored_as_it_is_and_derives_the_state() -> None:
    rig = Rig()
    record = hook_record("UserPromptSubmit", 1, pid=900, cwd="/work/app")
    assert rig.apply(record) is record
    (announced,) = rig.announced()
    assert announced.payload == SessionPayload(
        kind="claude", native_id="s-1", pid=900, cwd="/work/app"
    )
    (derived,) = rig.activities()
    assert (derived.entity_id, derived.source, derived.observed_at) == (KEY, "claude", at(1))
    assert derived.payload.state == "working"
    state = held(rig)
    assert state.state == "working"


def test_a_session_with_hooks_only_still_exists_with_its_state() -> None:
    rig = Rig()
    for n, (event, fields) in enumerate(
        [
            ("UserPromptSubmit", {}),
            ("PreToolUse", {"tool_name": "Read"}),
            ("PermissionRequest", {"tool_name": "Write", "edited_paths": ("/w/a.txt",)}),
        ],
        start=1,
    ):
        rig.apply(hook_record(event, n, pid=900, **fields))
    assert len(rig.announced()) == 1  # announced once, on first sight
    states = [a.payload.state for a in rig.activities()]
    assert states == ["working", "running_tool", "waiting_permission"]
    last = rig.activities()[-1].payload
    assert (last.current_tool, last.current_target, last.waiting_reason) == (
        "Write",
        "a.txt",
        "approve Write",
    )
    assert rig.engine.keys() == {KEY}


def test_an_event_that_changes_nothing_visible_emits_nothing() -> None:
    rig = Rig()
    rig.apply(hook_record("Stop", 1))
    rig.apply(hook_record("Stop", 2))  # the second one changes nothing the record carries
    rig.apply(hook_record("SubagentStop", 3))
    rig.apply(hook_record("Notification", 4, notification_type="idle_prompt"))
    assert [a.payload.state for a in rig.activities()] == ["turn_done"]


def test_a_statusline_reading_is_not_a_hook_event() -> None:
    rig = Rig()
    rig.apply(hook_record("StatusLine", 1, context_pct=40.0))
    assert rig.emitted == []
    assert rig.engine.keys() == set()


def test_the_registry_state_stands_until_the_first_hook() -> None:
    rig = Rig()
    registry = SessionActivity(state="waiting", waiting_reason="approve", title="Fix")
    snapshot = registry_snapshot(1, registry)
    assert rig.apply(snapshot) is snapshot  # nothing to overlay: the very same record
    state = held(rig)
    assert (state.state, state.hooked) == ("waiting", False)
    assert rig.emitted == []

    rig.apply(registry_snapshot(2, SessionActivity(state="idle", title="Fix")))
    state = held(rig)
    assert state.state == "idle"


def test_after_the_first_hook_the_registry_cannot_undo_the_hook_state() -> None:
    rig = Rig()
    base = SessionActivity(
        state="idle", title="Fix", last_prompt="p", current_tool="Bash", current_target="ls"
    )
    rig.apply(registry_snapshot(1, base))
    rig.apply(hook_record("PermissionRequest", 2, tool_name="Bash"))
    (derived,) = rig.activities()
    # merged over the registry's activity: title and the registry's target for the same tool survive
    assert derived.payload == base.model_copy(
        update={
            "state": "waiting_permission",
            "waiting_reason": "approve Bash",
            "current_tool": "Bash",
            "current_target": "ls",
        }
    )
    assert rig.announced() == []  # the registry already lists it

    later = registry_snapshot(3, SessionActivity(state="idle", title="Fix", last_prompt="q"))
    rewritten = rig.apply(later)
    assert isinstance(rewritten, SessionActivitySnapshot)
    (item,) = rewritten.items
    assert (item.payload.state, item.payload.waiting_reason) == (
        "waiting_permission",
        "approve Bash",
    )
    assert item.payload.last_prompt == "q"  # everything else the registry says stands
    assert later.items[0].payload.state == "idle"  # the original record was not mutated

    rig.apply(hook_record("Stop", 4))
    (again,) = payloads_of(rig.apply(registry_snapshot(5, SessionActivity(state="busy"))))
    assert again.state == "turn_done"


def test_a_snapshot_of_sessions_without_hooks_passes_through_untouched() -> None:
    rig = Rig()
    snapshot = registry_snapshot(1, SessionActivity(state="working", title="A"))
    assert rig.apply(snapshot) is snapshot
    event = SessionActivityEvent(
        source="runs",
        entity_id="norn:r1",
        observed_at=at(1),
        payload=SessionActivity(state="running", title="Pipeline"),
    )
    assert rig.apply(event) is event
    assert rig.engine.keys() == {KEY, "norn:r1"}


def test_an_activity_event_of_a_hooked_session_gets_the_hook_state() -> None:
    rig = Rig()
    rig.apply(hook_record("Stop", 1))
    event = SessionActivityEvent(
        source="claude",
        entity_id=KEY,
        observed_at=at(2),
        payload=SessionActivity(state="idle", title="Fix"),
    )
    merged = rig.apply(event)
    assert isinstance(merged, SessionActivityEvent)
    assert (merged.payload.state, merged.payload.title) == ("turn_done", "Fix")
    assert merged.dedup_key != event.dedup_key  # the key follows the payload that is stored
    assert (
        merged.dedup_key
        == SessionActivityEvent(
            source="claude", entity_id=KEY, observed_at=at(2), payload=merged.payload
        ).dedup_key
    )


def test_other_records_pass_through() -> None:
    rig = Rig()
    todo = TodoEvent(
        source="todos", entity_id="t", observed_at=at(1), payload=TodoPayload(text="x")
    )
    assert rig.apply(todo) is todo
    assert rig.emitted == []


def test_derived_records_are_never_older_than_what_the_registry_already_wrote() -> None:
    """A hook is stamped when it started, which can precede the poll that landed in between; the
    store would ignore such an observation, so the engine moves it up."""
    rig = Rig()
    rig.apply(registry_snapshot(10, SessionActivity(state="idle")))
    rig.apply(hook_record("UserPromptSubmit", 8))
    (derived,) = rig.activities()
    assert derived.observed_at == at(10)
    assert derived.payload.state == "working"
    rig.apply(hook_record("Stop", 12))
    assert rig.activities()[-1].observed_at == at(12)
    rig.apply(hook_record("UserPromptSubmit", 11.5))  # late: no effect at all
    assert len(rig.activities()) == 2


def test_seen_when_the_sessions_window_gains_focus_after_the_stop() -> None:
    rig = Rig({KEY: "66"})
    rig.apply(hook_record("Stop", 5))
    rig.apply(focus(4, "66"))  # focus before the Stop (replayed or late)
    assert [a.payload.state for a in rig.activities()] == ["turn_done"]
    rig.apply(focus(6, "99"))  # another window
    assert [a.payload.state for a in rig.activities()] == ["turn_done"]
    rig.apply(focus(7, "66"))
    states = [(a.payload.state, a.observed_at) for a in rig.activities()]
    assert states == [("turn_done", at(5)), ("seen", at(7))]
    state = held(rig)
    assert (state.state, state.since) == ("seen", at(7))
    rig.apply(focus(8, "66"))  # already seen
    assert len(rig.activities()) == 2


def test_focus_cannot_mark_a_session_without_a_window_or_without_a_finished_turn() -> None:
    rig = Rig()  # no window known for any session
    rig.apply(hook_record("Stop", 1))
    rig.apply(focus(2, "66"))
    rig.apply(focus(3, None))
    assert [a.payload.state for a in rig.activities()] == ["turn_done"]

    rig.windows[KEY] = "66"
    rig.apply(hook_record("UserPromptSubmit", 4))
    rig.apply(focus(5, "66"))
    assert rig.activities()[-1].payload.state == "working"


def test_seen_keeps_the_rest_of_the_activity_and_survives_the_registry() -> None:
    rig = Rig({KEY: "66"})
    rig.apply(registry_snapshot(1, SessionActivity(state="busy", title="Fix", last_reply="done.")))
    rig.apply(hook_record("Stop", 2))
    rig.apply(focus(3, "66"))
    seen = rig.activities()[-1].payload
    assert (seen.state, seen.title, seen.last_reply) == ("seen", "Fix", "done.")
    (item,) = payloads_of(rig.apply(registry_snapshot(4, SessionActivity(state="idle"))))
    assert item.state == "seen"


def test_two_sessions_in_one_window_are_both_marked_seen_in_key_order() -> None:
    rig = Rig({"claude:a": "66", "claude:b": "66", "claude:c": "70"})
    for name in ("b", "a", "c"):
        rig.apply(hook_record("Stop", 1, key=f"claude:{name}"))
    rig.apply(focus(2, "66"))
    assert [(a.entity_id, a.payload.state) for a in rig.activities()[3:]] == [
        ("claude:a", "seen"),
        ("claude:b", "seen"),
    ]


def test_seed_resumes_the_hook_only_states_after_a_restart() -> None:
    rig = Rig({KEY: "66"})
    rig.engine.seed(
        [
            (KEY, "claude", "turn_done", at(5), None),
            ("claude:w", "claude", "working", at(5), None),  # the registry reports this one again
            ("claude:p", "claude", "waiting_permission", None, "approve Bash"),
        ]
    )
    assert rig.engine.keys() == {KEY, "claude:p"}
    seeded = held(rig, "claude:p")
    assert seeded.hooked
    assert seeded.waiting_reason == "approve Bash"
    rig.engine.seed([(KEY, "claude", "error", at(1), None)])  # known sessions are not overwritten
    current = held(rig)
    assert current.state == "turn_done"

    # the registry's poll cannot undo a seeded hook state
    (item,) = payloads_of(rig.apply(registry_snapshot(6, SessionActivity(state="idle"))))
    assert item.state == "turn_done"
    rig.apply(focus(7, "66"))
    assert rig.activities()[-1].payload.state == "seen"
    assert rig.activities()[-1].observed_at == at(7)


def test_forget_and_the_memory_bound() -> None:
    rig = Rig()
    rig.apply(hook_record("Stop", 1))
    rig.engine.forget(KEY)
    rig.engine.forget("claude:never-seen")
    assert rig.engine.keys() == set()
    assert rig.engine.state_of(KEY) is None

    small = SessionStateEngine(lambda record: None, lambda key: None, max_sessions=2)
    for name in ("a", "b", "c"):
        small.apply(hook_record("Stop", 1, key=f"claude:{name}"))
    assert small.keys() == {"claude:b", "claude:c"}  # the oldest was forgotten


# --------------------------------------------------------------------------------------------
# A11: the /clear predecessor link
# --------------------------------------------------------------------------------------------


def test_a_clear_rotation_links_the_predecessor_by_pid() -> None:
    rig = Rig()
    rig.apply(hook_record("Stop", 1, key="claude:old", pid=900))
    rig.apply(hook_record("SessionEnd", 2, key="claude:old", pid=900, reason="clear"))
    rig.apply(hook_record("SessionStart", 3, key="claude:new", pid=900))
    assert rig.engine.predecessor("claude:new") == "claude:old"
    assert rig.engine.predecessor("claude:old") is None
    new = [e for e in rig.announced() if e.entity_id == "claude:new"]
    assert [e.payload.predecessor_key for e in new] == ["claude:old"]
    # the link is made once: a third session in the same process is not linked to the first
    rig.apply(hook_record("UserPromptSubmit", 4, key="claude:third", pid=900))
    assert rig.engine.predecessor("claude:third") is None


@pytest.mark.parametrize("reason", ["prompt_input_exit", "logout", "other", None])
def test_a_session_that_ended_for_another_reason_is_not_a_predecessor(reason: str | None) -> None:
    rig = Rig()
    rig.apply(hook_record("SessionEnd", 1, key="claude:old", pid=900, reason=reason))
    rig.apply(hook_record("UserPromptSubmit", 2, key="claude:new", pid=900))
    assert rig.engine.predecessor("claude:new") is None


def test_a_rotation_in_another_process_is_not_linked() -> None:
    rig = Rig()
    rig.apply(hook_record("SessionEnd", 1, key="claude:old", pid=900, reason="clear"))
    rig.apply(hook_record("UserPromptSubmit", 2, key="claude:new", pid=901))
    rig.apply(hook_record("UserPromptSubmit", 3, key="claude:nopid"))
    assert rig.engine.predecessor("claude:new") is None
    assert rig.engine.predecessor("claude:nopid") is None


def test_the_registry_continuity_record_gives_the_link_too() -> None:
    rig = Rig()
    payload = SessionPayload(
        kind="claude", native_id="new", process_key="900-17", predecessor_key="claude:old"
    )
    rig.apply(
        SessionSnapshot(
            source="claude",
            entity_id="all",
            observed_at=at(1),
            complete=True,
            items=(SnapshotItem(entity_id="claude:new", payload=payload),),
        )
    )
    assert rig.engine.predecessor("claude:new") == "claude:old"
    rig.apply(
        SessionEvent(
            source="claude",
            entity_id="claude:newer",
            observed_at=at(2),
            payload=SessionPayload(kind="claude", native_id="newer", predecessor_key="claude:new"),
        )
    )
    assert rig.engine.predecessor("claude:newer") == "claude:new"
    rig.engine.forget("claude:new")
    assert rig.engine.predecessor("claude:new") is None


def test_the_cleared_pid_table_is_bounded() -> None:
    rig = Rig()
    for pid in range(sessions.MAX_PIDS + 10):
        rig.apply(hook_record("SessionEnd", 1, key=f"claude:s{pid}", pid=pid, reason="clear"))
    rig.apply(hook_record("UserPromptSubmit", 2, key="claude:early", pid=0))
    rig.apply(hook_record("UserPromptSubmit", 2, key="claude:late", pid=sessions.MAX_PIDS + 9))
    assert rig.engine.predecessor("claude:early") is None  # evicted long ago
    assert rig.engine.predecessor("claude:late") == f"claude:s{sessions.MAX_PIDS + 9}"


# --------------------------------------------------------------------------------------------
# The recorded sequences, through the real normalizer
# --------------------------------------------------------------------------------------------


def load(name: str) -> dict[str, Any]:
    document = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def records_of(document: dict[str, Any]) -> list[HookEvent]:
    """The normalized hook events of a fixture: each payload goes through an envelope as the real
    ``fylgja hook`` builds it and through :func:`normalize`."""
    result = []
    for entry in document["events"]:
        payload = entry["payload"]
        environ = {
            "CLAUDE_CODE_SESSION_ID": payload["session_id"],
            "CLAUDE_PID": str(document["pid"]),
        }
        envelope = make_envelope(
            payload["hook_event_name"],
            payload,
            environ=environ,
            received_at_ns=entry["received_at_ns"],
            envelope_id=str(uuid.uuid4()),
        )
        result.append(normalize(parse_envelope(json.dumps(envelope))))
    return result


def test_the_fixture_set_covers_every_ratatosk_scenario() -> None:
    assert {
        "probe-session",
        "interrupt-session",
        "stopfailure-session",
        "clear-model-session",
    } <= set(SCENARIOS)
    ratatosk = Path(__file__).resolve().parent.parent / "fixtures" / "ratatosk"
    for directory in sorted(ratatosk.iterdir()):
        if (directory / "hook-events.ndjson").is_file():
            assert directory.name in SCENARIOS, f"no hook sequence for {directory.name}"


def test_the_fixture_sequences_follow_the_summaries_they_come_from() -> None:
    root = Path(__file__).resolve().parent.parent.parent
    for name in SCENARIOS:
        document = load(name)
        origin = document["origin"]
        if not origin.startswith("tests/fixtures/ratatosk/"):
            continue
        summaries = [
            json.loads(line)
            for line in (root / origin).read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert [e["payload"]["hook_event_name"] for e in document["events"]] == [
            s["event"] for s in summaries
        ], name


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_sequence_reaches_the_recorded_state_after_every_event(name: str) -> None:
    document = load(name)
    rig = Rig()
    for entry, record in zip(document["events"], records_of(document), strict=True):
        rig.apply(record)
        key = record.entity_id
        state = rig.engine.state_of(key)
        assert state is not None
        want = entry["expect"]
        got = {
            "state": state.state,
            "tool": state.current_tool,
            "target": state.current_target,
            "reason": state.waiting_reason,
        }
        assert got == {k: want[k] for k in got}, (name, record.payload.event)
        if "predecessor" in want:
            assert rig.engine.predecessor(key) == want["predecessor"], name
        # what is stored for the session agrees with what the engine holds
        activities = [a for a in rig.activities() if a.entity_id == key]
        if state.state is not None:
            assert activities[-1].payload.state == state.state
    assert rig.engine.keys() == {r.entity_id for r in records_of(document)}


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_sequence_ends_the_same_way_in_any_arrival_order(name: str) -> None:
    document = load(name)
    records = records_of(document)
    by_session: dict[str, list[HookEvent]] = {}
    for record in records:
        by_session.setdefault(record.entity_id, []).append(record)
    in_order = Rig()
    for record in records:
        in_order.apply(record)
    for seed in range(5):
        shuffled = Rig()
        order = list(records)
        random.Random(seed).shuffle(order)
        for record in order:
            shuffled.apply(record)
        for key in by_session:
            a, b = held(in_order, key), held(shuffled, key)
            assert (a.state, a.current_tool, a.order) == (b.state, b.current_tool, b.order), (
                name,
                key,
                seed,
            )


ALLOWED = {
    "session_id",
    "event",
    "tool_name",
    "notification_type",
    "reason",
    "cwd",
    "permission_mode",
    "edited_paths",
    "prompt",
    "pid",
    "ids",
    "envelope_id",
}


def test_a_synthesized_payload_is_minimized_before_it_reaches_the_state_machine() -> None:
    for record in records_of(load("synthesized-hooks-docs")):
        kept = {k for k, v in record.payload.model_dump().items() if v is not None}
        assert kept <= ALLOWED, kept
        dumped = record.payload.model_dump_json()
        for dropped in ("transcript", "old_string", "tool_response", "last_assistant_message"):
            assert dropped not in dumped


def test_the_clear_scenario_links_the_new_session_to_the_old_one() -> None:
    document = load("clear-model-session")
    rig = Rig()
    for record in records_of(document):
        rig.apply(record)
    old, new = (e["payload"]["session_id"] for e in (document["events"][0], document["events"][-1]))
    assert rig.engine.predecessor(f"claude:{new}") == f"claude:{old}"
    announced = {e.entity_id: e.payload for e in rig.announced()}
    assert announced[f"claude:{new}"].predecessor_key == f"claude:{old}"
    assert announced[f"claude:{new}"].pid == document["pid"]


def test_the_probe_scenario_turns_seen_when_its_window_is_focused_afterwards() -> None:
    document = load("probe-session")
    records = records_of(document)
    key = records[0].entity_id
    rig = Rig({key: "66"})
    stops = [r for r in records if r.payload.event == "Stop"]
    first_stop = stops[0].observed_at
    rig.apply(focus(-1, "66"))  # long before anything
    for record in records[:3]:
        rig.apply(record)
    rig.apply(
        FocusEvent(
            source="aerospace",
            entity_id="focus",
            observed_at=first_stop - timedelta(milliseconds=1),
            payload=FocusPayload(window_id="66"),
        )
    )
    state = held(rig, key)
    assert state.state == "turn_done"
    rig.apply(
        FocusEvent(
            source="aerospace",
            entity_id="focus",
            observed_at=first_stop + timedelta(milliseconds=500),
            payload=FocusPayload(window_id="66"),
        )
    )
    state = held(rig, key)
    assert state.state == "seen"
    for record in records[3:]:
        rig.apply(record)
    state = held(rig, key)
    assert state.state == "ended"


# --------------------------------------------------------------------------------------------
# The store: what the writer makes of the derived records
# --------------------------------------------------------------------------------------------


class _Clock:
    def now(self) -> datetime:
        return T0

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        await asyncio.Event().wait()


def _rows(path: Path, sql: str) -> list[sqlite3.Row]:
    connection = db.connect_reader(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


async def test_the_writer_stores_the_state_since_the_reason_and_the_one_liner() -> None:
    document = load("probe-session")
    writer = Writer(paths.database_file(), clock=_Clock())
    await writer.start()
    try:
        engine = SessionStateEngine(writer.submit, {}.get)
        records = records_of(document)
        for record in records:
            engine.apply(record)
            if record.payload.event == "Notification":
                await writer.flush()
                (row,) = _rows(
                    paths.database_file(),
                    "SELECT state, state_since, waiting_reason, pid, source FROM sessions",
                )
                assert (row["state"], row["waiting_reason"], row["pid"]) == (
                    "waiting_permission",
                    "approve Write",
                    document["pid"],
                )
                assert row["source"] == "claude"
        await writer.flush()
        history = _rows(
            paths.database_file(),
            "SELECT state, one_liner, last_tool, current_target, valid_to FROM session_snapshots"
            " ORDER BY id",
        )
        session = _rows(paths.database_file(), "SELECT state, state_since FROM sessions")
    finally:
        await writer.stop()
    states = [row["state"] for row in history]
    assert states == [
        "working",
        "turn_done",
        "working",
        "running_tool",
        "working",
        "turn_done",
        "working",
        "running_tool",
        "waiting_permission",
        "working",
        "turn_done",
        "ended",
    ]
    lines = {row["state"]: row["one_liner"] for row in history}
    assert lines["waiting_permission"] == "Waiting for permission: Write notes.txt"
    waiting = next(row for row in history if row["state"] == "waiting_permission")
    assert (waiting["last_tool"], waiting["current_target"]) == ("Write", "notes.txt")
    assert [row["valid_to"] is None for row in history] == [False] * 11 + [True]
    assert session[0]["state"] == "ended"
    assert session[0]["state_since"] == "2026-10-05T12:00:13.000000Z"
