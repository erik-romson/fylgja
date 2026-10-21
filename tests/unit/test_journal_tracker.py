"""The journal tracker and ``due`` (J1–J5, D2, D7, D10): live-view updates and focus spans become
dirty threads and boundaries, and ``due`` says which thread gets an attempt and when to look again.

The scenarios use ``LiveUpdate`` and ``Change`` values built directly, ``Opened`` and ``Closed``
spans, monotonic seconds and the default settings (120 s quiet, 600 s interval, 300 s focus).
Session keys such as ``claude:a`` and ``norn:r`` are only test data: the tracker finds children
by ``parent_key`` and turn ends by state.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from fylgja.config import JournalConfig
from fylgja.core.journal import (
    Due,
    DueThread,
    Source,
    ThreadKey,
    ThreadState,
    Trigger,
    due,
    highest_trigger,
    thread_id,
)
from fylgja.core.journal_service import JournalTracker, Resumed
from fylgja.core.live_view import Change, Collection, LiveUpdate, LiveView
from fylgja.core.spans import Closed, Dropped, Opened
from fylgja.store.rows import (
    ActivityRow,
    JournalThreadRow,
    ProjectRow,
    SessionRow,
    SpanRow,
    StoreRow,
    TodoRow,
)

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
REPO = "/w/repo"
OTHER = "/w/other"
MAIN = ThreadKey(REPO, "main")
FEATURE = ThreadKey(REPO, "feature")
A = "claude:a"
B = "claude:b"
SETTINGS = JournalConfig(enabled=True)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class Clock:
    """Sync virtual time: ``t`` monotonic seconds, wall time ``T0 + t``."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> datetime:
        return at(self.t)

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - unused
        raise AssertionError


# --------------------------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------------------------


def project(
    path: str = REPO,
    *,
    branch: str | None = "main",
    repo_root: str | None = None,
    last_commit_at: datetime | None = None,
    checkout: bool = True,
) -> ProjectRow:
    return ProjectRow(
        path=path,
        repo_root=(repo_root or path) if checkout else None,
        remote_url=None,
        repo_name=None,
        group_name=None,
        branch=branch,
        dirty=None,
        ahead=None,
        behind=None,
        pr_number=None,
        pr_state=None,
        review_decision=None,
        build_state=None,
        build=None,
        build_observed_at=None,
        build_error_kind=None,
        last_commit_at=last_commit_at,
        updated_at=T0,
    )


def session(
    key: str,
    *,
    state: str | None = "working",
    path: str | None = REPO,
    parent: str | None = None,
    title: str | None = None,
    since: datetime | None = None,
) -> SessionRow:
    kind, _, native_id = key.partition(":")
    return SessionRow(
        session_key=key,
        kind=kind,
        native_id=native_id,
        source=kind,
        pid=None,
        process_key=None,
        cwd=path,
        project_path=path,
        host_kind=None,
        host_ref=None,
        window_id=None,
        workspace=None,
        name=None,
        title=title,
        model=None,
        branch=None,
        state=state,
        state_since=since or T0,
        waiting_reason=None,
        parent_key=parent,
        predecessor_key=None,
        remote_url=None,
        first_seen=T0,
        ended_at=None,
        last_seen=T0,
    )


def activity(key: str, *, prompt: str | None = None, valid_from: float = 0.0) -> ActivityRow:
    kind = key.partition(":")[0]
    return ActivityRow(
        session_key=key,
        source=kind,
        valid_from=at(valid_from),
        state=None,
        waiting_reason=None,
        last_tool=None,
        current_target=None,
        open_task=None,
        stage=None,
        last_prompt=prompt,
        last_reply=None,
        one_liner=None,
        context_pct=None,
        cost_usd=None,
        tokens_input=None,
        tokens_output=None,
        tokens_cache_read=None,
        tokens_cache_write=None,
    )


def todo(todo_id: str, *, path: str = REPO, scope_kind: str = "project") -> TodoRow:
    return TodoRow(
        id=todo_id,
        scope_kind=scope_kind,
        scope_ref=path,
        text="a todo",
        status="open",
        created_at=T0,
        done_at=None,
        source="fylgja",
        source_ref=None,
        link=None,
        updated_at=T0,
    )


