"""Working time: work sessions, the away gap and active time, from focus spans.

``docs/decisions/0050``. Rankers age everything in **active** seconds, not wall-clock seconds:

* **active time is the union of the focus spans.** A span ends on a focus change, idle, a lock, a
  sleeping machine and shutdown, so time away from the keyboard is never in a span, and agent
  activity never enters it. Spans never overlap by construction (:mod:`fylgja.core.spans`); a union
  is taken anyway, so a stored overlap is counted once;
* **a work session** is a run of spans with no not-working gap of ``session_break`` or more
  between them (``[items] work_session_break_minutes``);
* **away** is the latest not-working gap of at least the idle threshold, or the gap after the last
  span while the person is idle, locked or focused on nothing (it is open: ``until`` is ``None``).

With no open span, :meth:`Activity.active_since` cannot grow, so nothing that is aged in active
seconds changes while the person is away. Wall time only splits work sessions.

The module is pure: it reads no clock and does no I/O. The daemon builds an :class:`Activity` from
its span window (:mod:`fylgja.core.span_window`) and its own clock.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from fylgja.store.rows import SpanRow

__all__ = [
    "Activity",
    "Away",
    "Interval",
    "WorkSession",
    "build_activity",
    "span_order",
    "split_sessions",
]

Interval = tuple[datetime, datetime]
"""A ``(start, end)`` pair of active time."""


@dataclass(frozen=True)
class WorkSession:
    """A run of focus spans with no gap of the session break or more between them."""

    start: datetime
    end: datetime
    """The end of its last span (now, for an open span)."""
    active_seconds: float
    """The union of its span time."""


@dataclass(frozen=True)
class Away:
    """A not-working gap of at least the idle threshold."""

    since: datetime
    """The end of the last span before the gap."""
    until: datetime | None
    """The start of the first span after it; ``None`` while still away."""


def span_order(span: SpanRow) -> tuple[object, ...]:
    """The order of spans: by ``(started_at, window_id)``, then by every other field, so the same
    spans sort the same way whatever order they came in."""
    return (
        span.started_at,
        span.window_id is not None,
        span.window_id or "",
        span.ended_at is None,
        span.ended_at or span.started_at,
        span.workspace or "",
        span.app_bundle_id or "",
        span.project_path or "",
        span.session_key or "",
        span.title or "",
        span.end_reason or "",
    )


def _end(span: SpanRow, now: datetime) -> datetime:
    return now if span.ended_at is None else span.ended_at


def split_sessions(
    spans: Iterable[SpanRow], *, now: datetime, session_break: timedelta
) -> list[list[SpanRow]]:
    """The spans grouped into work sessions, oldest first. ``spans`` must be sorted by
    ``started_at``; an open span runs to ``now``. A gap of ``session_break`` or more between the
    end of everything before a span and its start begins a new session."""
    groups: list[list[SpanRow]] = []
    reach: datetime | None = None
    for span in spans:
        end = _end(span, now)
        if reach is None or span.started_at - reach >= session_break:
            groups.append([span])
            reach = end
        else:
            groups[-1].append(span)
            reach = max(reach, end)
    return groups


@dataclass(frozen=True)
class Activity:
    """Working time as of ``now``: see the module docstring."""

    now: datetime
    spans: tuple[SpanRow, ...]
    """Sorted by :func:`span_order`; an open span is clipped to ``now`` (its ``end_reason`` stays
    ``None``). A span that starts after ``now``, or a closed one with no length left before it,
    is left out."""
    intervals: tuple[Interval, ...]
    """The union of ``spans``: sorted, disjoint ``(start, end)`` pairs."""
    sessions: tuple[WorkSession, ...]
    """Oldest first."""
    current: WorkSession | None
    """The last work session, or ``None`` when away for the session break or longer."""
    previous: WorkSession | None
    """The last work session before ``current`` (the last one when ``current`` is ``None``)."""
    away: Away | None
    """The latest not-working gap of at least the idle threshold; open while away."""
    working: bool
    """A span is open, and neither idle nor a lock holds."""
    open_span: SpanRow | None = None
    """The span still running, clipped to ``now`` (the last of ``spans``), or ``None``."""
    _starts: tuple[datetime, ...] = field(init=False, repr=False, compare=False)
    _ends: tuple[datetime, ...] = field(init=False, repr=False, compare=False)
    _before: tuple[float, ...] = field(init=False, repr=False, compare=False)
    """Active seconds before each interval: the cumulative array ``active_since`` bisects."""
    _total: float = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        before: list[float] = []
        total = 0.0
        for start, end in self.intervals:
            before.append(total)
            total += (end - start).total_seconds()
        object.__setattr__(self, "_starts", tuple(start for start, _ in self.intervals))
        object.__setattr__(self, "_ends", tuple(end for _, end in self.intervals))
        object.__setattr__(self, "_before", tuple(before))
        object.__setattr__(self, "_total", total)

    @property
    def active_seconds(self) -> float:
        """All active seconds the spans hold."""
        return self._total

    def active_since(self, moment: datetime) -> float:
        """Active seconds in ``(moment, now]``: the union of span time after ``moment``."""
        index = bisect_right(self._starts, moment)
        if index == 0:
            return self._total
        last = index - 1
        inside = (min(self._ends[last], moment) - self._starts[last]).total_seconds()
        return max(0.0, self._total - self._before[last] - max(0.0, inside))

    @property
    def active_in_current(self) -> float:
        """Active seconds of the current work session (0 while there is none)."""
        return 0.0 if self.current is None else self.current.active_seconds


def _clip(
    spans: Iterable[SpanRow], now: datetime
) -> tuple[list[SpanRow], list[Interval], SpanRow | None]:
    """The spans in order, each ended at ``now`` at the latest; their ``(start, end)`` pairs; and
    the open one."""
    clipped: list[SpanRow] = []
    pairs: list[Interval] = []
    running: SpanRow | None = None
    for span in sorted(spans, key=span_order):
        end = min(_end(span, now), now)
        if span.started_at > now or (end <= span.started_at and span.ended_at is not None):
            continue  # an open span that starts right now is kept: the person is working
        kept = span if end == span.ended_at else replace(span, ended_at=end)
        clipped.append(kept)
        pairs.append((kept.started_at, end))
        if span.ended_at is None:
            running = kept
    return clipped, pairs, running


def _union(pairs: Iterable[Interval]) -> list[Interval]:
    """Sorted ``pairs`` merged where they overlap or touch."""
    merged: list[Interval] = []
    for start, end in pairs:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _sessions(intervals: Sequence[Interval], session_break: timedelta) -> list[WorkSession]:
    sessions: list[WorkSession] = []
    start: datetime | None = None
    end: datetime | None = None
    active = 0.0
    for begin, finish in intervals:
        if start is not None and end is not None and begin - end >= session_break:
            sessions.append(WorkSession(start, end, active))
            start = None
        if start is None:
            start, active = begin, 0.0
        end = finish
        active += (finish - begin).total_seconds()
    if start is not None and end is not None:
        sessions.append(WorkSession(start, end, active))
    return sessions


def _away(
    intervals: Sequence[Interval], *, working: bool, idle_threshold: timedelta
) -> Away | None:
    if not intervals:
        return None
    if not working:
        return Away(since=intervals[-1][1], until=None)
    for (_, end), (start, _) in zip(reversed(intervals[:-1]), reversed(intervals[1:]), strict=True):
        if start - end >= idle_threshold:
            return Away(since=end, until=start)
    return None


def build_activity(
    spans: Sequence[SpanRow],
    *,
    now: datetime,
    suspended: bool,
    idle_threshold: timedelta,
    session_break: timedelta,
) -> Activity:
    """The working time of ``spans`` as of ``now``.

    ``suspended`` says idle or a lock holds right now (the span tracker's state). The order of
    ``spans`` does not matter: they are sorted before every sum.
    """
    clipped, pairs, running = _clip(spans, now)
    intervals = _union(pairs)
    sessions = _sessions(intervals, session_break)
    working = running is not None and not suspended
    current = sessions[-1] if sessions and now - sessions[-1].end < session_break else None
    if current is not None:
        previous = sessions[-2] if len(sessions) > 1 else None
    else:
        previous = sessions[-1] if sessions else None
    return Activity(
        now=now,
        spans=tuple(clipped),
        intervals=tuple(intervals),
        sessions=tuple(sessions),
        current=current,
        previous=previous,
        away=_away(intervals, working=working, idle_threshold=idle_threshold),
        working=working,
        open_span=running,
    )
