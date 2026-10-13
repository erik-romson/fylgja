"""The focus spans of the last 14 days, in memory (docs/decisions/0050).

Rankers read the recent spans on every request, and ``/now`` is fetched on every ``now.changed``.
So the spans are read from the store **once**, when the daemon starts (in a thread, off the event
loop), and then kept current from the :class:`~fylgja.core.spans.SpanEngine`'s actions: constant
work per focus change, and nothing is read per request.

**What it holds.** Every span that ended less than :data:`WINDOW` ago or is still open, plus the
whole last *complete* work session (one followed by a break of at least ``session_break``), even
when that session is older. So after a long absence the work session before the current one is
still known. The window never holds more than :data:`MAX_SPANS`; past that the oldest go first.

**The load race.** Actions that arrive before the load has finished are buffered and applied after
the loaded rows, keyed by ``(started_at, window_id)``: a row that is both loaded and re-sent comes
out as one row, and the action, which is newer, wins.

The window only reads the store, through the readers the daemon passes; the daemon's writer stays
the only writer.
"""

from __future__ import annotations

import asyncio
import logging
from bisect import bisect_left, insort
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Final

from fylgja.core.activity import split_sessions
from fylgja.core.spans import Action, Dropped
from fylgja.store.rows import SpanRow

__all__ = [
    "BEFORE_PAGE",
    "MAX_SPANS",
    "WINDOW",
    "BeforeReader",
    "SpanKey",
    "SpanReader",
    "SpanWindow",
    "span_key",
]

logger = logging.getLogger("fylgja.span_window")

WINDOW: Final = timedelta(days=14)
"""How far back the window holds every span."""
MAX_SPANS: Final = 50_000
"""The most spans the window holds."""
BEFORE_PAGE: Final = 500
"""Rows per read when the load walks back past the window for the last complete work session."""
DEFAULT_SESSION_BREAK: Final = timedelta(minutes=30)

SpanReader = Callable[[datetime], list[SpanRow]]
"""The spans that ended after ``since`` or are still open (``since`` is the argument)."""
BeforeReader = Callable[[datetime, int], list[SpanRow]]
"""At most ``limit`` closed spans that ended at or before ``before``, newest first."""

SpanKey = tuple[datetime, str]


def span_key(span: SpanRow) -> SpanKey:
    """What identifies a span: its start and its window, as the store closes and drops it."""
    return (span.started_at, span.window_id or "")


def _ended_by(span: SpanRow, moment: datetime) -> bool:
    return span.ended_at is not None and span.ended_at <= moment


def _last_complete(
    groups: Sequence[Sequence[SpanRow]], now: datetime, session_break: timedelta
) -> int | None:
    """The index of the last complete work session in ``groups``: one that another session
    follows, or that is closed and ended at least ``session_break`` before ``now``."""
    if not groups:
        return None
    last = groups[-1]
    if all(span.ended_at is not None for span in last):
        reach = max(span.ended_at for span in last if span.ended_at is not None)
        if now - reach >= session_break:
            return len(groups) - 1
    return len(groups) - 2 if len(groups) > 1 else None


def _holds_complete_session(
    spans: Sequence[SpanRow], *, cutoff: datetime, now: datetime, session_break: timedelta
) -> bool:
    """``spans`` (sorted by start) hold a whole complete work session: one that nothing before
    ``cutoff`` could still belong to."""
    groups = split_sessions(spans, now=now, session_break=session_break)
    index = _last_complete(groups, now, session_break)
    if index is None:
        return False
    return index > 0 or groups[0][0].started_at - cutoff >= session_break


def _walk_back(
    found: list[SpanRow],
    read_before: BeforeReader,
    *,
    cutoff: datetime,
    now: datetime,
    session_break: timedelta,
    limit: int,
) -> list[SpanRow]:
    """``found`` and the spans before ``cutoff`` back to the start of the last complete work
    session (newest first, a page at a time, never more than ``limit`` in all)."""
    older: list[SpanRow] = []
    earliest = found[0].started_at if found else None
    groups = split_sessions(found, now=now, session_break=session_break)
    complete = _last_complete(groups, now, session_break) is not None
    before = cutoff
    while len(found) + len(older) < limit:
        asked = min(BEFORE_PAGE, limit - len(found) - len(older))
        page = read_before(before, asked)
        for span in page:
            end = span.started_at if span.ended_at is None else span.ended_at
            if earliest is None:
                complete = now - end >= session_break
            elif earliest - end >= session_break:
                if complete:
                    return older[::-1] + found
                complete = True  # a new, older session: the one after it is complete
            older.append(span)
            earliest = span.started_at if earliest is None else min(earliest, span.started_at)
        if len(page) < asked:
            break
        before = page[-1].started_at
    return older[::-1] + found


