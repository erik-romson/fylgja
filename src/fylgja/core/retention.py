"""Nightly maintenance: retention, ``PRAGMA optimize`` and the WAL checkpoint
(``PLAN-final-plan.md`` §4.1, A12; ``PLAN.md`` §5).

**Retention.**

* Raw ``events`` observed more than 30 days ago are deleted.
* ``focus_spans`` and the closed change intervals of ``windows_snapshot``, ``session_snapshots``
  and ``usage_snapshots`` that *ended* more than one year (365 days) ago are deleted. Open spans
  and intervals are current state and are never deleted.
* ``todos`` are kept forever; nothing else is deleted either.

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

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from typing import Final

from fylgja.collectors.base import Clock
from fylgja.core.writer import Tx, Writer
from fylgja.store import db, queries

__all__ = [
    "ARCHIVE_RETENTION",
    "DEFAULT_RUN_AT",
    "RAW_EVENT_RETENTION",
    "MaintenanceJob",
    "MaintenanceReport",
    "RetentionPolicy",
    "apply_retention",
    "seconds_until",
]

logger = logging.getLogger("fylgja.retention")

RAW_EVENT_RETENTION: Final = timedelta(days=30)
ARCHIVE_RETENTION: Final = timedelta(days=365)
DEFAULT_RUN_AT: Final = time(3, 30)


@dataclass(frozen=True)
class RetentionPolicy:
    raw_events: timedelta = RAW_EVENT_RETENTION
    archives: timedelta = ARCHIVE_RETENTION


@dataclass(frozen=True)
class MaintenanceReport:
    ran_at: datetime
    deleted: Mapping[str, int]
    optimized: bool
    checkpointed: bool | None
    """``True`` after a full checkpoint, ``False`` when readers kept it partial, ``None`` when it
    was skipped because the writer was busy."""


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
    ) -> None:
        self._writer = writer
        self._clock = clock
        self._policy = RetentionPolicy() if policy is None else policy
        self._run_at = run_at
        self._tz = tz
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
        await self._writer.execute(lambda tx: queries.optimize(tx.connection), transaction=False)
        checkpointed: bool | None = None
        if self._is_idle():
            checkpointed = await self._writer.execute(
                lambda tx: queries.checkpoint_truncate(tx.connection), transaction=False
            )
        report = MaintenanceReport(now, deleted, optimized=True, checkpointed=checkpointed)
        self.last_report = report
        logger.info(
            "maintenance: deleted %s; checkpoint %s",
            ", ".join(f"{table}={count}" for table, count in deleted.items()),
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
