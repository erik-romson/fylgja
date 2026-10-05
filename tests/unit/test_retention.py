"""Nightly maintenance: retention boundaries exactly at 30 days and one year, optimize and the WAL
checkpoint, and the schedule under a fake clock (§4.1, A12)."""

from __future__ import annotations

import asyncio
import heapq
import itertools
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from fylgja import paths
from fylgja.core.retention import (
    ARCHIVE_RETENTION,
    QUARANTINE_RETENTION,
    RAW_EVENT_RETENTION,
    MaintenanceJob,
    RetentionPolicy,
    apply_retention,
    prune_quarantine,
    seconds_until,
)
from fylgja.core.writer import Tx, Writer
from fylgja.store import db

pytestmark = pytest.mark.timeout(20)

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
TICK = timedelta(microseconds=1)


class FakeClock:
    """Virtual time: ``sleep`` returns when ``advance`` passes its deadline."""

    def __init__(self, start: datetime) -> None:
        self._origin = start
        self._mono = 0.0
        self._waiters: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    def now(self) -> datetime:
        return self._origin + timedelta(seconds=self._mono)

    def monotonic(self) -> float:
        return self._mono

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (self._mono + seconds, next(self._seq), future))
        await future

    async def advance(self, seconds: float) -> None:
        target = self._mono + seconds
        for _ in range(50):
            await asyncio.sleep(0)
        while self._waiters and self._waiters[0][0] <= target:
            deadline, _, future = heapq.heappop(self._waiters)
            self._mono = deadline
            if not future.done():
                future.set_result(None)
            for _ in range(50):
                await asyncio.sleep(0)
        self._mono = target


@pytest.fixture
async def writer() -> AsyncIterator[Writer]:
    instance = Writer(paths.database_file(), clock=FakeClock(NOW))
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


def t(value: datetime) -> str:
    return db.to_db_time(value)


def seed(tx: Tx) -> None:
    """Rows on both sides of every boundary, plus rows that must survive at any age."""
    c = tx.connection
    raw_cut = NOW - RAW_EVENT_RETENTION
    archive_cut = NOW - ARCHIVE_RETENTION
    ancient = NOW - timedelta(days=1000)
    for key, observed in (
        ("keep-boundary", raw_cut),
        ("drop-boundary", raw_cut - TICK),
        ("keep-recent", NOW - timedelta(days=1)),
        ("drop-ancient", ancient),
    ):
        c.execute(
            "INSERT INTO events (dedup_key, source, record_type, entity_kind, entity_id, "
            "observed_at, received_at, payload) VALUES (?, 's', 'event', 'focus', 'e', ?, ?, '{}')",
            (key, t(observed), t(observed)),
        )
    c.execute(
        "INSERT INTO sessions (session_key, kind, native_id, source, first_seen, last_seen) "
        "VALUES ('claude:s', 'claude', 's', 'claude', ?, ?)",
        (t(ancient), t(ancient)),
    )
    for label, ended in (("keep", archive_cut), ("drop", archive_cut - TICK), ("open", None)):
        start = t(ancient)
        end = None if ended is None else t(ended)
        c.execute(
            "INSERT INTO focus_spans (window_id, started_at, ended_at) VALUES (?, ?, ?)",
            (label, start, end),
        )
        c.execute(
            "INSERT INTO windows_snapshot (source, window_id, valid_from, valid_to) "
            "VALUES ('aero', ?, ?, ?)",
            (label, start, end),
        )
        c.execute(
            "INSERT INTO session_snapshots (session_key, source, valid_from, valid_to, state) "
            "VALUES ('claude:s', 'claude', ?, ?, ?)",
            (start, end, label),
        )
        c.execute(
            "INSERT INTO usage_snapshots (collector, provider, window, source, valid_from, "
            "valid_to) VALUES ('claude', 'claude', ?, 'statusline', ?, ?)",
            (label, start, end),
        )
    c.execute(
        "INSERT INTO todos (id, text, status, created_at, done_at, source, updated_at) "
        "VALUES ('old', 'kept forever', 'done', ?, ?, 'manual', ?)",
        (t(ancient), t(ancient), t(ancient)),
    )


def survivors(tx: Tx) -> dict[str, list[str]]:
    c = tx.connection
    return {
        "events": [r[0] for r in c.execute("SELECT dedup_key FROM events ORDER BY dedup_key")],
        "focus_spans": [r[0] for r in c.execute("SELECT window_id FROM focus_spans ORDER BY 1")],
        "windows_snapshot": [
            r[0] for r in c.execute("SELECT window_id FROM windows_snapshot ORDER BY 1")
        ],
        "session_snapshots": [
            r[0] for r in c.execute("SELECT state FROM session_snapshots ORDER BY 1")
        ],
        "usage_snapshots": [
            r[0] for r in c.execute("SELECT window FROM usage_snapshots ORDER BY 1")
        ],
        "todos": [r[0] for r in c.execute("SELECT id FROM todos")],
        "sessions": [r[0] for r in c.execute("SELECT session_key FROM sessions")],
    }