class SpanWindow:
    """See the module docstring. Call every method on the event loop thread."""

    def __init__(
        self,
        *,
        session_break: timedelta = DEFAULT_SESSION_BREAK,
        window: timedelta = WINDOW,
        max_spans: int = MAX_SPANS,
    ) -> None:
        if session_break <= timedelta(0) or window <= timedelta(0):
            raise ValueError("the session break and the window must be positive")
        if max_spans < 1:
            raise ValueError("max_spans must be at least 1")
        self._break = session_break
        self._window = window
        self._max = max_spans
        self.version = 0
        """Bumps on the load and on every applied action."""
        self._rows: dict[SpanKey, SpanRow] = {}
        self._order: list[SpanKey] = []
        self._view: tuple[SpanRow, ...] | None = ()
        self._loaded = False
        self._pending: list[Action] = []
        self._kept = 0
        """How many spans at the start of the order ended before the window but were kept (the
        last complete work session), as of the version in ``_kept_version``."""
        self._kept_version = -1

    @property
    def loaded(self) -> bool:
        return self._loaded

    def __len__(self) -> int:
        return len(self._rows)

    # -- loading ----------------------------------------------------------------------------

    async def load(
        self, read: SpanReader, now: datetime, *, read_before: BeforeReader | None = None
    ) -> None:
        """Read the window from the store, in a thread, then apply what arrived meanwhile.

        ``read_before`` is asked only when the spans of the window hold no complete work session.
        A read that fails is logged; the window then holds only the spans that follow.
        """
        if self._loaded:
            raise RuntimeError("the span window was already loaded")
        try:
            found = await asyncio.to_thread(self._read, read, read_before, now)
        except Exception:
            logger.exception("span window load failed; it holds only the spans that follow")
            found = []
        for span in found:
            self._put(span)
        self._loaded = True
        pending, self._pending = self._pending, []
        for action in pending:
            self._apply(action)
        dropped = self._cap()
        self._changed()
        logger.info(
            "span window loaded %d span(s) and %d buffered action(s); dropped %d",
            len(found),
            len(pending),
            dropped,
        )

    def _read(
        self, read: SpanReader, read_before: BeforeReader | None, now: datetime
    ) -> list[SpanRow]:
        cutoff = now - self._window
        found = sorted(read(cutoff), key=span_key)
        if read_before is None or _holds_complete_session(
            found, cutoff=cutoff, now=now, session_break=self._break
        ):
            return found
        return _walk_back(
            found,
            read_before,
            cutoff=cutoff,
            now=now,
            session_break=self._break,
            limit=self._max,
        )

    # -- changing ---------------------------------------------------------------------------

    def apply(self, action: Action) -> None:
        """Take one span action: ``Opened`` and ``Closed`` replace the span with the same
        ``(started_at, window_id)``, ``Dropped`` removes it. Buffered until the load is done."""
        if not self._loaded:
            self._pending.append(action)
            return
        self._apply(action)
        dropped = self._cap()
        if dropped:
            logger.debug("span window full: dropped the oldest %d span(s)", dropped)
        self._changed()

    def _apply(self, action: Action) -> None:
        if isinstance(action, Dropped):
            self._remove(span_key(action.span))
        else:
            self._put(action.span)

    def _put(self, span: SpanRow) -> None:
        key = span_key(span)
        if key not in self._rows:
            insort(self._order, key)
        self._rows[key] = span

    def _remove(self, key: SpanKey) -> None:
        if self._rows.pop(key, None) is None:
            return
        index = bisect_left(self._order, key)
        del self._order[index]

    def _cap(self) -> int:
        excess = len(self._order) - self._max
        if excess <= 0:
            return 0
        for key in self._order[:excess]:
            del self._rows[key]
        del self._order[:excess]
        return excess

    def _changed(self) -> None:
        self.version += 1
        self._view = None

    # -- reading ----------------------------------------------------------------------------

    def view(self, now: datetime) -> tuple[SpanRow, ...]:
        """The spans as of ``now``, sorted by ``(started_at, window_id)``; an open span has
        ``ended_at = None``. A span that ended :data:`WINDOW` before ``now`` or earlier is pruned
        first, unless it belongs to the last complete work session."""
        cutoff = now - self._window
        first = self._kept if self._kept_version == self.version else 0
        if first < len(self._order) and _ended_by(self._rows[self._order[first]], cutoff):
            self._prune(now, cutoff)
        if self._view is None:
            self._view = tuple(self._rows[key] for key in self._order)
        return self._view

    def _prune(self, now: datetime, cutoff: datetime) -> None:
        rows = [self._rows[key] for key in self._order]
        groups = split_sessions(rows, now=now, session_break=self._break)
        index = _last_complete(groups, now, self._break)
        keep = set() if index is None else {span_key(span) for span in groups[index]}
        stale = {
            key for key in self._order if key not in keep and _ended_by(self._rows[key], cutoff)
        }
        if stale:
            for key in stale:
                del self._rows[key]
            self._order = [key for key in self._order if key not in stale]
            self._view = None
        kept = 0
        while kept < len(self._order) and _ended_by(self._rows[self._order[kept]], cutoff):
            kept += 1
        self._kept, self._kept_version = kept, self.version
