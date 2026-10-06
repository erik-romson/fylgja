"""Span algebra (WP5.2): overlaps never produced, idle back-dating, lock, shutdown, a daemon
restart mid-span, zero-length spans dropped, and the summary and focus-time arithmetic."""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from fylgja.core.spans import (
    Action,
    Closed,
    Dropped,
    EndReason,
    Opened,
    Span,
    SpanEngine,
    SpanTracker,
    Target,
    focus_seconds_by_workspace,
    focus_time_by_project,
    recover_stale,
    summarize,
)
from fylgja.core.writer import Tx
from fylgja.models import FocusEvent, FocusPayload, PresenceEvent, PresencePayload
from fylgja.store import db, queries

BASE = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
THRESHOLD = timedelta(minutes=5)


def at(minutes: float) -> datetime:
    return BASE + timedelta(minutes=minutes)


def target(window_id: str, workspace: str | None = "1", app: str = "com.app") -> Target:
    return Target(window_id, workspace, app, f"title {window_id}")


def tracker(attribute: object = None) -> SpanTracker:
    return SpanTracker(idle_threshold=THRESHOLD, attribute=attribute)  # type: ignore[arg-type]


def closed(actions: list[Action]) -> Span:
    [action] = [a for a in actions if isinstance(a, Closed)]
    return action.span


def kinds(actions: list[Action]) -> list[str]:
    return [type(a).__name__ for a in actions]


# --- opening and the next focus --------------------------------------------------------------


def test_a_focus_event_opens_a_span_and_the_next_one_closes_it() -> None:
    t = tracker()
    first = t.focus(at(0), target("a"))
    assert kinds(first) == ["Opened"]
    opened = first[0].span  # type: ignore[union-attr]
    assert (opened.window_id, opened.workspace, opened.app_bundle_id) == ("a", "1", "com.app")
    assert (opened.started_at, opened.ended_at, opened.end_reason) == (at(0), None, None)

    second = t.focus(at(10), target("b", "2"))
    assert kinds(second) == ["Closed", "Opened"]
    done = closed(second)
    assert (done.window_id, done.started_at, done.ended_at) == ("a", at(0), at(10))
    assert done.end_reason == EndReason.FOCUS.value
    assert t.open_span is not None
    assert t.open_span.window_id == "b"
    assert t.open_span.started_at == at(10)


def test_a_focus_event_for_no_window_closes_and_opens_nothing() -> None:
    t = tracker()
    t.focus(at(0), target("a"))
    actions = t.focus(at(3), None)
    assert kinds(actions) == ["Closed"]
    assert t.open_span is None
    assert t.focused is None
    assert t.focus(at(4), None) == []


def test_the_same_window_again_changes_nothing_but_another_workspace_splits_the_span() -> None:
    t = tracker()
    t.focus(at(0), target("a", "1"))
    assert t.focus(at(2), target("a", "1")) == []
    assert t.focus(at(3), target("a", None)) == []  # the event did not say where
    split = t.focus(at(5), target("a", "2"))
    assert kinds(split) == ["Closed", "Opened"]
    assert closed(split).workspace == "1"
    assert t.open_span is not None
    assert t.open_span.workspace == "2"


# --- idle ------------------------------------------------------------------------------------


def test_idle_closes_the_span_back_dated_to_the_start_of_the_idle_period() -> None:
    t = tracker()
    t.focus(at(0), target("a"))
    # the threshold was crossed at 12:00 (the event's time): idle began five minutes before
    done = closed(t.idle_start(at(12)))
    assert done.ended_at == at(7)
    assert done.end_reason == "idle"
    assert t.open_span is None
    assert t.suspended == {EndReason.IDLE}


def test_idle_end_starts_a_span_for_the_window_that_is_still_focused() -> None:
    t = tracker()
    t.focus(at(0), target("a"))
    t.idle_start(at(12))
    reopened = t.idle_end(at(30))
    assert kinds(reopened) == ["Opened"]
    span = reopened[0].span  # type: ignore[union-attr]
    assert (span.window_id, span.started_at) == ("a", at(30))
    assert t.suspended == frozenset()