def span(path: str | None, start: float, seconds: float | None = None) -> SpanRow:
    return SpanRow(
        window_id="w1" if path is not None else "browser",
        workspace="1",
        app_bundle_id=None,
        title=None,
        project_path=path,
        session_key=None,
        started_at=at(start),
        ended_at=None if seconds is None else at(start + seconds),
        end_reason=None if seconds is None else "focus",
    )


def session_source(key: str) -> Source:
    return Source("session", key, key.partition(":")[0])


def thread_source(key: ThreadKey) -> Source:
    return Source("thread", thread_id(key), f"repo · {key.branch}")


_COLLECTIONS: dict[type[StoreRow], Collection] = {
    ProjectRow: Collection.PROJECTS,
    SessionRow: Collection.SESSIONS,
    ActivityRow: Collection.ACTIVITY,
    TodoRow: Collection.TODOS,
}


def _key(row: StoreRow) -> str:
    if isinstance(row, ProjectRow):
        return row.path
    if isinstance(row, SessionRow | ActivityRow):
        return row.session_key
    assert isinstance(row, TodoRow)
    return row.id


# --------------------------------------------------------------------------------------------
# The world: a live view, a clock and a seeded tracker
# --------------------------------------------------------------------------------------------


class World:
    """A seeded tracker over a live view. By default ``/w/repo`` on ``main`` with session
    ``claude:a`` working in it."""

    def __init__(self, *rows: StoreRow, default: bool = True) -> None:
        self.clock = Clock()
        self.view = LiveView()
        self.version = 0
        self.resumed: list[Resumed] = []
        """What every ``observe`` returned, in order."""
        seeded: list[StoreRow] = [project(), session(A)] if default else []
        seeded += rows
        self.view.apply(Change(_COLLECTIONS[type(row)], _key(row), row) for row in seeded)
        self.tracker = JournalTracker(SETTINGS, self.clock)
        self.tracker.seed(self.view, ())

    @property
    def states(self) -> dict[ThreadKey, ThreadState]:
        return dict(self.tracker.states)

    def state_of(self, key: ThreadKey = MAIN) -> ThreadState:
        return self.tracker.states[key]

    def change(self, t: float, *changes: Change, linked: Iterable[str] = ()) -> None:
        self.clock.t = t
        self.view.apply(changes)
        self.version += 1
        update = LiveUpdate(self.version, changes)
        self.resumed += self.tracker.observe(update, self.view, linked=set(linked))

    def put(self, t: float, row: StoreRow, *, linked: Iterable[str] = ()) -> None:
        self.change(t, Change(_COLLECTIONS[type(row)], _key(row), row), linked=linked)

    def remove(self, t: float, collection: Collection, key: str) -> None:
        self.change(t, Change(collection, key, None))

    def state(self, t: float, key: str, state: str, **fields: Any) -> None:
        """Session ``key`` changes to ``state`` at ``t``."""
        self.put(t, replace(self.view.sessions[key], state=state, state_since=at(t), **fields))

    def prompt(self, t: float, key: str, text: str) -> None:
        """A new prompt of session ``key`` (an ``ACTIVITY`` change)."""
        self.put(t, activity(key, prompt=text, valid_from=t))

    def turn(self, t_prompt: float, t_stop: float, key: str = A, text: str = "more") -> None:
        """A prompt (the session works) and its Stop."""
        self.state(t_prompt, key, "working")
        self.prompt(t_prompt, key, text)
        self.state(t_stop, key, "turn_done")

    def branch(self, t: float, branch: str | None, **fields: Any) -> None:
        self.put(t, replace(self.view.projects[REPO], branch=branch, **fields))

    def span(self, action: Opened | Closed | Dropped) -> None:
        self.tracker.on_span(action)

    def visit(self, path: str | None, start: float, seconds: float) -> None:
        """A focus span on ``path`` that opens at ``start`` and closes ``seconds`` later."""
        self.span(Opened(span(path, start)))
        self.span(Closed(span(path, start, seconds)))

    def due(self, t: float) -> Due:
        return due(t, self.tracker.states, SETTINGS)

    def attempt(
        self, reserve: float, begin: float | None = None, trigger: Trigger = "turn_done"
    ) -> tuple[ThreadState, Trigger]:
        """Reserve the main thread at ``reserve`` and capture at ``begin`` (the same instant by
        default)."""
        self.clock.t = reserve
        self.tracker.reserve(MAIN)
        self.clock.t = reserve if begin is None else begin
        return self.tracker.begin(MAIN, trigger)

    def entry(self, t: float) -> None:
        """A successful attempt of the main thread at ``t``."""
        self.attempt(t)
        self.tracker.succeeded(MAIN)


