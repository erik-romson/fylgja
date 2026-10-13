"""Working time (docs/decisions/0050): active time is the union of the focus spans, a gap of the
session break splits work sessions, and away is the latest not-working gap of the idle threshold.

Every span is hand-built; nothing reads a clock.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from fylgja.core.activity import Activity, Away, WorkSession, build_activity, split_sessions
from fylgja.store.rows import SpanRow

DAY = datetime(2026, 10, 5, tzinfo=UTC)
IDLE = timedelta(minutes=5)
BREAK = timedelta(minutes=30)


def hm(hours: int, minutes: float = 0, *, day: int = 0) -> datetime:
    return DAY + timedelta(days=day, hours=hours, minutes=minutes)


def span(
    start: datetime,
    end: datetime | None,
    window: str = "w1",
    *,
    project: str | None = "/work/alpha/main",
    reason: str | None = "focus",
) -> SpanRow:
    return SpanRow(
        window_id=window,
        workspace="1",
        app_bundle_id="com.example.app",
        title=None,
        project_path=project,
        session_key=None,
        started_at=start,
        ended_at=end,
        end_reason=None if end is None else reason,
    )


def activity(spans: list[SpanRow], now: datetime, *, suspended: bool = False) -> Activity:
    return build_activity(
        spans, now=now, suspended=suspended, idle_threshold=IDLE, session_break=BREAK
    )


# --- active time -----------------------------------------------------------------------------


def test_nights_and_gaps_add_no_active_time() -> None:
    spans = [
        span(hm(9), hm(12)),
        span(hm(13), hm(14), "w2"),
        span(hm(9, day=1), hm(10, day=1)),
    ]
    found = activity(spans, hm(11, day=1))
    assert found.active_seconds == 5 * 3600
    assert found.active_since(hm(8)) == 5 * 3600
    assert found.active_since(hm(12)) == 2 * 3600  # lunch and the night count nothing
    assert found.active_since(hm(12, 30)) == 2 * 3600
    assert found.active_since(hm(13, 30)) == 1.5 * 3600
    assert found.active_since(hm(23)) == 3600
    assert found.active_since(hm(9, 45, day=1)) == 15 * 60
    assert found.active_since(hm(10, day=1)) == 0.0
    assert found.active_since(hm(11, day=1)) == 0.0


def test_active_since_runs_across_an_open_span_to_now() -> None:
    spans = [span(hm(9), hm(9, 30)), span(hm(10), None, "w2")]
    found = activity(spans, hm(10, 20))
    assert found.working
    assert found.open_span is not None
    assert (found.open_span.window_id, found.open_span.ended_at) == ("w2", hm(10, 20))
    assert found.open_span.end_reason is None  # clipped to now, still open
    assert found.spans[-1] == found.open_span
    assert found.active_since(hm(10, 10)) == 600
    assert found.active_since(hm(9, 15)) == 900 + 1200
    assert found.active_since(hm(10, 20)) == 0.0
    assert found.active_since(hm(11)) == 0.0
    later = activity(spans, hm(10, 50))
    assert later.active_since(hm(10, 10)) == 2400  # the open span grows with now


def test_nothing_moves_while_away() -> None:
    spans = [span(hm(9), hm(10)), span(hm(10), hm(11), "w2")]
    first = activity(spans, hm(12), suspended=True)
    second = activity(spans, hm(18), suspended=True)
    for moment in (hm(8), hm(9, 30), hm(10, 45), hm(11)):
        assert first.active_since(moment) == second.active_since(moment)
    assert not first.working
    assert not second.working


def test_overlapping_spans_are_counted_once() -> None:
    spans = [span(hm(9), hm(10)), span(hm(9, 30), hm(10, 30), "w2"), span(hm(10, 30), hm(11))]
    found = activity(spans, hm(12))
    assert found.intervals == ((hm(9), hm(11)),)
    assert found.active_seconds == 2 * 3600
    assert found.active_since(hm(9, 45)) == 75 * 60


def test_a_span_after_now_is_left_out_and_one_past_now_is_clipped() -> None:
    spans = [span(hm(9), hm(10, 30)), span(hm(11), hm(12), "w2")]
    found = activity(spans, hm(10))
    assert [(s.window_id, s.ended_at) for s in found.spans] == [("w1", hm(10))]
    assert found.active_seconds == 3600
    assert found.open_span is None


def test_no_spans_is_no_activity() -> None:
    found = activity([], hm(12))
    assert found.sessions == ()
    assert (found.current, found.previous, found.away, found.working) == (None, None, None, False)
    assert found.active_since(hm(0)) == 0.0
    assert found.active_in_current == 0.0


# --- work sessions ---------------------------------------------------------------------------


@pytest.mark.parametrize(("gap", "sessions"), [(31, 2), (30, 2), (29, 1)])
def test_a_gap_of_the_session_break_splits_work_sessions(gap: int, sessions: int) -> None:
    spans = [span(hm(9), hm(10)), span(hm(10, gap), hm(11), "w2")]
    found = activity(spans, hm(11))
    assert len(found.sessions) == sessions
    assert found.sessions[0].start == hm(9)
    assert found.sessions[-1].end == hm(11)
    assert sum(s.active_seconds for s in found.sessions) == found.active_seconds


def test_current_is_the_last_session_and_previous_the_one_before() -> None:
    spans = [span(hm(8), hm(9)), span(hm(10), hm(10, 30)), span(hm(10, 40), None, "w2")]
    found = activity(spans, hm(11))
    assert found.sessions == (
        WorkSession(hm(8), hm(9), 3600.0),
        WorkSession(hm(10), hm(11), 3000.0),
    )
    assert found.current == found.sessions[1]
    assert found.previous == found.sessions[0]
    assert found.active_in_current == 3000.0


def test_current_is_none_after_an_absence_of_the_session_break() -> None:
    spans = [span(hm(8), hm(9)), span(hm(10), hm(10, 30))]
    still = activity(spans, hm(10, 59))
    assert still.current == WorkSession(hm(10), hm(10, 30), 1800.0)
    assert still.previous == WorkSession(hm(8), hm(9), 3600.0)
    gone = activity(spans, hm(11))
    assert gone.current is None
    assert gone.previous == WorkSession(hm(10), hm(10, 30), 1800.0)
    assert gone.active_in_current == 0.0


def test_an_open_span_that_starts_now_begins_a_new_session() -> None:
    spans = [span(hm(8), hm(9)), span(hm(12), None, "w2")]
    found = activity(spans, hm(12))
    assert found.working
    assert found.current == WorkSession(hm(12), hm(12), 0.0)
    assert found.previous == WorkSession(hm(8), hm(9), 3600.0)
    assert found.away == Away(since=hm(9), until=hm(12))


def test_split_sessions_keeps_the_spans_of_each_session() -> None:
    spans = [span(hm(8), hm(9)), span(hm(9, 10), hm(9, 20), "w2"), span(hm(10), None)]
    groups = split_sessions(spans, now=hm(10, 30), session_break=BREAK)
    assert [[s.started_at for s in group] for group in groups] == [[hm(8), hm(9, 10)], [hm(10)]]


# --- away ------------------------------------------------------------------------------------


def test_away_is_open_while_suspended_and_closed_after_the_return() -> None:
    before = [span(hm(9), hm(10), reason="idle")]
    away = activity(before, hm(10, 20), suspended=True)
    assert away.away == Away(since=hm(10), until=None)
    assert not away.working
    back = activity([*before, span(hm(10, 20), None)], hm(10, 30))
    assert back.away == Away(since=hm(10), until=hm(10, 20))
    assert back.working


def test_away_is_open_while_no_span_runs() -> None:
    found = activity([span(hm(9), hm(10))], hm(10, 2))
    assert found.away == Away(since=hm(10), until=None)
    assert not found.working


def test_a_short_gap_is_not_away_and_the_latest_long_one_is() -> None:
    spans = [
        span(hm(8), hm(9)),
        span(hm(9, 20), hm(10), "w2"),  # 20 minutes away
        span(hm(10, 3), None),  # 3 minutes: below the idle threshold
    ]
    found = activity(spans, hm(10, 30))
    assert found.away == Away(since=hm(9), until=hm(9, 20))
    assert len(found.sessions) == 1


def test_no_long_gap_while_working_is_no_away() -> None:
    spans = [span(hm(9), hm(9, 58)), span(hm(10), None, "w2")]
    assert activity(spans, hm(10, 30)).away is None


def test_an_open_span_while_suspended_is_not_working() -> None:
    found = activity([span(hm(9), None)], hm(10), suspended=True)
    assert not found.working
    assert found.away == Away(since=hm(10), until=None)


# --- determinism -----------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(5))
def test_shuffled_spans_give_an_equal_activity(seed: int) -> None:
    spans = [
        span(hm(8), hm(8, 40)),
        span(hm(8, 40), hm(9), "w2"),
        span(hm(9), hm(9, 5), "w3", project=None),
        span(hm(10), hm(10, 30), "w2"),
        span(hm(10, 30), hm(10, 30, day=0) + timedelta(seconds=1), "w1"),
        span(hm(11), hm(12)),
        span(hm(12), None, "w3"),
    ]
    expected = activity(spans, hm(13))
    shuffled = list(spans)
    random.Random(seed).shuffle(shuffled)
    found = activity(shuffled, hm(13))
    assert found == expected
    assert found.active_since(hm(9, 2)) == expected.active_since(hm(9, 2))
    assert [s.started_at for s in found.spans] == sorted(s.started_at for s in spans)
