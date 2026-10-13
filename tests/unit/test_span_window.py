"""The in-memory span window (docs/decisions/0050): the load, the actions that arrive during it,
pruning that keeps the whole last work session, and the span cap.

The readers are plain functions over hand-built spans; nothing touches a store.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime, timedelta

import pytest

from fylgja.core import span_window as span_window_module
from fylgja.core.span_window import WINDOW, SpanWindow, span_key
from fylgja.core.spans import Closed, Dropped, Opened
from fylgja.store.rows import SpanRow

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
BREAK = timedelta(minutes=30)


def ago(days: float = 0, hours: float = 0, minutes: float = 0) -> datetime:
    return NOW - timedelta(days=days, hours=hours, minutes=minutes)


def span(start: datetime, end: datetime | None, window: str = "w1") -> SpanRow:
    return SpanRow(
        window_id=window,
        workspace="1",
        app_bundle_id="com.example.app",
        title=None,
        project_path="/work/alpha/main",
        session_key=None,
        started_at=start,
        ended_at=end,
        end_reason=None if end is None else "focus",
    )


def reader(*spans: SpanRow) -> span_window_module.SpanReader:
    """What the store answers: the spans that ended after ``since`` or are open."""

    def read(since: datetime) -> list[SpanRow]:
        return [s for s in spans if s.ended_at is None or s.ended_at > since]

    return read


class Before:
    """What the store answers for spans before a moment, newest first; records each ask."""

    def __init__(self, *spans: SpanRow) -> None:
        self.spans = sorted(
            (s for s in spans if s.ended_at is not None),
            key=lambda s: (s.ended_at, s.started_at),
            reverse=True,
        )
        self.asks: list[tuple[datetime, int]] = []

    def __call__(self, before: datetime, limit: int) -> list[SpanRow]:
        self.asks.append((before, limit))
        return [s for s in self.spans if s.ended_at is not None and s.ended_at <= before][:limit]


async def loaded(*spans: SpanRow, **options: object) -> SpanWindow:
    window = SpanWindow(session_break=BREAK, **options)  # type: ignore[arg-type]
    await window.load(reader(*spans), NOW)
    return window


def starts(window: SpanWindow, now: datetime = NOW) -> list[datetime]:
    return [s.started_at for s in window.view(now)]


# --- load and apply --------------------------------------------------------------------------


async def test_the_load_reads_the_window_and_actions_keep_it_current() -> None:
    first = span(ago(hours=3), ago(hours=2))
    second = span(ago(hours=2), None, "w2")
    old = span(ago(days=20), ago(days=20) + timedelta(hours=1))
    window = SpanWindow(session_break=BREAK)
    assert not window.loaded
    await window.load(reader(old, second, first), NOW)
    assert window.loaded
    assert window.version == 1
    assert window.view(NOW) == (first, second)  # sorted, and only the last 14 days

    closed = span(second.started_at, ago(minutes=10), "w2")
    window.apply(Closed(closed))
    opened = span(ago(minutes=10), None, "w3")
    window.apply(Opened(opened))
    assert window.version == 3
    assert window.view(NOW) == (first, closed, opened)
    assert len(window) == 3


async def test_an_action_during_the_load_is_not_lost_and_not_doubled() -> None:
    stored_open = span(ago(hours=1), None)
    stored_done = span(ago(hours=3), ago(hours=2), "w2")
    started, release = threading.Event(), threading.Event()

    def slow_read(since: datetime) -> list[SpanRow]:
        started.set()
        release.wait(5)
        return [stored_done, stored_open]

    window = SpanWindow(session_break=BREAK)
    task = asyncio.create_task(window.load(slow_read, NOW))
    assert await asyncio.to_thread(started.wait, 5)
    # while the store is read: the open span closes, a new one opens and is re-sent
    closed = span(stored_open.started_at, ago(minutes=5))
    opened = span(ago(minutes=5), None, "w3")
    window.apply(Closed(closed))
    window.apply(Opened(opened))
    window.apply(Opened(opened))
    assert window.view(NOW) == ()  # nothing is shown before the load is done
    release.set()
    await task
    assert window.view(NOW) == (stored_done, closed, opened)
    assert window.version == 1


async def test_a_row_that_is_loaded_and_re_sent_comes_out_once() -> None:
    stored = span(ago(hours=1), None)
    window = SpanWindow(session_break=BREAK)
    window.apply(Opened(stored))  # queued before the load; the store already holds it
    await window.load(reader(stored), NOW)
    assert window.view(NOW) == (stored,)


async def test_dropped_removes_the_span() -> None:
    kept = span(ago(hours=2), ago(hours=1))
    opened = span(ago(minutes=1), None, "w2")
    window = await loaded(kept)
    window.apply(Opened(opened))
    window.apply(Dropped(opened))
    window.apply(Dropped(span(ago(days=1), None, "w9")))  # unknown: nothing happens
    assert window.view(NOW) == (kept,)
    buffered = SpanWindow(session_break=BREAK)
    buffered.apply(Dropped(kept))
    await buffered.load(reader(kept), NOW)
    assert buffered.view(NOW) == ()


async def test_the_view_is_reused_until_something_changes() -> None:
    window = await loaded(span(ago(hours=2), ago(hours=1)))
    first = window.view(NOW)
    assert window.view(NOW) is first
    window.apply(Opened(span(ago(minutes=5), None, "w2")))
    assert window.view(NOW) is not first


async def test_a_second_load_is_refused_and_a_failed_one_leaves_an_empty_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    window = await loaded()
    with pytest.raises(RuntimeError, match="already loaded"):
        await window.load(reader(), NOW)

    def broken(since: datetime) -> list[SpanRow]:
        raise OSError("cannot read")

    failing = SpanWindow(session_break=BREAK)
    opened = span(ago(minutes=1), None)
    failing.apply(Opened(opened))
    with caplog.at_level(logging.ERROR, logger="fylgja.span_window"):
        await failing.load(broken, NOW)
    assert "span window load failed" in caplog.text
    assert failing.loaded
    assert failing.view(NOW) == (opened,)


def test_the_settings_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        SpanWindow(session_break=timedelta(0))
    with pytest.raises(ValueError, match="positive"):
        SpanWindow(window=timedelta(0))
    with pytest.raises(ValueError, match="at least 1"):
        SpanWindow(max_spans=0)


# --- pruning ---------------------------------------------------------------------------------


async def test_pruning_drops_spans_that_ended_before_the_window() -> None:
    old_session = [span(ago(days=13, hours=1), ago(days=13, minutes=50))]
    recent = [span(ago(days=1, hours=1), ago(days=1)), span(ago(hours=1), ago(minutes=40))]
    window = await loaded(*old_session, *recent)
    assert len(window.view(NOW)) == 3
    later = NOW + timedelta(days=2)  # the old session is now 15 days ago
    assert starts(window, later) == [s.started_at for s in recent]
    assert len(window) == 2


async def test_pruning_keeps_the_whole_last_work_session() -> None:
    # the last complete session straddles the edge of the window: 20 minutes apart, one span
    # ended 14 days and 10 minutes ago, the next 13 days and 23 hours ago
    edge = ago(days=14)
    straddling = [
        span(edge - timedelta(hours=1), edge - timedelta(minutes=10)),
        span(edge + timedelta(minutes=10), edge + timedelta(hours=1), "w2"),
    ]
    older = span(edge - timedelta(days=1), edge - timedelta(days=1) + timedelta(hours=1))
    current = span(ago(minutes=20), None, "w3")
    window = SpanWindow(session_break=BREAK)
    await window.load(reader(older, *straddling, current), NOW)
    for item in (older, *straddling):
        window.apply(Closed(item))  # all three are held now
    assert len(window) == 4
    assert window.view(NOW) == (*straddling, current)  # the older one went, the session stayed


async def test_after_a_long_absence_the_last_session_stays() -> None:
    session = [
        span(ago(days=20, hours=2), ago(days=20, hours=1)),
        span(ago(days=20, minutes=50), ago(days=20), "w2"),
    ]
    window = SpanWindow(session_break=BREAK)
    await window.load(reader(), NOW, read_before=Before(*session))
    assert window.view(NOW) == tuple(session)
    # back at work: the new session is not complete yet, so the old one is still the last
    back = span(NOW, None, "w3")
    window.apply(Opened(back))
    assert window.view(NOW + timedelta(minutes=5)) == (*session, back)
    # the new session ends and a break passes: the old one goes
    done = span(NOW, NOW + timedelta(hours=1), "w3")
    window.apply(Closed(done))
    assert window.view(NOW + timedelta(hours=2)) == (done,)


async def test_the_kept_session_is_not_pruned_again_on_every_view() -> None:
    session = [span(ago(days=20, hours=1), ago(days=20))]
    window = SpanWindow(session_break=BREAK)
    await window.load(reader(), NOW, read_before=Before(*session))
    first = window.view(NOW)
    assert window.view(NOW + timedelta(minutes=1)) is first


# --- the load walks back for the last complete session ----------------------------------------


async def test_the_load_reads_before_the_window_only_without_a_complete_session() -> None:
    before = Before(span(ago(days=20, hours=1), ago(days=20)))
    complete = [span(ago(days=2, hours=1), ago(days=2)), span(ago(hours=1), None, "w2")]
    window = SpanWindow(session_break=BREAK)
    await window.load(reader(*complete), NOW, read_before=before)
    assert before.asks == []
    assert window.view(NOW) == tuple(complete)


async def test_the_load_walks_back_page_by_page_to_the_start_of_the_last_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(span_window_module, "BEFORE_PAGE", 2)
    previous = [  # one session, 20 days ago: four spans 5 minutes apart
        span(ago(days=20, minutes=60), ago(days=20, minutes=50)),
        span(ago(days=20, minutes=45), ago(days=20, minutes=35), "w2"),
        span(ago(days=20, minutes=30), ago(days=20, minutes=20)),
        span(ago(days=20, minutes=15), ago(days=20, minutes=5), "w2"),
    ]
    earlier = span(ago(days=25, hours=1), ago(days=25))
    current = span(ago(minutes=20), None, "w3")
    before = Before(earlier, *previous)
    window = SpanWindow(session_break=BREAK)
    await window.load(reader(earlier, *previous, current), NOW, read_before=before)
    assert window.view(NOW) == (*previous, current)
    assert [limit for _, limit in before.asks] == [2, 2, 2]
    assert before.asks[0][0] == NOW - WINDOW
    assert before.asks[1][0] == previous[2].started_at


async def test_the_walk_back_completes_a_session_cut_by_the_window() -> None:
    edge = ago(days=14)
    cut = [
        span(edge - timedelta(hours=2), edge - timedelta(hours=1, minutes=50)),
        span(edge - timedelta(hours=1, minutes=40), edge - timedelta(minutes=5), "w2"),
        span(edge - timedelta(minutes=1), edge + timedelta(minutes=30)),
    ]
    earlier = span(edge - timedelta(days=1), edge - timedelta(days=1) + timedelta(hours=1))
    current = span(ago(minutes=20), None, "w3")
    everything = (earlier, *cut, current)
    window = SpanWindow(session_break=BREAK)
    await window.load(reader(*everything), NOW, read_before=Before(*everything))
    assert window.view(NOW) == (*cut, current)


async def test_the_walk_back_stops_at_the_cap_and_when_the_store_runs_out() -> None:
    spans = [span(ago(days=20, minutes=10 * i + 5), ago(days=20, minutes=10 * i)) for i in range(5)]
    capped = SpanWindow(session_break=BREAK, max_spans=3)
    await capped.load(reader(), NOW, read_before=Before(*spans))
    assert len(capped) == 3
    short = Before(spans[0])
    window = SpanWindow(session_break=BREAK)
    await window.load(reader(), NOW, read_before=short)
    assert window.view(NOW) == (spans[0],)
    assert len(short.asks) == 1


# --- the cap ---------------------------------------------------------------------------------


async def test_the_window_never_holds_more_than_max_spans() -> None:
    spans = [span(ago(hours=10 - i), ago(hours=10 - i, minutes=-30)) for i in range(5)]
    window = await loaded(*spans, max_spans=3)
    assert window.view(NOW) == tuple(spans[2:])
    newest = span(ago(minutes=10), None, "w2")
    window.apply(Opened(newest))
    assert window.view(NOW) == (*spans[3:], newest)
    assert span_key(newest) == (newest.started_at, "w2")