def due_now(world: World, t: float) -> list[tuple[ThreadKey, Trigger]]:
    return [(item.key, item.trigger) for item in world.due(t).threads]


# --------------------------------------------------------------------------------------------
# The plan's scenarios
# --------------------------------------------------------------------------------------------


def test_01_a_turn_is_due_after_the_quiet() -> None:
    world = World()
    world.state(0, A, "turn_done")
    early = world.due(119)
    assert early.threads == ()
    assert early.next_check == 120
    assert world.due(120) == Due((DueThread(MAIN, "turn_done", session_source(A)),), None)


def test_02_input_restarts_the_quiet() -> None:
    world = World()
    world.state(0, A, "turn_done")
    world.state(60, A, "working")
    world.prompt(60, A, "a new prompt")
    world.state(90, A, "turn_done")
    assert world.due(120).threads == ()
    assert world.due(120).next_check == 210
    assert due_now(world, 210) == [(MAIN, "turn_done")]


def test_03_min_interval_between_soft_entries() -> None:
    world = World()
    world.state(0, A, "turn_done")
    world.entry(120)
    world.turn(180, 180)
    assert world.due(300).threads == ()
    assert world.due(300).next_check == 720
    assert due_now(world, 720) == [(MAIN, "turn_done")]


def test_04_a_session_end_is_due_at_once() -> None:
    world = World()
    world.state(0, A, "turn_done")
    world.state(5, A, "ended")
    assert world.due(5).threads == (DueThread(MAIN, "session_end", session_source(A)),)


def test_05_a_session_end_on_a_clean_thread_is_dropped() -> None:
    world = World()
    world.state(0, A, "turn_done")
    world.entry(120)
    world.state(125, A, "ended")
    assert world.state_of().pending == {}
    assert world.due(125).threads == ()
    assert world.due(10_000).threads == ()


def test_06_one_session_end_per_session_key() -> None:
    world = World(session(B))
    world.state(0, A, "turn_done")
    world.state(5, A, "ended")
    assert list(world.state_of().pending) == ["turn_done", "session_end"]
    assert due_now(world, 5) == [(MAIN, "session_end")]
    world.entry(6)
    world.prompt(7, B, "other work")  # dirty again
    world.remove(8, Collection.SESSIONS, A)
    assert world.state_of().pending == {}
    assert world.due(8).threads == ()


def test_06_a_row_leaving_the_view_is_a_session_end() -> None:
    world = World()
    world.prompt(0, A, "work")
    world.remove(5, Collection.SESSIONS, A)
    world.remove(5, Collection.SESSIONS, "claude:unknown")
    assert world.due(5).threads == (DueThread(MAIN, "session_end", session_source(A)),)


def test_07_a_run_with_children_gives_one_run_finished() -> None:
    run = "norn:r"
    world = World(
        session(run, state="running"),
        session("claude:c1", parent=run),
        session("claude:c2", parent=run),
    )
    world.state(0, "claude:c1", "turn_done")
    world.state(1, "claude:c2", "busy")
    world.state(2, "claude:c2", "idle")
    world.state(3, "claude:c1", "ended")
    world.remove(4, Collection.SESSIONS, "claude:c2")
    state = world.state_of()
    assert state.dirty
    assert not state.stop_seen
    assert state.pending == {}
    assert world.due(10_000) == Due((), None)
    world.state(5_000, run, "passed")
    assert world.due(5_000).threads == (DueThread(MAIN, "run_finished", session_source(run)),)


def test_07_a_stale_run_is_no_outcome() -> None:
    run = "norn:r"
    world = World(session(run, state="running"))
    world.state(10, run, "stale")
    assert world.state_of().dirty
    assert world.due(10_000).threads == ()


