"""Focus spans: where the user's attention was, derived from focus events (WP5.2, A12).

``PLAN-final-plan.md`` §4.1 (``focus_spans``), §4.2 (``/history``) and WP5.2; ``PLAN.md`` §3 rule 4
and §12 Phase 5. The module holds three layers.

**The algebra** (:class:`SpanTracker`) is a pure state machine over time-stamped signals; it
reads no clock and does no I/O. A span

* **opens** on a focus event for a window (a focus event that names no window opens nothing);
* **closes** on the next focus change (``focus``), on idle (``idle``), on a screen lock (``lock``)
  and on shutdown (``shutdown``).

An ``idle.start`` event is stamped when the idle threshold was crossed, so the idle period began
``threshold`` seconds before its ``observed_at`` and the span ends *there*, not when the event was
noticed. When the idle period or the lock ends, the span of the window that is still focused starts
again: nothing was focused in between, and the user is back in the same window.

Overlaps are impossible by construction: the tracker keeps a floor (the latest span boundary) and
no timestamp, however late or out of order, moves a boundary below it. A span that ends where it
began is not a span: it is reported as :class:`Dropped`, never stored.

**The engine** (:class:`SpanEngine`) is the state-engine hook. The daemon shows it every record
the ingest gate accepted (titles are redacted by then); it feeds focus and presence events to the
tracker, attributes each span to a project and a session through ``attribute`` (the resolver's
placements) and queues the writes through the writer: a span is stored open when it starts and
closed when it ends, so a daemon that dies mid-span leaves an open row. :func:`recover_stale`
closes such a row at the last time the store observed anything, with ``end_reason = shutdown``.

**The summary** (:func:`summarize`) clips stored spans to a window and totals the seconds per
workspace, per project and per app. :func:`focus_time_by_project` is the resolver's input for
project → workspace (``PLAN.md`` §3 rule 4).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from fylgja.core.writer import Tx
from fylgja.models import FocusEvent, PresenceEvent, Record
from fylgja.store import db, queries
from fylgja.store.rows import SpanRow, WorkedOnRow

__all__ = [
    "Action",
    "Attribute",
    "Closed",
    "Dropped",
    "EndReason",
    "History",
    "Opened",
    "Span",
    "SpanEngine",
    "SpanTracker",
    "Summary",
    "SummarySpan",
    "Target",
    "Total",
    "focus_seconds_by_workspace",
    "focus_time_by_project",
    "last_focus_by_project",
    "recover_stale",
    "summarize",
]

Span = SpanRow
"""A stored span; ``ended_at`` is ``None`` while it is open."""

IDLE_ENTITY: Final = "idle"
LOCK_ENTITY: Final = "lock"
"""The ``entity_id`` of the presence events (``docs/collectors/presence.md``)."""

Attribute = Callable[[str], tuple[str | None, str | None]]
"""``window_id`` to ``(project_path, session_key)``; either may be unknown."""


class EndReason(StrEnum):
    FOCUS = "focus"
    IDLE = "idle"
    LOCK = "lock"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class Target:
    """The window that has focus."""

    window_id: str
    workspace: str | None = None
    app_bundle_id: str | None = None
    title: str | None = None


# --------------------------------------------------------------------------------------------
# The algebra
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Opened:
    """A span started: store it open."""

    span: Span


@dataclass(frozen=True)
class Closed:
    """The open span ended: ``span`` is the final row."""

    span: Span


@dataclass(frozen=True)
class Dropped:
    """The open span ended where it began: delete it. ``span`` is the span as opened."""

    span: Span


Action = Opened | Closed | Dropped


def _no_attribution(window_id: str) -> tuple[str | None, str | None]:
    return None, None


class SpanTracker:
    """The span state machine. Every method takes the time of its signal and returns the actions
    to store; see the module docstring.

    ``attribute`` is asked when a span opens and, if the span had no attribution then (the
    resolver may be a moment behind), once more when it closes.
    """

    def __init__(
        self,
        *,
        idle_threshold: timedelta,
        attribute: Attribute | None = None,
    ) -> None:
        if idle_threshold <= timedelta(0):
            raise ValueError("idle_threshold must be positive")
        self._threshold = idle_threshold
        self._attribute: Attribute = _no_attribution if attribute is None else attribute
        self._open: Span | None = None
        self._current: Target | None = None
        self._suspended: set[EndReason] = set()
        self._floor: datetime | None = None

    # -- reading ----------------------------------------------------------------------------

    @property
    def open_span(self) -> Span | None:
        return self._open

    @property
    def focused(self) -> Target | None:
        """The window that has focus, even while no span runs for it (idle or locked)."""
        return self._current

    @property
    def suspended(self) -> frozenset[EndReason]:
        """``idle`` and ``lock`` while they hold."""
        return frozenset(self._suspended)

    # -- helpers ----------------------------------------------------------------------------

    def _clamp(self, at: datetime) -> datetime:
        return at if self._floor is None else max(at, self._floor)

    def _close(self, at: datetime, reason: EndReason) -> list[Action]:
        span = self._open
        if span is None:
            return []
        self._open = None
        end = max(at, span.started_at)
        self._floor = end
        if end <= span.started_at:
            return [Dropped(span)]
        project, session = span.project_path, span.session_key
        if (project is None or session is None) and span.window_id is not None:
            found_project, found_session = self._attribute(span.window_id)
            project = project or found_project
            session = session or found_session
        closed = replace(
            span,
            ended_at=end,
            end_reason=reason.value,
            project_path=project,
            session_key=session,
        )
        return [Closed(closed)]

    def _start(self, at: datetime) -> list[Action]:
        target = self._current
        if target is None or self._suspended or self._open is not None:
            return []
        start = self._clamp(at)
        project, session = self._attribute(target.window_id)
        self._open = Span(
            window_id=target.window_id,
            workspace=target.workspace,
            app_bundle_id=target.app_bundle_id,
            title=target.title,
            project_path=project,
            session_key=session,
            started_at=start,
            ended_at=None,
            end_reason=None,
        )
        self._floor = start
        return [Opened(self._open)]

    # -- signals ----------------------------------------------------------------------------

    def focus(self, at: datetime, target: Target | None) -> list[Action]:
        """Focus moved to ``target`` (``None``: to no window)."""
        span = self._open
        if (
            span is not None
            and target is not None
            and span.window_id == target.window_id
            and (target.workspace is None or span.workspace == target.workspace)
        ):
            self._current = target
            return []
        at = self._clamp(at)
        actions = self._close(at, EndReason.FOCUS)
        self._current = target
        return actions + self._start(at)

    def idle_start(self, observed_at: datetime) -> list[Action]:
        """The idle threshold was crossed at ``observed_at``: the span ended a threshold ago."""
        self._suspended.add(EndReason.IDLE)
        return self._close(observed_at - self._threshold, EndReason.IDLE)

    def idle_end(self, at: datetime) -> list[Action]:
        """The first input after an idle period, at ``at``."""
        self._suspended.discard(EndReason.IDLE)
        return self._start(at)

    def lock_start(self, at: datetime) -> list[Action]:
        self._suspended.add(EndReason.LOCK)
        return self._close(at, EndReason.LOCK)

    def lock_end(self, at: datetime) -> list[Action]:
        self._suspended.discard(EndReason.LOCK)
        return self._start(at)

    def shutdown(self, at: datetime) -> list[Action]:
        """The daemon stops. Nothing stays focused: the next focus event starts a span."""
        actions = self._close(at, EndReason.SHUTDOWN)
        self._current = None
        self._suspended.clear()
        return actions


# --------------------------------------------------------------------------------------------
# The engine hook
# --------------------------------------------------------------------------------------------

Enqueue = Callable[[Callable[[Tx], object]], None]


def _target_of(event: FocusEvent) -> Target | None:
    payload = event.payload
    if payload.window_id is None:
        return None
    return Target(payload.window_id, payload.workspace, payload.app_bundle_id, payload.title)


def _persist(action: Action) -> Callable[[Tx], object]:
    """The writer job of one action."""
    span = action.span
    started = db.to_db_time(span.started_at)

    def open_job(tx: Tx) -> object:
        queries.insert_open_span(
            tx.connection,
            window_id=span.window_id,
            workspace=span.workspace,
            app_bundle_id=span.app_bundle_id,
            title=span.title,
            project_path=span.project_path,
            session_key=span.session_key,
            started_at=started,
        )
        return None

    def close_job(tx: Tx) -> object:
        if span.ended_at is None or span.end_reason is None:
            return False
        return queries.close_open_span(
            tx.connection,
            window_id=span.window_id,
            started_at=started,
            ended_at=db.to_db_time(span.ended_at),
            end_reason=span.end_reason,
            project_path=span.project_path,
            session_key=span.session_key,
        )

    def drop_job(tx: Tx) -> object:
        return queries.drop_open_span(tx.connection, window_id=span.window_id, started_at=started)

    match action:
        case Opened():
            return open_job
        case Closed():
            return close_job
        case Dropped():
            return drop_job


class SpanEngine:
    """Runs a :class:`SpanTracker` for the daemon and writes its spans through the writer.

    :meth:`observe` is an ingest-tap observer (call it on the event loop thread, after the record
    was queued, so a span job is queued behind the raw event). A record it does not understand is
    ignored.
    """

    def __init__(
        self,
        enqueue: Enqueue,
        attribute: Attribute | None = None,
        *,
        idle_threshold_seconds: float = 300.0,
    ) -> None:
        self._enqueue = enqueue
        self.tracker = SpanTracker(
            idle_threshold=timedelta(seconds=idle_threshold_seconds), attribute=attribute
        )

    def _write(self, actions: Iterable[Action]) -> None:
        for action in actions:
            self._enqueue(_persist(action))

    def observe(self, record: Record) -> None:
        tracker = self.tracker
        if isinstance(record, FocusEvent):
            self._write(tracker.focus(record.observed_at, _target_of(record)))
        elif isinstance(record, PresenceEvent):
            self._write(self._presence(record))

    def _presence(self, event: PresenceEvent) -> list[Action]:
        tracker = self.tracker
        at = event.observed_at
        state = event.payload.state
        if event.entity_id == IDLE_ENTITY:
            return tracker.idle_start(at) if state == "idle" else tracker.idle_end(at)
        if event.entity_id == LOCK_ENTITY:
            return tracker.lock_start(at) if state == "locked" else tracker.lock_end(at)
        return []

    def private_focus(self, at: datetime) -> None:
        """Focus moved to a window the privacy filter ignores (an ignored app or title).

        The window is never stored, so the span before it ends and none starts: the ignored window
        is "no window". Only the time is known here.
        """
        self._write(self.tracker.focus(at, None))

    def stop(self, at: datetime) -> None:
        """Close the open span for a stopping daemon."""
        self._write(self.tracker.shutdown(at))


def recover_stale(tx: Tx, now: datetime) -> int:
    """Close the spans a daemon that died left open (see the module docstring). Returns how many."""
    return queries.close_stale_spans(
        tx.connection,
        last_observed=queries.last_observed_at(tx.connection),
        now=db.to_db_time(now),
    )


# --------------------------------------------------------------------------------------------
# Totals
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SummarySpan:
    """A stored span clipped to the window asked for."""

    window_id: str | None
    workspace: str | None
    app_bundle_id: str | None
    title: str | None
    project_path: str | None
    session_key: str | None
    started_at: datetime
    ended_at: datetime
    end_reason: str | None
    seconds: float
    open: bool
    """The span has not ended; ``ended_at`` is now, or the end of the window if that is earlier."""
    clipped: bool
    """The span began before the window or ended after it."""


@dataclass(frozen=True)
class Total:
    key: str | None
    seconds: float


@dataclass(frozen=True)
class Summary:
    since: datetime
    until: datetime
    spans: tuple[SummarySpan, ...]
    workspaces: tuple[Total, ...]
    projects: tuple[Total, ...]
    apps: tuple[Total, ...]
    total_seconds: float


@dataclass(frozen=True)
class History:
    """What ``/history`` answers: the summary of the spans and the secondary signal."""

    summary: Summary
    worked_on: tuple[WorkedOnRow, ...]


def _clip(span: Span, since: datetime, until: datetime, now: datetime) -> SummarySpan | None:
    still_open = span.ended_at is None
    end = now if span.ended_at is None else span.ended_at
    start = max(span.started_at, since)
    stop = min(end, until)
    if stop <= start:
        return None
    return SummarySpan(
        window_id=span.window_id,
        workspace=span.workspace,
        app_bundle_id=span.app_bundle_id,
        title=span.title,
        project_path=span.project_path,
        session_key=span.session_key,
        started_at=start,
        ended_at=stop,
        end_reason=span.end_reason,
        seconds=(stop - start).total_seconds(),
        open=still_open,
        clipped=start != span.started_at or stop != end,
    )


def _totals(
    spans: Sequence[SummarySpan], key: Callable[[SummarySpan], str | None]
) -> tuple[Total, ...]:
    sums: dict[str | None, float] = {}
    for span in spans:
        sums[key(span)] = sums.get(key(span), 0.0) + span.seconds
    ordered = sorted(sums.items(), key=lambda item: (item[0] is None, -item[1], item[0] or ""))
    return tuple(Total(name, seconds) for name, seconds in ordered)


def summarize(spans: Iterable[Span], since: datetime, until: datetime, now: datetime) -> Summary:
    """Clip ``spans`` to ``[since, until)`` and total them. An open span runs to ``now``.

    A span that does not overlap the window is left out. Totals are ordered by seconds, largest
    first; the entry for an unknown workspace, project or app (``None``) comes last.
    """
    clipped = [item for span in spans if (item := _clip(span, since, until, now)) is not None]
    clipped.sort(key=lambda item: (item.started_at, item.ended_at))
    return Summary(
        since=since,
        until=until,
        spans=tuple(clipped),
        workspaces=_totals(clipped, lambda s: s.workspace),
        projects=_totals(clipped, lambda s: s.project_path),
        apps=_totals(clipped, lambda s: s.app_bundle_id),
        total_seconds=sum(item.seconds for item in clipped),
    )


def focus_time_by_project(
    spans: Iterable[Span], since: datetime, until: datetime, now: datetime
) -> dict[str, dict[str, float]]:
    """Seconds of focus per project and workspace in ``[since, until)``: the input of the
    resolver's project → workspace join. A span without a project or a workspace says nothing
    about either and is left out."""
    found: dict[str, dict[str, float]] = {}
    for span in spans:
        clipped = _clip(span, since, until, now)
        if clipped is None or clipped.project_path is None or clipped.workspace is None:
            continue
        per_workspace = found.setdefault(clipped.project_path, {})
        per_workspace[clipped.workspace] = (
            per_workspace.get(clipped.workspace, 0.0) + clipped.seconds
        )
    return found


def last_focus_by_project(spans: Iterable[Span], now: datetime) -> dict[str, datetime]:
    """When each project last had the person's focus: the end of its latest span, or ``now`` while
    a span is open. A span without a project says nothing and is left out (waiting-on-me's
    "touched today")."""
    found: dict[str, datetime] = {}
    for span in spans:
        if span.project_path is None:
            continue
        last = now if span.ended_at is None else span.ended_at
        if span.project_path not in found or last > found[span.project_path]:
            found[span.project_path] = last
    return found


def focus_seconds_by_workspace(
    spans: Iterable[Span], since: datetime, until: datetime, now: datetime
) -> dict[str, float]:
    """Seconds of focus per workspace in ``[since, until)`` (the "most focus today" of the
    ranking). A span that names no workspace says nothing and is left out."""
    found: dict[str, float] = {}
    for span in spans:
        clipped = _clip(span, since, until, now)
        if clipped is None or clipped.workspace is None:
            continue
        found[clipped.workspace] = found.get(clipped.workspace, 0.0) + clipped.seconds
    return found