async def test_boundaries_at_exactly_30_days_and_one_year(writer: Writer) -> None:
    await writer.execute(seed)
    job = MaintenanceJob(writer, clock=FakeClock(NOW))
    report = await job.run_once()
    assert report.ran_at == NOW
    assert dict(report.deleted) == {
        "events": 2,
        "focus_spans": 1,
        "windows_snapshot": 1,
        "session_snapshots": 1,
        "usage_snapshots": 1,
    }
    assert await writer.execute(survivors) == {
        "events": ["keep-boundary", "keep-recent"],
        "focus_spans": ["keep", "open"],
        "windows_snapshot": ["keep", "open"],
        "session_snapshots": ["keep", "open"],
        "usage_snapshots": ["keep", "open"],
        "todos": ["old"],
        "sessions": ["claude:s"],
    }
    assert report.optimized
    assert report.checkpointed is True
    assert job.last_report is report
    again = await job.run_once()
    assert sum(again.deleted.values()) == 0


async def test_one_microsecond_later_the_boundary_rows_go(writer: Writer) -> None:
    await writer.execute(seed)
    later = MaintenanceJob(writer, clock=FakeClock(NOW + TICK))
    report = await later.run_once()
    assert report.deleted["events"] == 3
    assert report.deleted["focus_spans"] == 2
    assert report.deleted["session_snapshots"] == 2
    remaining = await writer.execute(survivors)
    assert remaining["events"] == ["keep-recent"]
    assert remaining["usage_snapshots"] == ["open"]
    assert remaining["todos"] == ["old"]


async def test_checkpoint_is_skipped_while_busy(writer: Writer) -> None:
    job = MaintenanceJob(writer, clock=FakeClock(NOW), is_idle=lambda: False)
    report = await job.run_once()
    assert report.checkpointed is None


async def test_a_custom_policy(writer: Writer) -> None:
    await writer.execute(seed)
    policy = RetentionPolicy(raw_events=timedelta(days=2), archives=timedelta(days=10_000))
    deleted = await writer.execute(lambda tx: apply_retention(tx.connection, NOW, policy))
    assert deleted["events"] == 3
    assert deleted["focus_spans"] == 0


def test_seconds_until_the_next_run() -> None:
    at = time(3, 30)
    assert seconds_until(datetime(2026, 10, 5, 1, 0, tzinfo=UTC), at, UTC) == 2.5 * 3600
    assert seconds_until(datetime(2026, 10, 5, 3, 30, tzinfo=UTC), at, UTC) == 24 * 3600
    assert seconds_until(datetime(2026, 10, 5, 4, 0, tzinfo=UTC), at, UTC) == 23.5 * 3600
    oslo = timezone(timedelta(hours=2))
    assert seconds_until(datetime(2026, 10, 5, 1, 0, tzinfo=UTC), at, oslo) == 0.5 * 3600
    assert seconds_until(datetime(2026, 10, 5, 1, 0, tzinfo=UTC), at) > 0  # system zone


async def test_the_job_runs_nightly_on_the_injected_clock(writer: Writer) -> None:
    clock = FakeClock(datetime(2026, 10, 5, 1, 0, tzinfo=UTC))
    job = MaintenanceJob(writer, clock=clock, run_at=time(3, 30), tz=UTC)
    runs: list[datetime] = []
    finished = asyncio.Event()
    original = job.run_once
    failures = iter([True])

    async def counted() -> object:
        runs.append(clock.now())
        try:
            if next(failures, False):
                raise RuntimeError("first night fails")
            return await original()
        finally:
            finished.set()

    job.run_once = counted  # type: ignore[method-assign]
    task = asyncio.create_task(job.run())
    try:
        await clock.advance(2.5 * 3600 - 1)
        assert runs == []
        await clock.advance(1)
        await asyncio.wait_for(finished.wait(), 5)
        finished.clear()
        assert runs == [datetime(2026, 10, 5, 3, 30, tzinfo=UTC)]
        assert job.last_report is None  # the first run failed and was logged
        await clock.advance(24 * 3600)
        await asyncio.wait_for(finished.wait(), 5)
        assert runs[1] == datetime(2026, 10, 6, 3, 30, tzinfo=UTC)
        assert job.last_report is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# -- quarantined hook-inbox files (WP2.2): older than 7 days go -----------------------------------