def test_07_one_run_finished_per_outcome_instant() -> None:
    run = "norn:r"
    world = World(session(run, state="running"))
    world.state(10, run, "passed")
    finished = world.view.sessions[run].state_since
    assert due_now(world, 10) == [(MAIN, "run_finished")]
    world.entry(10)
    world.put(20, replace(world.view.sessions[run], state="stale", state_since=at(20)))
    world.put(30, replace(world.view.sessions[run], state="passed", state_since=finished))
    assert world.state_of().dirty
    assert world.state_of().pending == {}
    world.state(40, run, "failed")
    assert due_now(world, 40) == [(MAIN, "run_finished")]


def test_08_a_hook_less_turn_end() -> None:
    world = World(session("codex:x", state="busy"))
    world.state(0, "codex:x", "idle")
    assert world.state_of().stop_seen
    assert due_now(world, 120) == [(MAIN, "turn_done")]

    working = World()
    working.state(0, A, "idle")
    assert working.state_of().stop_seen
    assert due_now(working, 120) == [(MAIN, "turn_done")]


def test_08_idle_to_idle_with_unchanged_activity_is_not_dirty() -> None:
    idle = session("codex:y", state="idle")
    world = World(idle, activity("codex:y", prompt="same"))
    world.change(
        10,
        Change(Collection.SESSIONS, "codex:y", replace(idle, last_seen=at(10))),
        Change(Collection.ACTIVITY, "codex:y", activity("codex:y", prompt="same", valid_from=10)),
    )
    assert world.states == {}


def test_a_run_that_goes_on_after_waiting_is_resumed_without_a_quiet() -> None:
    run = "norn:r"
    world = World(session(run, state="running"))
    world.entry(0)
    world.state(100, run, "waiting_input")
    assert world.state_of().pending == {}
    world.state(400, run, "running")
    assert world.resumed == [Resumed(run, at(100))]
    assert world.state_of().pending == {"resumed": session_source(run)}
    assert world.due(599) == Due((), 600)  # the interval since the entry at 0
    world.prompt(590, run, "the run keeps going")  # input does not hold it back
    assert world.due(600).threads == (DueThread(MAIN, "resumed", session_source(run)),)


def test_a_permission_answered_is_resumed_and_a_stop_after_it_too() -> None:
    world = World()
    world.state(0, A, "waiting_permission")
    world.state(10, A, "working")
    world.state(20, A, "turn_done")
    assert world.resumed == [Resumed(A, at(0))]
    assert set(world.state_of().pending) == {"resumed", "turn_done"}
    assert due_now(world, 20) == [(MAIN, "resumed")]


def test_a_child_that_goes_on_is_resumed_but_no_boundary() -> None:
    run = "norn:r"
    world = World(session(run, state="running"), session("norn:c", parent=run))
    world.state(10, "norn:c", "waiting_input")
    world.state(20, "norn:c", "running")
    assert world.resumed == [Resumed("norn:c", at(10))]
    assert world.state_of().dirty
    assert world.state_of().pending == {}


def test_waiting_that_ends_or_keeps_waiting_is_not_resumed() -> None:
    world = World(session("norn:r", state="waiting_input"), session("claude:b", state="waiting"))
    world.state(10, "norn:r", "ended")
    world.state(20, "claude:b", "waiting_permission")
    world.state(30, A, "idle")
    assert world.resumed == []
    assert "resumed" not in world.state_of().pending


def test_09_focus_alone_gives_nothing() -> None:
    world = World(project(OTHER))
    world.visit(REPO, 0, 1800)
    world.visit(REPO, 1800, 1800)
    world.span(Opened(span(OTHER, 3600)))
    assert world.states == {}
    assert world.due(3600) == Due((), None)


def test_10_leaving_after_a_long_visit_is_focus_left() -> None:
    world = World()
    world.prompt(0, A, "work")
    world.visit(REPO, 0, 200)
    world.visit(None, 200, 60)  # a browser peek does not end the visit
    world.visit(REPO, 260, 150)
    assert world.state_of().pending == {}
    world.span(Opened(span(OTHER, 410)))
    assert world.due(410).threads == (DueThread(MAIN, "focus_left", thread_source(MAIN)),)


def test_10_a_short_visit_is_no_boundary() -> None:
    world = World()
    world.prompt(0, A, "work")
    world.visit(REPO, 0, 60)
    world.span(Opened(span(OTHER, 60)))
    assert world.state_of().pending == {}
    assert world.due(60).threads == ()