def test_idle_that_began_before_the_span_leaves_a_zero_length_span_that_is_dropped() -> None:
    t = tracker()
    t.focus(at(10), target("a"))
    actions = t.idle_start(at(12))  # idle began at 07: before this span even started
    assert kinds(actions) == ["Dropped"]
    dropped = actions[0]
    assert isinstance(dropped, Dropped)
    assert dropped.span.started_at == at(10)


def test_a_focus_change_while_idle_is_remembered_and_starts_at_the_end_of_idle() -> None:
    t = tracker()
    t.focus(at(0), target("a"))
    t.idle_start(at(12))
    assert t.focus(at(20), target("b")) == []
    assert t.focused == target("b")
    opened = t.idle_end(at(25))
    assert opened[0].span.window_id == "b"  # type: ignore[union-attr]
    assert opened[0].span.started_at == at(25)  # type: ignore[union-attr]


def test_an_idle_end_with_no_idle_start_or_no_window_does_nothing() -> None:
    t = tracker()
    assert t.idle_end(at(0)) == []
    t.focus(at(1), target("a"))
    assert t.idle_end(at(2)) == []  # a span is already running
    assert t.open_span is not None
    assert t.open_span.started_at == at(1)


# --- lock ------------------------------------------------------------------------------------


def test_lock_closes_at_the_lock_and_unlock_starts_again() -> None:
    t = tracker()
    t.focus(at(0), target("a"))
    done = closed(t.lock_start(at(20)))
    assert (done.ended_at, done.end_reason) == (at(20), "lock")
    assert t.lock_start(at(21)) == []
    again = t.lock_end(at(50))
    assert again[0].span.started_at == at(50)  # type: ignore[union-attr]


def test_a_span_stays_closed_until_both_idle_and_lock_have_ended() -> None:
    t = tracker()
    t.focus(at(0), target("a"))
    t.lock_start(at(10))
    t.idle_start(at(20))  # idle began at 15, while locked
    assert t.suspended == {EndReason.IDLE, EndReason.LOCK}
    assert t.lock_end(at(40)) == []  # still idle
    resumed = t.idle_end(at(41))
    assert resumed[0].span.started_at == at(41)  # type: ignore[union-attr]


# --- shutdown and restart --------------------------------------------------------------------


def test_shutdown_closes_the_span_and_the_next_focus_opens_a_new_one() -> None:
    t = tracker()
    t.focus(at(0), target("a"))
    done = closed(t.shutdown(at(30)))
    assert (done.ended_at, done.end_reason) == (at(30), "shutdown")
    assert t.focused is None
    # coming back from idle or lock names no window: nothing opens
    assert t.idle_end(at(31)) == []
    assert t.lock_end(at(31)) == []
    assert t.open_span is None
    opened = t.focus(at(32), target("a"))
    assert kinds(opened) == ["Opened"]
    assert opened[0].span.started_at == at(32)  # type: ignore[union-attr]


def test_shutdown_without_a_span_does_nothing() -> None:
    assert tracker().shutdown(at(0)) == []


# --- zero length and order -------------------------------------------------------------------


def test_a_span_that_ends_where_it_began_is_dropped() -> None:
    t = tracker()
    t.focus(at(0), target("a"))
    actions = t.focus(at(0), target("b"))
    assert kinds(actions) == ["Dropped", "Opened"]
    assert t.open_span is not None
    assert t.open_span.window_id == "b"


def test_a_late_event_never_moves_a_boundary_back() -> None:
    t = tracker()
    t.focus(at(10), target("a"))
    actions = t.focus(at(5), target("b"))  # stamped before the span it follows
    assert kinds(actions) == ["Dropped", "Opened"]
    assert t.open_span is not None
    assert t.open_span.started_at == at(10)
    done = closed(t.focus(at(20), target("c")))
    assert (done.window_id, done.started_at, done.ended_at) == ("b", at(10), at(20))