def _stamp(path: Path, mtime: datetime) -> Path:
    """Give ``path`` an exact mtime (microsecond precision, as the boundary tests need)."""
    nanoseconds = (mtime - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1) * 1000
    os.utime(path, ns=(nanoseconds, nanoseconds), follow_symlinks=False)
    return path


def _quarantined(source: str, name: str, mtime: datetime) -> Path:
    directory = paths.quarantine_dir(source)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("{}")
    return _stamp(path, mtime)


def _names(source: str) -> list[str]:
    return sorted(p.name for p in paths.quarantine_dir(source).iterdir())


def test_the_quarantine_retention_is_seven_days() -> None:
    assert QUARANTINE_RETENTION == timedelta(days=7)
    assert RetentionPolicy().quarantine == timedelta(days=7)


def test_a_quarantined_file_is_deleted_only_when_older_than_seven_days() -> None:
    cut = NOW - QUARANTINE_RETENTION
    _quarantined("claude", "keep-boundary", cut)
    _quarantined("claude", "drop-boundary", cut - TICK)
    _quarantined("claude", "keep-recent", NOW - timedelta(days=1))
    _quarantined("claude", "drop-ancient", NOW - timedelta(days=400))
    _quarantined("codex", "drop-other-source", cut - timedelta(hours=1))
    _quarantined("codex", "keep-other-source", cut + timedelta(hours=1))

    assert prune_quarantine(paths.hook_inbox_root(), NOW) == 3
    assert _names("claude") == ["keep-boundary", "keep-recent"]
    assert _names("codex") == ["keep-other-source"]
    assert prune_quarantine(paths.hook_inbox_root(), NOW) == 0

    # One microsecond later the boundary file is older than seven days too.
    assert prune_quarantine(paths.hook_inbox_root(), NOW + TICK) == 1
    assert _names("claude") == ["keep-recent"]


def test_only_regular_files_in_a_real_quarantine_directory_are_touched() -> None:
    old = NOW - timedelta(days=30)
    inbox = paths.inbox_dir("claude")
    spool = inbox / "1791231759255399000-6f1e2d3c-4b5a-4c6d-8e7f-0a1b2c3d4e5f.json"
    _quarantined("claude", "kept-quarantine-dir", NOW)  # makes the directory
    spool.write_text("{}")
    _stamp(spool, old)  # an old spool file is not the quarantine's business
    nested = paths.quarantine_dir("claude") / "a-directory"
    nested.mkdir()
    (nested / "inner").write_text("x")
    _stamp(nested / "inner", old)
    link = paths.quarantine_dir("claude") / "a-link"
    link.symlink_to(spool)
    _stamp(link, old)

    elsewhere = paths.data_dir() / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / "victim"
    victim.write_text("x")
    _stamp(victim, old)
    linked_source = paths.hook_inbox_root() / "linked"
    linked_source.symlink_to(elsewhere)
    (elsewhere / "quarantine").mkdir()
    _stamp(_quarantined_in(elsewhere / "quarantine", "old-behind-a-link", old), old)

    assert prune_quarantine(paths.hook_inbox_root(), NOW) == 0
    assert spool.exists()
    assert (nested / "inner").exists()
    assert link.is_symlink()
    assert victim.exists()
    assert (elsewhere / "quarantine" / "old-behind-a-link").exists()


def _quarantined_in(directory: Path, name: str, mtime: datetime) -> Path:
    path = directory / name
    path.write_text("{}")
    return _stamp(path, mtime)


def test_a_missing_inbox_root_deletes_nothing() -> None:
    assert not paths.hook_inbox_root().exists()
    assert prune_quarantine(paths.hook_inbox_root(), NOW) == 0


async def test_the_maintenance_job_prunes_the_quarantine_and_reports_it(writer: Writer) -> None:
    cut = NOW - QUARANTINE_RETENTION
    _quarantined("claude", "keep", cut)
    _quarantined("claude", "drop", cut - TICK)
    report = await MaintenanceJob(writer, clock=FakeClock(NOW)).run_once()
    assert report.quarantine_deleted == 1
    assert _names("claude") == ["keep"]
    later = await MaintenanceJob(writer, clock=FakeClock(NOW + TICK)).run_once()
    assert later.quarantine_deleted == 1
    assert _names("claude") == []


async def test_a_custom_policy_and_inbox_root_are_honoured(writer: Writer, tmp_path: Path) -> None:
    root = tmp_path / "other-inbox-root"
    directory = root / "claude" / "quarantine"
    directory.mkdir(parents=True)
    young = _quarantined_in(directory, "two-days-old", NOW - timedelta(days=2))
    policy = RetentionPolicy(quarantine=timedelta(days=1))
    job = MaintenanceJob(writer, clock=FakeClock(NOW), policy=policy, inbox_root=root)
    assert (await job.run_once()).quarantine_deleted == 1
    assert not young.exists()