def test_11_a_detached_head_has_no_thread() -> None:
    world = World(session(B))
    world.branch(10, None)
    world.prompt(20, A, "rebasing")
    world.state(25, A, "turn_done")
    world.state(30, A, "ended")
    assert world.states == {}
    world.branch(40, "main")
    assert world.states == {}
    world.prompt(50, B, "back on main")
    world.remove(60, Collection.SESSIONS, A)  # its end was taken while detached
    assert world.state_of().pending == {}
    assert world.due(10_000).threads == ()


def test_11_a_dirty_thread_stays_due_through_a_detach() -> None:
    world = World()
    world.state(0, A, "turn_done")
    world.branch(10, None)
    world.prompt(20, A, "rebasing")
    assert world.state_of().last_input == 0
    assert world.due(119).next_check == 120
    assert due_now(world, 120) == [(MAIN, "turn_done")]


def test_11_detached_then_another_branch_is_a_switch() -> None:
    world = World()
    world.prompt(0, A, "work")
    world.branch(10, None)
    world.branch(20, "feature")
    assert world.due(20).threads == (DueThread(MAIN, "branch_changed", thread_source(MAIN)),)
    assert world.state_of(FEATURE).floor == at(20)


def test_12_a_branch_switch() -> None:
    world = World()
    world.prompt(0, A, "work")
    world.branch(50, "feature")
    assert world.due(50).threads == (DueThread(MAIN, "branch_changed", thread_source(MAIN)),)
    world.prompt(60, A, "feature work")
    feature = world.state_of(FEATURE)
    assert feature.dirty
    assert feature.floor == at(50)
    assert feature.first_input == at(60)


def test_12_a_switch_on_a_clean_thread_is_no_boundary() -> None:
    world = World()
    world.branch(50, "feature")
    assert MAIN not in world.states
    assert not world.state_of(FEATURE).dirty
    assert world.due(10_000) == Due((), None)


def test_13_no_thread_without_a_checkout() -> None:
    world = World(
        project("/w/plain", checkout=False),
        project("/w/repo/sub", repo_root=REPO),
        session("claude:n", path="/w/none"),
        session("claude:p", path="/w/plain"),
        session("claude:s", path="/w/repo/sub"),
        session("claude:u", path=None),
    )
    for key in ("claude:n", "claude:p", "claude:s", "claude:u"):
        world.prompt(0, key, "work")
        world.state(1, key, "turn_done")
        world.state(2, key, "ended")
    assert world.states == {}


def test_14_a_failure_waits_the_interval() -> None:
    world = World()
    world.state(0, A, "turn_done")
    snapshot, _ = world.attempt(120)
    world.tracker.failed(MAIN, "timeout", snapshot)
    state = world.state_of()
    assert state.dirty
    assert state.stop_seen
    assert (state.failed_kind, state.failed_at) == ("timeout", 120)
    assert world.due(719).threads == ()
    assert world.due(719).next_check == 720
    assert due_now(world, 720) == [(MAIN, "turn_done")]


def test_14_bad_output_waits_for_new_input() -> None:
    world = World()
    world.state(0, A, "turn_done")
    snapshot, _ = world.attempt(120)
    world.tracker.failed(MAIN, "bad_output", snapshot)
    assert world.due(720) == Due((), None)
    world.prompt(800, A, "again")
    assert world.due(919).next_check == 920
    assert due_now(world, 920) == [(MAIN, "turn_done")]


def test_15_project_todos_are_input() -> None:
    world = World(todo("t0"))
    world.put(10, todo("t1"))
    assert world.state_of().last_input == 10
    world.remove(20, Collection.TODOS, "t0")
    assert world.state_of().last_input == 20


def test_15_linked_and_other_todos_are_not_input() -> None:
    world = World(todo("t0"), todo("t3"))
    world.put(10, todo("t1"), linked={"t1"})
    world.change(20, Change(Collection.TODOS, "t0", None), linked={"t0"})
    world.put(30, todo("t2", scope_kind="workspace"))
    world.put(40, todo("t3", scope_kind="workspace"))  # moved out of the checkout
    world.remove(50, Collection.TODOS, "t3")
    world.remove(60, Collection.TODOS, "never-seen")
    assert world.states == {}