def _random_run(seed: int) -> list[Span]:
    rng = random.Random(seed)
    t = tracker()
    stored: list[Span] = []
    clock = BASE
    for _ in range(60):
        clock += timedelta(seconds=rng.choice([0, 1, 30, 90, 600, -45, -600]))
        choice = rng.randrange(8)
        actions: list[Action]
        if choice < 3:
            actions = t.focus(clock, target(rng.choice("abc"), rng.choice(["1", "2", None])))
        elif choice == 3:
            actions = t.focus(clock, None)
        elif choice == 4:
            actions = t.idle_start(clock) if rng.random() < 0.6 else t.idle_end(clock)
        elif choice == 5:
            actions = t.lock_start(clock) if rng.random() < 0.6 else t.lock_end(clock)
        elif choice == 6:
            actions = t.shutdown(clock)
        else:
            actions = t.idle_end(clock) + t.lock_end(clock)
        stored.extend(a.span for a in actions if isinstance(a, Closed))
    return stored


@pytest.mark.parametrize("seed", range(40))
def test_random_signals_never_produce_overlapping_or_inverted_spans(seed: int) -> None:
    spans = _random_run(seed)
    for span in spans:
        assert span.ended_at is not None
        assert span.ended_at > span.started_at
    ordered = sorted(spans, key=lambda s: s.started_at)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        assert earlier.ended_at is not None
        assert earlier.ended_at <= later.started_at


# --- attribution -----------------------------------------------------------------------------


def test_attribution_is_taken_at_open_and_completed_at_close() -> None:
    known: dict[str, tuple[str | None, str | None]] = {}

    def attribute(window_id: str) -> tuple[str | None, str | None]:
        return known.get(window_id, (None, None))

    t = tracker(attribute)
    known["a"] = ("/code/x", "claude:1")
    opened = t.focus(at(0), target("a"))[0]
    assert isinstance(opened, Opened)
    assert (opened.span.project_path, opened.span.session_key) == ("/code/x", "claude:1")

    # the next span opens before the resolver placed the session: it is filled in at close
    t.focus(at(5), target("b"))
    known["b"] = ("/code/y", "claude:2")
    done = closed(t.focus(at(9), target("c")))
    assert (done.project_path, done.session_key) == ("/code/y", "claude:2")

    # a span that was attributed at open keeps it, whatever the window holds later
    known["d"] = ("/code/d", "claude:4")
    t.focus(at(10), target("d"))
    known["d"] = ("/other", "claude:9")
    kept = closed(t.focus(at(11), target("e")))
    assert (kept.window_id, kept.project_path, kept.session_key) == ("d", "/code/d", "claude:4")

    # a window nothing is placed in stays unattributed
    unplaced = closed(t.focus(at(12), target("f")))
    assert (unplaced.window_id, unplaced.project_path, unplaced.session_key) == ("e", None, None)


def test_the_idle_threshold_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        SpanTracker(idle_threshold=timedelta(0))


# --- the engine and the store ----------------------------------------------------------------


def focus_event(minutes: float, window_id: str | None, workspace: str = "1") -> FocusEvent:
    return FocusEvent(
        source="wm",
        entity_id="focus",
        observed_at=at(minutes),
        payload=FocusPayload(
            window_id=window_id, workspace=workspace, app_bundle_id="com.app", title="t"
        ),
    )


def presence(minutes: float, entity: str, state: str) -> PresenceEvent:
    return PresenceEvent(
        source="pres",
        entity_id=entity,
        observed_at=at(minutes),
        payload=PresencePayload(state=state),  # type: ignore[arg-type]
    )


class Rig:
    """An engine whose jobs run at once against a real store."""

    def __init__(self, tmp_path: object) -> None:
        self.path = tmp_path / "spans.db"  # type: ignore[operator]
        self.connection = db.open_store(self.path)
        self.engine = SpanEngine(
            self._run, lambda w: ("/code/p", "claude:1") if w == "a" else (None, None)
        )

    def _run(self, job: object) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        job(Tx(self.connection))  # type: ignore[operator]
        self.connection.execute("COMMIT")

    def rows(self) -> list[tuple[str | None, str, str | None, str | None, str | None]]:
        return [
            (r["window_id"], r["started_at"], r["ended_at"], r["end_reason"], r["project_path"])
            for r in self.connection.execute("SELECT * FROM focus_spans ORDER BY started_at, id")
        ]


