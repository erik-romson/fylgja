"""Nightly maintenance: retention, ``PRAGMA optimize`` and the WAL checkpoint
(``PLAN-final-plan.md`` §4.1, A12; ``PLAN.md`` §5).

**Retention.**

* Raw ``events`` observed more than 30 days ago are deleted.
* ``focus_spans`` and the closed change intervals of ``windows_snapshot``, ``session_snapshots``
  and ``usage_snapshots`` that *ended* more than one year (365 days) ago are deleted. Open spans
  and intervals are current state and are never deleted.
* ``todos`` are kept forever; nothing else is deleted either. That includes the ``journal_threads``,
  ``journal_entries`` and ``journal_items`` rows (decision 0057): a model call cannot be repeated.
* Quarantined hook-inbox files (``hook-inbox/<source>/quarantine/``) whose mtime is more than 7 days
  old are deleted (:func:`prune_quarantine`). They are plain files, so the writer is not involved.

A row exactly at a boundary is kept: the cut-off is strict ("older than").

**Rebuildability (A12).** Derived state is rebuildable only from the raw events still retained,
that is, within the 30-day raw-event window. Spans and snapshots older than that are retained
archives: they are kept for a year, but nothing can recompute them once their events are gone.

**Schedule.** :class:`MaintenanceJob` runs once a night at a fixed local time (03:30 by default).
It sleeps through the injected clock, so tests drive it with a fake one. Every write goes through
the writer, which stays the only SQLite writer. After retention it runs ``PRAGMA optimize``, and
``wal_checkpoint(TRUNCATE)`` when the writer is idle.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Final

from fylgja import paths
from fylgja.collectors.base import Clock
from fylgja.core.writer import Tx, Writer
from fylgja.store import db, queries

__all__ = [
    "ARCHIVE_RETENTION",
    "DEFAULT_RUN_AT",
    "QUARANTINE_RETENTION",
    "RAW_EVENT_RETENTION",
    "MaintenanceJob",
    "MaintenanceReport",
    "RetentionPolicy",
    "apply_retention",
    "prune_quarantine",
    "seconds_until",
]

logger = logging.getLogger("fylgja.retention")

RAW_EVENT_RETENTION: Final = timedelta(days=30)
ARCHIVE_RETENTION: Final = timedelta(days=365)
QUARANTINE_RETENTION: Final = timedelta(days=7)
DEFAULT_RUN_AT: Final = time(3, 30)
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class RetentionPolicy:
    raw_events: timedelta = RAW_EVENT_RETENTION
    archives: timedelta = ARCHIVE_RETENTION
    quarantine: timedelta = QUARANTINE_RETENTION


@dataclass(frozen=True)
class MaintenanceReport:
    ran_at: datetime
    deleted: Mapping[str, int]
    optimized: bool
    checkpointed: bool | None
    """``True`` after a full checkpoint, ``False`` when readers kept it partial, ``None`` when it
    was skipped because the writer was busy."""
    quarantine_deleted: int = 0
    """Quarantined inbox files removed because they were older than the policy allows."""


def apply_retention(
    connection: sqlite3.Connection, now: datetime, policy: RetentionPolicy | None = None
) -> dict[str, int]:
    """Delete what the policy expires as of ``now``. Returns deleted rows per table."""
    rules = RetentionPolicy() if policy is None else policy
    return queries.delete_expired(
        connection,
        events_before=db.to_db_time(now - rules.raw_events),
        archives_before=db.to_db_time(now - rules.archives),
    )


def prune_quarantine(root: Path, now: datetime, retention: timedelta = QUARANTINE_RETENTION) -> int:
    """Delete the quarantined files of every inbox below ``root`` that are older than ``retention``.

    A file exactly ``retention`` old is kept ("older than" is strict). Only regular files directly
    inside ``<root>/<source>/quarantine/`` are considered; links, directories and an inbox or
    quarantine directory that is itself a link are left alone. A missing ``root`` deletes nothing.
    Returns the number of files deleted.
    """
    cutoff_ns = (
        (now - _EPOCH) // timedelta(microseconds=1) - retention // timedelta(microseconds=1)
    ) * 1000
    deleted = 0
    try:
        sources = sorted(os.listdir(root))
    except OSError:
        return 0
    for source in sources:
        directory = root / source / "quarantine"
        try:
            if os.path.islink(root / source) or os.path.islink(directory):
                continue
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            path = directory / name
            try:
                info = os.lstat(path)
                if (info.st_mode & 0o170000) == 0o100000 and info.st_mtime_ns < cutoff_ns:
                    os.unlink(path)
                    deleted += 1
            except OSError:
                continue
    return deleted


def seconds_until(now: datetime, at: time, tz: tzinfo | None = None) -> float:
    """Seconds from ``now`` to the next ``at`` in ``tz`` (the system zone when ``None``).

    When ``now`` is exactly ``at``, the next run is a day later.
    """
    local = now.astimezone(tz)
    target = datetime.combine(local.date(), at, tzinfo=local.tzinfo)
    if target <= local:
        target = datetime.combine(local.date() + timedelta(days=1), at, tzinfo=local.tzinfo)
    return (target - local).total_seconds()


class MaintenanceJob:
    """The nightly job. :meth:`run` lives until cancelled; :meth:`run_once` runs it now."""

    def __init__(
        self,
        writer: Writer,
        *,
        clock: Clock,
        policy: RetentionPolicy | None = None,
        run_at: time = DEFAULT_RUN_AT,
        tz: tzinfo | None = None,
        is_idle: Callable[[], bool] | None = None,
        inbox_root: Path | None = None,
    ) -> None:
        self._writer = writer
        self._clock = clock
        self._policy = RetentionPolicy() if policy is None else policy
        self._run_at = run_at
        self._tz = tz
        self._inbox_root = inbox_root
        self._is_idle: Callable[[], bool] = self._writer_idle if is_idle is None else is_idle
        self.last_report: MaintenanceReport | None = None

    def _writer_idle(self) -> bool:
        return self._writer.idle

    async def run_once(self) -> MaintenanceReport:
        now = self._clock.now()
        policy = self._policy

        def expire(tx: Tx) -> dict[str, int]:
            return apply_retention(tx.connection, now, policy)

        deleted = await self._writer.execute(expire)
        root = paths.hook_inbox_root() if self._inbox_root is None else self._inbox_root
        quarantined = await asyncio.to_thread(prune_quarantine, root, now, policy.quarantine)
        await self._writer.execute(lambda tx: queries.optimize(tx.connection), transaction=False)
        checkpointed: bool | None = None
        if self._is_idle():
            checkpointed = await self._writer.execute(
                lambda tx: queries.checkpoint_truncate(tx.connection), transaction=False
            )
        report = MaintenanceReport(
            now,
            deleted,
            optimized=True,
            checkpointed=checkpointed,
            quarantine_deleted=quarantined,
        )
        self.last_report = report
        logger.info(
            "maintenance: deleted %s, quarantine=%d; checkpoint %s",
            ", ".join(f"{table}={count}" for table, count in deleted.items()),
            quarantined,
            {True: "done", False: "partial", None: "skipped"}[checkpointed],
        )
        return report

    async def run(self) -> None:
        """Run nightly until cancelled. A failed run is logged and retried the next night."""
        while True:
            await self._clock.sleep(seconds_until(self._clock.now(), self._run_at, self._tz))
            try:
                await self.run_once()
            except Exception:
                logger.exception("maintenance run failed")