def test_16_seen_does_not_dirty_or_restart_the_quiet() -> None:
    world = World()
    world.state(0, A, "turn_done")
    world.state(60, A, "seen")
    assert world.state_of().last_input == 0
    assert due_now(world, 120) == [(MAIN, "turn_done")]

    clean = World(session(B, state="turn_done"))
    clean.state(10, B, "seen")
    assert clean.states == {}


def test_17_branch_lag_logs_the_work_under_the_old_branch() -> None:
    """Assumption 8: ``projects.branch`` can lag a switch by up to 5 minutes."""
    world = World()
    world.prompt(0, A, "work done after the switch")
    world.branch(300, "feature")
    assert world.due(300).threads == (DueThread(MAIN, "branch_changed", thread_source(MAIN)),)
    assert not world.state_of(FEATURE).dirty


def test_18_input_while_waiting_for_a_slot_is_acknowledged_at_capture() -> None:
    world = World()
    world.state(0, A, "turn_done")
    assert due_now(world, 120) == [(MAIN, "turn_done")]
    world.clock.t = 120
    world.tracker.reserve(MAIN)
    world.prompt(125, A, "while waiting")
    world.clock.t = 130
    world.tracker.begin(MAIN, "turn_done")
    world.tracker.succeeded(MAIN)
    assert not world.state_of().dirty
    assert world.due(730) == Due((), None)
    assert world.due(10_000).threads == ()


def test_18_input_after_capture_keeps_the_thread_dirty() -> None:
    world = World()
    world.state(0, A, "turn_done")
    world.attempt(120, 130)
    world.prompt(140, A, "during the call")
    world.tracker.succeeded(MAIN)
    state = world.state_of()
    assert state.dirty
    assert state.last_attempt == 130


def test_18_a_hard_boundary_while_waiting_raises_the_trigger() -> None:
    world = World()
    world.state(0, A, "turn_done")
    world.clock.t = 120
    world.tracker.reserve(MAIN)
    world.state(125, A, "ended")
    world.clock.t = 130
    snapshot, trigger = world.tracker.begin(MAIN, "turn_done")
    assert trigger == "session_end"
    assert snapshot.pending["session_end"] == session_source(A)
    state = world.state_of()
    assert (state.dirty, state.stop_seen, state.pending) == (False, False, {})

    manual = World()
    manual.state(0, A, "turn_done")
    manual.tracker.reserve(MAIN)
    manual.state(5, A, "ended")
    assert manual.tracker.begin(MAIN, "manual")[1] == "manual"


def test_18_a_reserved_thread_is_not_due() -> None:
    world = World()
    world.state(0, A, "turn_done")
    world.clock.t = 120
    world.tracker.reserve(MAIN)
    assert world.due(120) == Due((), None)
    world.state(130, A, "ended")
    assert world.due(130) == Due((), None)


def test_18_cancelled_before_capture_keeps_the_thread_dirty() -> None:
    world = World()
    world.state(0, A, "turn_done")
    world.clock.t = 120
    world.tracker.reserve(MAIN)
    world.tracker.cancelled(MAIN)
    state = world.state_of()
    assert not state.running
    assert state.dirty
    assert due_now(world, 120) == [(MAIN, "turn_done")]


# --------------------------------------------------------------------------------------------
# Details of the rules the scenarios do not reach
# --------------------------------------------------------------------------------------------


def test_the_order_of_triggers() -> None:
    assert highest_trigger("turn_done", ["focus_left", "session_end"]) == "session_end"
    assert highest_trigger("focus_left", ["turn_done"]) == "focus_left"
    assert highest_trigger("turn_done", ["resumed"]) == "resumed"
    assert highest_trigger("resumed", ["focus_left"]) == "focus_left"
    assert highest_trigger("session_end", ["run_finished", "branch_changed"]) == "run_finished"
    assert highest_trigger("manual", ["run_finished"]) == "manual"
    assert highest_trigger("turn_done", []) == "turn_done"