@pytest.fixture
def rig(tmp_path: object) -> Iterator[Rig]:
    opened = Rig(tmp_path)
    yield opened
    opened.connection.close()


def stamp(minutes: float) -> str:
    return db.to_db_time(at(minutes))


def test_the_engine_stores_a_span_open_and_closes_it(rig: Rig) -> None:
    rig.engine.observe(focus_event(0, "a"))
    assert rig.rows() == [("a", stamp(0), None, None, "/code/p")]
    rig.engine.observe(focus_event(10, "b", "2"))
    assert rig.rows() == [
        ("a", stamp(0), stamp(10), "focus", "/code/p"),
        ("b", stamp(10), None, None, None),
    ]
    rig.engine.stop(at(15))
    assert rig.rows()[1] == ("b", stamp(10), stamp(15), "shutdown", None)


def test_the_engine_turns_presence_events_into_idle_and_lock_closes(rig: Rig) -> None:
    rig.engine.observe(focus_event(0, "a"))
    rig.engine.observe(presence(12, "idle", "idle"))
    rig.engine.observe(presence(20, "idle", "active"))
    rig.engine.observe(presence(30, "lock", "locked"))
    rig.engine.observe(presence(40, "lock", "active"))
    rig.engine.observe(presence(41, "other", "active"))  # not a signal it knows
    assert [(r[1], r[2], r[3]) for r in rig.rows()] == [
        (stamp(0), stamp(7), "idle"),
        (stamp(20), stamp(30), "lock"),
        (stamp(40), None, None),
    ]


def test_a_zero_length_span_is_deleted_from_the_store(rig: Rig) -> None:
    rig.engine.observe(focus_event(0, "a"))
    rig.engine.observe(focus_event(0, "b"))
    assert [(r[0], r[2]) for r in rig.rows()] == [("b", None)]


def test_focus_in_an_ignored_window_ends_the_span_and_starts_none(rig: Rig) -> None:
    rig.engine.observe(focus_event(0, "a"))
    rig.engine.private_focus(at(10))  # only the time is known
    rig.engine.private_focus(at(11))
    assert rig.rows() == [("a", stamp(0), stamp(10), "focus", "/code/p")]
    rig.engine.observe(focus_event(20, "a"))
    assert [r[1] for r in rig.rows()] == [stamp(0), stamp(20)]


def test_records_the_engine_does_not_know_are_ignored(rig: Rig) -> None:
    rig.engine.observe(focus_event(0, None))
    assert rig.rows() == []


def test_a_daemon_that_died_mid_span_is_closed_at_the_last_observed_time(rig: Rig) -> None:
    rig.engine.observe(focus_event(0, "a"))
    for minutes, key in ((5, "k1"), (25, "k2")):
        queries.insert_event(
            rig.connection,
            dedup_key=key,
            source="wm",
            record_type="event",
            entity_kind="focus",
            entity_id="focus",
            observed_at=stamp(minutes),
            received_at=stamp(minutes),
            payload_json="{}",
        )
    # a new daemon: its tracker knows nothing of the open row; recovery closes it
    rig.connection.execute("BEGIN IMMEDIATE")
    count = recover_stale(Tx(rig.connection), at(60))
    rig.connection.execute("COMMIT")
    assert count == 1
    assert rig.rows() == [("a", stamp(0), stamp(25), "shutdown", "/code/p")]
    # the next focus opens a new span; nothing is left to recover
    rig.engine = SpanEngine(rig._run)
    rig.engine.observe(focus_event(61, "a"))
    assert [r[2] for r in rig.rows()] == [stamp(25), None]


