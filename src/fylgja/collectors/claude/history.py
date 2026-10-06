"""Claude's ``~/.claude/history.jsonl`` as a secondary "worked on" signal (WP5.2).

Claude appends one JSON object to ``history.jsonl`` for every prompt: the prompt text (``display``),
its ``timestamp`` and the ``project`` directory it was typed in, and more keys that change from
version to version. Fylgja wants **where and when**, never what: :func:`parse_entry` keeps the
project and the time and drops everything else on the spot (the prompt text is never stored,
logged or emitted), so an unknown key is harmless.

:class:`HistoryReader` reads the file incrementally (a byte offset, a torn last line carried to the
next poll; :class:`~fylgja.collectors.claude.transcript.TranscriptTail`) and emits at most one
``worked_on`` marker per project per ``coalesce_seconds``, a ``context_item`` event
(``context_kind`` ``worked_on``, ``data.project``) stamped with the prompt's own time. The first
poll reads the whole file but emits only the last ``backfill_seconds``; a marker's dedup key is made
of its content, so a restart that reads the same prompts again stores nothing twice.

Focus spans are the primary signal: a marker says that a prompt was typed in a project, not that
the window was in front. It never changes a span.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from fylgja.collectors.base import Clock, RecordSink
from fylgja.collectors.claude import parse
from fylgja.collectors.claude.transcript import TranscriptTail
from fylgja.collectors.seams import FileSystem
from fylgja.models import ContextItemEvent, ContextItemPayload

__all__ = [
    "WORKED_ON",
    "HistoryEntry",
    "HistoryReader",
    "parse_entry",
    "worked_on_event",
]

WORKED_ON: Final = "worked_on"
SOURCE: Final = "claude"
MAX_PATH: Final = 1024
MAX_READS_PER_POLL: Final = 64
"""Reads of one poll; a bigger backlog continues at the next poll."""


@dataclass(frozen=True)
class HistoryEntry:
    """One prompt: where and when."""

    project: str
    at: datetime


def _moment(value: object) -> datetime | None:
    """Epoch milliseconds (or seconds), or an ISO 8601 text with an offset."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, int | float):
            if not math.isfinite(value) or value < 0:
                return None
            seconds = value / 1000 if value > 1e11 else float(value)
            return datetime.fromtimestamp(seconds, UTC)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value)
            return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except (OverflowError, OSError, ValueError):
        return None
    return None


def parse_entry(entry: dict[str, Any]) -> HistoryEntry | None:
    """The project and time of a history object; ``None`` when either is missing or unusable.

    Any other key (the prompt text, pasted contents, the session id, whatever a later Claude adds)
    is ignored.
    """
    project = entry.get("project")
    if not isinstance(project, str) or not project.startswith("/") or "\x00" in project:
        return None
    if len(project) > MAX_PATH:
        return None
    at = _moment(entry.get("timestamp"))
    if at is None:
        return None
    return HistoryEntry(project.rstrip("/") or "/", at)


def worked_on_event(entry: HistoryEntry) -> ContextItemEvent:
    return ContextItemEvent(
        source=SOURCE,
        entity_id=f"{WORKED_ON}:{entry.project}",
        observed_at=entry.at,
        payload=ContextItemPayload(context_kind=WORKED_ON, data={"project": entry.project}),
    )


class HistoryReader:
    """Polls ``history.jsonl`` and emits the markers of the prompts added since the last poll."""

    def __init__(
        self,
        path: Path,
        fs: FileSystem,
        sink: RecordSink,
        clock: Clock,
        logger: logging.LoggerAdapter[logging.Logger],
        *,
        ignore_cwd: Sequence[str] = (),
        backfill_seconds: float = 86400.0,
        coalesce_seconds: float = 60.0,
        max_read: int | None = None,
    ) -> None:
        self._sink = sink
        self._clock = clock
        self._logger = logger
        self._ignore = tuple(ignore_cwd)
        self._backfill = timedelta(seconds=backfill_seconds)
        self._coalesce = timedelta(seconds=coalesce_seconds)
        self._tail = (
            TranscriptTail(fs, path)
            if max_read is None
            else TranscriptTail(fs, path, max_read=max_read)
        )
        self._last: dict[str, datetime] = {}
        self.emitted = 0
        """Markers emitted so far."""
        self.prompts = 0
        """Usable history entries read so far (a marker stands for one or more)."""
        self.skipped = 0
        """Lines that were not a usable entry."""

    def poll(self) -> int:
        """Read what was appended and emit the markers. Returns how many were emitted."""
        now = self._clock.now()
        cutoff = now - self._backfill
        emitted = 0
        for _ in range(MAX_READS_PER_POLL):
            update = self._tail.poll()
            if update.reset:
                self._last.clear()
            for raw in update.entries:
                entry = parse_entry(raw)
                if entry is None:
                    self.skipped += 1
                    continue
                self.prompts += 1
                emitted += self._mark(entry, cutoff, now)
            if not update.more:
                break
        if emitted:
            self._logger.debug("claude history: %d worked-on marker(s)", emitted)
        return emitted

    def _mark(self, entry: HistoryEntry, cutoff: datetime, now: datetime) -> int:
        if entry.at < cutoff or parse.cwd_ignored(entry.project, self._ignore):
            return 0
        last = self._last.get(entry.project)
        if last is not None and abs(entry.at - last) < self._coalesce:
            return 0
        self._last[entry.project] = entry.at
        self._sink.emit(worked_on_event(HistoryEntry(entry.project, min(entry.at, now))))
        self.emitted += 1
        return 1