def test_due_picks_the_highest_pending_boundary_and_the_earliest_deadline() -> None:
    session_end = Source("session", "s", "s")
    run = Source("session", "r", "r")
    states = {
        ThreadKey("/w/b", "main"): ThreadState(
            dirty=True, pending={"session_end": session_end, "run_finished": run}
        ),
        ThreadKey("/w/a", "main"): ThreadState(
            dirty=True, last_input=50.0, pending={"turn_done": session_end}
        ),
        ThreadKey("/w/c", "main"): ThreadState(
            dirty=True, last_input=10.0, pending={"turn_done": session_end}
        ),
        ThreadKey("/w/d", "main"): ThreadState(dirty=True, last_input=0.0),
        ThreadKey("/w/e", "main"): ThreadState(pending={"turn_done": session_end}),
        ThreadKey("/w/f", "main"): ThreadState(
            dirty=True, last_input=90.0, pending={"turn_done": session_end}
        ),
    }
    found = due(100, states, SETTINGS)
    assert found.threads == (DueThread(ThreadKey("/w/b", "main"), "run_finished", run),)
    assert found.next_check == 130


def test_a_stop_without_a_recorded_input_is_due_at_once() -> None:
    """The service's restart bootstrap (J6) may set a Stop; ``due`` needs no input time."""
    source = Source("session", "s", "s")
    states = {MAIN: ThreadState(dirty=True, pending={"turn_done": source})}
    assert due(0, states, SETTINGS) == Due((DueThread(MAIN, "turn_done", source),), None)


def test_a_failure_holds_back_a_hard_boundary_too() -> None:
    source = Source("session", "s", "s")
    states = {
        MAIN: ThreadState(dirty=True, pending={"session_end": source}, failed_kind="timeout"),
        FEATURE: ThreadState(
            dirty=True, pending={"session_end": source}, failed_kind="timeout", failed_at=0.0
        ),
    }
    found = due(100, states, SETTINGS)
    assert found.threads == (DueThread(MAIN, "session_end", source),)
    assert found.next_check == 600


def test_a_failure_merges_the_snapshot_with_later_input() -> None:
    world = World(session(B))
    world.state(0, A, "turn_done")
    snapshot, _ = world.attempt(120)
    world.state(130, B, "turn_done")
    world.tracker.failed(MAIN, "bad_output", snapshot)
    state = world.state_of()
    assert state.last_input == 130
    assert state.pending == {"turn_done": session_source(B)}
    assert state.input_since_failure
    assert state.first_input == at(0)
    assert state.failed_at == 130  # when the failure was told, not the capture
    assert world.due(729).next_check == 730
    assert due_now(world, 730) == [(MAIN, "turn_done")]
    world.entry(730)
    state = world.state_of()
    assert (state.failed_kind, state.failed_at, state.input_since_failure) == (None, None, False)


def test_input_after_a_failure_is_noted() -> None:
    world = World()
    world.state(0, A, "turn_done")
    snapshot, _ = world.attempt(120)
    world.tracker.failed(MAIN, "bad_output", snapshot)
    assert not world.state_of().input_since_failure
    world.prompt(130, A, "new")
    assert world.state_of().input_since_failure
    assert world.state_of().last_input == 130


def test_a_later_commit_is_input_and_a_switch_starts_clean() -> None:
    world = World(project(last_commit_at=at(-100)), default=False)
    world.branch(10, "main", last_commit_at=at(5))
    assert world.state_of().last_input == 10
    world.branch(20, "main", last_commit_at=at(5))
    assert world.state_of().last_input == 10
    world.branch(30, "feature", last_commit_at=at(-50))
    world.branch(40, "feature", last_commit_at=at(25))
    assert world.state_of(FEATURE).last_input == 40

    switched = World()
    switched.branch(10, "feature", last_commit_at=at(5))
    assert not switched.state_of(FEATURE).dirty


def test_a_branch_switch_of_a_subfolder_row_is_no_boundary() -> None:
    sub = "/w/repo/sub"
    world = World(project(sub, repo_root=REPO))
    world.prompt(0, A, "work")
    world.put(10, project(sub, repo_root=REPO, branch="feature"))
    assert world.states.keys() == {MAIN}
    assert world.state_of().pending == {}


def test_focus_and_windows_are_not_input() -> None:
    world = World()
    world.change(
        10,
        Change(Collection.FOCUS, "current", None),
        Change(Collection.WINDOWS, "w1", None),
    )
    assert world.states == {}


def test_a_project_seen_first_after_the_seed_is_no_input() -> None:
    world = World(default=False)
    world.put(10, project(last_commit_at=at(5)))
    world.put(20, session(A))
    assert world.state_of().last_input == 20
    world.remove(30, Collection.PROJECTS, REPO)
    world.prompt(40, A, "after the row left")
    assert world.state_of().last_input == 20