def test_recovery_deletes_a_span_that_has_no_length_and_never_ends_in_the_future(
    rig: Rig,
) -> None:
    rig.engine.observe(focus_event(0, "a"))
    rig.connection.execute("BEGIN IMMEDIATE")
    assert queries.close_stale_spans(rig.connection, last_observed=None, now=stamp(60)) == 1
    rig.connection.execute("COMMIT")
    assert rig.rows() == []  # nothing was observed after it began
    rig.engine = SpanEngine(rig._run)  # a new daemon
    rig.engine.observe(focus_event(1, "a"))
    rig.connection.execute("BEGIN IMMEDIATE")
    queries.close_stale_spans(rig.connection, last_observed=stamp(500), now=stamp(10))
    rig.connection.execute("COMMIT")
    assert rig.rows()[0][2] == stamp(10)


# --- summaries -------------------------------------------------------------------------------


def stored(
    start: float,
    end: float | None,
    *,
    workspace: str | None = "1",
    project: str | None = "/p",
    app: str | None = "com.app",
    reason: str | None = "focus",
) -> Span:
    return Span(
        window_id="w",
        workspace=workspace,
        app_bundle_id=app,
        title=None,
        project_path=project,
        session_key=None,
        started_at=at(start),
        ended_at=None if end is None else at(end),
        end_reason=None if end is None else reason,
    )


def test_summarize_clips_spans_to_the_window_and_totals_them() -> None:
    summary = summarize(
        [
            stored(0, 10, workspace="1"),
            stored(10, 40, workspace="2", project="/q", app="com.other"),
            stored(40, 50, workspace=None, project=None, app=None),
            stored(100, 110),  # after the window
        ],
        at(5),
        at(45),
        at(200),
    )
    assert summary.total_seconds == (5 + 30 + 5) * 60
    assert [(s.started_at, s.ended_at, s.clipped) for s in summary.spans] == [
        (at(5), at(10), True),
        (at(10), at(40), False),
        (at(40), at(45), True),
    ]
    assert [(t.key, t.seconds) for t in summary.workspaces] == [
        ("2", 1800),
        ("1", 300),
        (None, 300),
    ]
    assert [(t.key, t.seconds) for t in summary.projects] == [
        ("/q", 1800),
        ("/p", 300),
        (None, 300),
    ]
    assert [t.key for t in summary.apps] == ["com.other", "com.app", None]


def test_an_open_span_runs_to_now_and_a_window_in_the_past_clips_it() -> None:
    now_ = at(30)
    live = summarize([stored(10, None)], at(0), at(60), now_)
    assert [(s.ended_at, s.open, s.clipped) for s in live.spans] == [(at(30), True, False)]
    assert live.total_seconds == 20 * 60
    earlier = summarize([stored(10, None)], at(0), at(20), now_)
    assert [(s.ended_at, s.open, s.clipped) for s in earlier.spans] == [(at(20), True, True)]


def test_spans_outside_the_window_or_with_no_length_are_left_out() -> None:
    summary = summarize([stored(0, 10), stored(20, 30)], at(10), at(20), at(100))
    assert summary.spans == ()
    assert summary.total_seconds == 0
    assert summary.workspaces == ()
    assert summary.projects == ()
    assert summary.apps == ()


def test_focus_time_by_project_ignores_spans_without_a_project_or_workspace() -> None:
    found = focus_time_by_project(
        [
            stored(0, 10, workspace="1"),
            stored(10, 40, workspace="2"),
            stored(40, 50, workspace="1"),
            stored(50, 60, workspace=None),
            stored(60, 70, project=None),
            stored(70, 80, project="/q", workspace="3"),
        ],
        at(0),
        at(100),
        at(200),
    )
    assert found == {"/p": {"1": 1200.0, "2": 1800.0}, "/q": {"3": 600.0}}


def test_focus_seconds_by_workspace_clips_to_the_window_and_skips_unplaced_spans() -> None:
    found = focus_seconds_by_workspace(
        [
            stored(-30, 10, workspace="1"),  # clipped to the window: 10 minutes
            stored(10, 40, workspace="2"),
            stored(40, 50, workspace="1"),
            stored(50, 60, workspace=None),
            stored(60, 100, project=None, workspace="2"),  # no project: the workspace counts
            stored(150, 160, workspace="9"),  # outside the window
        ],
        at(0),
        at(100),
        at(200),
    )
    assert found == {"1": 1200.0, "2": 1800.0 + 2400.0}