def test_one_update_takes_its_changes_in_order() -> None:
    """A session and its checkout in one update: the checkout's row is read from the view."""
    world = World(default=False)
    world.change(
        10,
        Change(Collection.SESSIONS, A, session(A)),
        Change(Collection.PROJECTS, REPO, project()),
    )
    assert world.state_of().last_input == 10


def test_activity_of_a_session_the_memo_does_not_know() -> None:
    world = World()
    world.view.apply([Change(Collection.SESSIONS, B, session(B))])  # not observed
    world.prompt(10, B, "work")
    assert world.state_of().last_input == 10
    world.remove(20, Collection.ACTIVITY, B)
    world.prompt(30, "claude:ghost", "no session row")
    assert world.state_of().last_input == 10


def test_a_session_first_seen_as_ended_or_done() -> None:
    world = World(session(B))
    world.prompt(0, B, "dirty")
    world.put(5, session("claude:e", state="ended"))
    world.put(6, session("claude:t", state="turn_done"))
    assert set(world.state_of().pending) == {"session_end", "turn_done"}


def test_a_closed_or_dropped_span_without_a_visit() -> None:
    world = World()
    world.prompt(0, A, "work")
    world.span(Closed(span(REPO, 0, 400)))  # it opened before the tracker saw it
    world.span(Dropped(span(REPO, 400)))
    world.span(Opened(span(OTHER, 400)))
    assert world.state_of().pending == {}


def test_leaving_a_folder_that_has_no_thread() -> None:
    world = World(project("/w/plain", checkout=False))
    world.prompt(0, A, "work")
    world.visit("/w/plain", 0, 400)
    world.visit("/w/nowhere", 400, 400)
    world.span(Opened(span(REPO, 800)))
    assert world.state_of().pending == {}


def test_seed_replaces_the_memo_and_dirties_nothing() -> None:
    world = World(session("norn:r", state="passed"), activity(A, prompt="old"), todo("t0"))
    world.tracker.seed(world.view, ())
    assert world.states == {}
    world.put(10, activity(A, prompt="old", valid_from=10))
    world.put(11, replace(world.view.sessions["norn:r"], last_seen=at(11)))
    assert world.states == {}
    world.remove(12, Collection.TODOS, "t0")
    assert world.state_of().last_input == 12


def thread_row(key: ThreadKey, watermark: float | None) -> JournalThreadRow:
    return JournalThreadRow(
        thread_id=thread_id(key),
        repo_root=key.repo_root,
        branch=key.branch,
        name="repo",
        first_at=T0,
        last_at=None,
        watermark=None if watermark is None else at(watermark),
        where_text=None,
        where_at=None,
        last_attempt_at=None,
        error_kind=None,
        error_at=None,
    )


def test_seed_bootstraps_the_current_thread_with_activity_after_its_watermark() -> None:
    """J6: a stored thread that is its checkout's current thread, with a session's activity
    newer than its watermark, gets one entry ``quiet_seconds`` after the start."""
    world = World(
        activity(A, prompt="late", valid_from=50),
        project(OTHER),
        session(B, path=OTHER),
        activity(B, valid_from=10),
    )
    world.clock.t = 900
    other = ThreadKey(OTHER, "main")
    world.tracker.seed(
        world.view, (thread_row(MAIN, 40), thread_row(FEATURE, None), thread_row(other, 10))
    )
    assert set(world.states) == {MAIN}  # feature is not current; /w/other has nothing newer
    state = world.state_of()
    assert (state.dirty, state.stop_seen, state.last_input) == (True, True, 900)
    assert state.first_input == at(900)
    assert state.pending == {"turn_done": session_source(A)}
    assert due(1019, world.states, SETTINGS).threads == ()
    assert due(1020, world.states, SETTINGS).threads == (
        DueThread(MAIN, "turn_done", session_source(A)),
    )


def test_seed_bootstraps_a_thread_row_without_a_watermark() -> None:
    world = World(activity(A, valid_from=5))
    world.clock.t = 30
    world.tracker.seed(world.view, (thread_row(MAIN, None),))
    assert world.state_of().stop_seen
    assert world.tracker.thread_of(REPO) == MAIN
    assert world.tracker.thread_of("/w/nowhere") is None
