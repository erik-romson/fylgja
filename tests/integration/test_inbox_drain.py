"""The hook inbox drain (WP2.2, A5): delete-after-commit, replay after a crash, a 10 000-file
backlog, quarantine, and the kqueue and polling watchers. The sink is the real ingest gate in
front of the real single writer and a real SQLite file."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from inbox_support import SESSION, make_envelope, write_envelope

from fylgja import hook, paths
from fylgja.collectors.base import RecordSink
from fylgja.collectors.claude import plugin
from fylgja.collectors.claude.adapter import LocalFileSystem, Seams
from fylgja.collectors.claude.collector import ClaudeCollector, ClaudeSettings
from fylgja.collectors.claude.inbox import InboxDrainer, kqueue_available
from fylgja.collectors.claude.statusline import StatuslineReader
from fylgja.collectors.context import build_context
from fylgja.core.ingest import IngestGate
from fylgja.core.writer import Writer
from fylgja.models import HealthState, Record
from fylgja.testing.fakes import FakeClock, FakeProcessRunner, RecordingSink

LOGGER = logging.LoggerAdapter(logging.getLogger("test.inbox"), {})
DAY_NS = 86_400 * 1_000_000_000


class Stack:
    """The real writer, ingest gate and a gate sink for plugin ``claude``."""

    def __init__(self, writer: Writer, gate: IngestGate) -> None:
        self.writer = writer
        self.gate = gate
        self.sink = gate.sink("claude")
        self.inbox = paths.inbox_dir("claude")

    def drainer(self, sink: RecordSink | None = None, **kwargs: Any) -> InboxDrainer:
        kwargs.setdefault("use_kqueue", False)
        return InboxDrainer(
            self.inbox, self.sink if sink is None else sink, SystemClockLike(), LOGGER, **kwargs
        )

    def rows(self) -> list[tuple[Any, ...]]:
        with closing(
            sqlite3.connect(f"file:{self.writer.database}?mode=ro", uri=True)
        ) as connection:
            return connection.execute(
                "SELECT id, dedup_key, entity_kind, entity_id, observed_at, payload "
                "FROM events ORDER BY id"
            ).fetchall()


class SystemClockLike:
    """The real clock (a small stand-in so the tests need no import of the collector's own)."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@pytest.fixture
async def stack() -> AsyncIterator[Stack]:
    writer = Writer(paths.database_file())
    await writer.start()
    gate = IngestGate(writer, [plugin])
    paths.inbox_dir("claude").mkdir(parents=True, mode=0o700)
    try:
        yield Stack(writer, gate)
    finally:
        await writer.stop()


def put(stack: Stack, event: str = "Stop", **kwargs: Any) -> Path:
    return write_envelope(stack.inbox, make_envelope(event, **kwargs))


def left(stack: Stack) -> list[str]:
    return sorted(p.name for p in stack.inbox.iterdir() if p.is_file())


class CrashError(BaseException):
    """A simulated process death: not an ``Exception``, so nothing in the drain catches it."""


class GatedSink:
    """Holds every commit until released, so a test can look at the inbox mid-commit."""

    def __init__(self, inner: RecordSink) -> None:
        self.inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def emit(self, record: Record) -> None:
        self.inner.emit(record)

    async def emit_committed(self, record: Record) -> None:
        self.entered.set()
        await self.release.wait()
        await self.inner.emit_committed(record)


# -- delete-after-commit ------------------------------------------------------------------------


async def test_the_file_stays_until_the_commit_has_resolved(stack: Stack) -> None:
    path = put(stack)
    gated = GatedSink(stack.sink)
    task = asyncio.create_task(stack.drainer(gated).drain())
    await asyncio.wait_for(gated.entered.wait(), 5)
    await asyncio.sleep(0.05)
    assert path.exists(), "the file went before the commit"
    assert stack.rows() == []
    gated.release.set()
    result = await asyncio.wait_for(task, 10)
    assert (result.committed, result.quarantined, result.failed) == (1, 0, 0)
    assert not path.exists()
    (row,) = stack.rows()
    assert row[2:4] == ("hook_event", f"claude:{SESSION}")


async def test_a_failed_commit_leaves_the_file_and_it_is_retried(stack: Stack) -> None:
    path = put(stack)
    broken = RecordingSink(commit_error=RuntimeError("database is locked"))
    drainer = stack.drainer(broken)
    result = await drainer.drain()
    assert (result.committed, result.failed) == (0, 1)
    assert path.exists()
    assert drainer.failure is not None
    assert drainer.failure[0] == "inbox_commit_failed"
    assert "database is locked" not in drainer.failure[1]

    broken.commit_error = None
    recovered = await drainer.drain()
    assert recovered.committed == 1
    assert not path.exists()
    assert drainer.failure is None


async def test_a_stopped_writer_keeps_the_files(stack: Stack) -> None:
    path = put(stack)
    await stack.writer.stop()
    drainer = stack.drainer()
    result = await drainer.drain()
    assert (result.committed, result.failed) == (0, 1)
    assert path.exists()
    assert drainer.failure is not None


# -- crash between commit and unlink --------------------------------------------------------------


async def test_a_crash_between_commit_and_unlink_is_replayed_without_duplicates(
    stack: Stack,
) -> None:
    first = put(stack, "UserPromptSubmit", payload={"session_id": SESSION, "prompt": "hello"})
    second = put(stack, "Stop")

    def die(path: Path) -> None:
        raise CrashError

    with pytest.raises(CrashError):
        await stack.drainer(unlink=die).drain()
    assert first.exists(), "a committed file survives the crash"
    assert second.exists()
    committed = stack.rows()
    assert len(committed) == 2

    replay = stack.drainer()
    result = await replay.drain()
    assert result.committed == 2
    assert left(stack) == []
    assert stack.rows() == committed, "the replay changed the store"
    assert stack.writer.stats.duplicates == 2


async def test_a_file_that_cannot_be_unlinked_is_counted_and_replayed_harmlessly(
    stack: Stack,
) -> None:
    path = put(stack)

    def refuse(target: Path) -> None:
        raise PermissionError

    drainer = stack.drainer(unlink=refuse)
    result = await drainer.drain()
    assert (result.committed, result.failed) == (0, 1)
    assert drainer.failure is not None
    assert drainer.failure[0] == "inbox_unlink_failed"
    assert path.exists()
    again = await stack.drainer().drain()
    assert again.committed == 1
    assert len(stack.rows()) == 1


async def test_a_file_removed_by_someone_else_is_not_an_error(stack: Stack) -> None:
    path = put(stack)

    def already_gone(target: Path) -> None:
        os.unlink(target)
        os.unlink(target)

    result = await stack.drainer(unlink=already_gone).drain()
    assert not path.exists()
    assert (result.committed, result.failed) == (1, 0)


# -- order and idempotence ------------------------------------------------------------------------


async def test_files_are_committed_in_name_order(stack: Stack) -> None:
    base = time.time_ns() - 10_000_000_000
    for offset in (5, 1, 3, 2, 4):
        put(stack, "PreToolUse", received_at_ns=base + offset * 1_000_000_000)
    await stack.drainer(batch_size=2).drain()
    times = [row[4] for row in stack.rows()]
    assert times == sorted(times)
    assert len(times) == 5


async def test_the_same_envelope_twice_is_stored_once(stack: Stack) -> None:
    envelope = make_envelope("Stop")
    write_envelope(stack.inbox, envelope)
    await stack.drainer().drain()
    write_envelope(stack.inbox, envelope)  # the same file again, as a replay would find it
    await stack.drainer().drain()
    assert len(stack.rows()) == 1
    assert left(stack) == []


async def test_the_stored_event_holds_the_minimized_fields_only(stack: Stack) -> None:
    secret = "tool-body-secret-4417"
    put(
        stack,
        "PostToolUse",
        payload={
            "session_id": SESSION,
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "cwd": "/w",
            "tool_input": {"file_path": "/w/a.py", "new_string": secret},
            "tool_response": {"output": secret},
            "transcript_path": f"/x/{secret}.jsonl",
        },
    )
    await stack.drainer().drain()
    (row,) = stack.rows()
    payload = json.loads(row[5])
    assert payload["edited_paths"] == ["/w/a.py"]
    assert payload["tool_name"] == "Edit"
    assert secret not in row[5]


# -- quarantine -----------------------------------------------------------------------------------


async def test_unusable_files_are_quarantined_and_the_rest_is_drained(stack: Stack) -> None:
    good = put(stack)
    not_json = stack.inbox / f"{time.time_ns()}-{'a' * 36}.json"
    not_json.write_text("{ torn")
    wrong_schema = stack.inbox / f"{time.time_ns()}-{'b' * 36}.json"
    wrong_schema.write_text(json.dumps({**make_envelope("Stop"), "schema": "fylgja.hook/9"}))
    unattributable = write_envelope(stack.inbox, make_envelope("Stop", "{}", environ={}))
    too_old = put(stack, "Stop", received_at_ns=time.time_ns() - 60 * DAY_NS)
    wrong_inbox = write_envelope(stack.inbox, make_envelope("Stop", source="codex"))
    oversize = stack.inbox / f"{time.time_ns()}-{'c' * 36}.json"
    oversize.touch()
    os.truncate(oversize, 33 * 1024 * 1024)
    (stack.inbox / "elsewhere").write_text("{}")
    link = stack.inbox / f"{time.time_ns()}-{'d' * 36}.json"
    link.symlink_to(good)
    untouched = {
        ".backlog-stamp": "ok\n",
        "dropped.count": "3\n",
        "README.json": "{}",
        f"{time.time_ns()}-short.json": "{}",
    }
    for name, text in untouched.items():
        (stack.inbox / name).write_text(text)

    drainer = stack.drainer()
    result = await drainer.drain()

    assert result.committed == 1
    assert result.quarantined == 7
    assert result.failed == 0
    quarantine = stack.inbox / "quarantine"
    assert (quarantine.stat().st_mode & 0o777) == 0o700
    moved = sorted(p.name for p in quarantine.iterdir())
    assert moved == sorted(
        p.name
        for p in (not_json, wrong_schema, unattributable, too_old, wrong_inbox, oversize, link)
    )
    assert good.exists() is False
    assert not any((stack.inbox / n).exists() is False for n in untouched)
    assert len(stack.rows()) == 1
    assert drainer.failure is None
    # A second pass finds nothing left to do.
    assert (await drainer.drain()).handled == 0


async def test_a_directory_with_an_envelope_name_is_quarantined_not_followed(
    stack: Stack,
) -> None:
    trap = stack.inbox / f"{time.time_ns()}-{'e' * 36}.json"
    trap.mkdir()
    (trap / "inner").write_text("x")
    result = await stack.drainer().drain()
    assert result.quarantined == 1
    assert (stack.inbox / "quarantine" / trap.name / "inner").exists()


async def test_a_quarantine_that_cannot_be_written_leaves_the_file_and_reports_it(
    stack: Stack,
) -> None:
    bad = stack.inbox / f"{time.time_ns()}-{'f' * 36}.json"
    bad.write_text("nope")

    def refuse(source: Path, target: Path) -> None:
        raise PermissionError

    drainer = stack.drainer(rename=refuse)
    result = await drainer.drain()
    assert (result.quarantined, result.failed) == (0, 1)
    assert bad.exists()
    assert drainer.failure is not None
    assert drainer.failure[0] == "inbox_quarantine_failed"


# -- the backlog ----------------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.timeout(120)
async def test_a_backlog_of_10000_files_is_drained_within_30_seconds(stack: Stack) -> None:
    base = time.time_ns() - 60_000_000_000
    for index in range(10_000):
        envelope = make_envelope(
            "PreToolUse",
            payload={
                "session_id": f"{SESSION[:-3]}{index % 11:03d}",
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "x" * 200},
            },
            received_at_ns=base + index * 1000,
        )
        write_envelope(stack.inbox, envelope)
    assert len(left(stack)) == 10_000

    started = time.monotonic()
    result = await asyncio.wait_for(stack.drainer().drain(), 60)
    elapsed = time.monotonic() - started

    assert elapsed < 30, f"the drain took {elapsed:.1f} s"
    assert (result.committed, result.quarantined, result.failed) == (10_000, 0, 0)
    assert left(stack) == []
    assert len(stack.rows()) == 10_000
    assert stack.writer.stats.transactions < 1_000, "commits were not grouped"


# -- housekeeping ---------------------------------------------------------------------------------


async def test_stale_tmp_files_are_swept_and_fresh_ones_are_left(stack: Stack) -> None:
    clock = FakeClock(datetime.now(UTC))
    stale = stack.inbox / ".tmp-stale"
    fresh = stack.inbox / ".tmp-fresh"
    stale.write_text("x")
    fresh.write_text("x")
    now = time.time()
    os.utime(stale, (now - 120, now - 120))
    os.utime(fresh, (now - 5, now - 5))
    drainer = InboxDrainer(stack.inbox, stack.sink, clock, LOGGER, use_kqueue=False)
    await drainer.drain()
    assert not stale.exists()
    assert fresh.exists()


async def test_a_missing_inbox_is_normal_and_a_file_in_its_place_is_a_failure(
    stack: Stack, tmp_path: Path
) -> None:
    missing = InboxDrainer(
        tmp_path / "none", stack.sink, SystemClockLike(), LOGGER, use_kqueue=False
    )
    assert (await missing.drain()).handled == 0
    assert missing.failure is None

    blocker = tmp_path / "blocked"
    blocker.write_text("x")
    blocked = InboxDrainer(blocker, stack.sink, SystemClockLike(), LOGGER, use_kqueue=False)
    await blocked.drain()
    assert blocked.failure is not None
    assert blocked.failure[0] == "inbox_not_a_directory"
    blocker.unlink()
    blocker.mkdir()
    await blocked.drain()
    assert blocked.failure is None


async def test_the_batch_size_must_be_positive(stack: Stack) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        stack.drainer(batch_size=0)


# -- the watchers ---------------------------------------------------------------------------------


async def wait_until(condition: Any, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def test_the_polling_watcher_drains_on_each_interval(stack: Stack) -> None:
    clock = FakeClock(datetime.now(UTC))
    drainer = InboxDrainer(
        stack.inbox, stack.sink, clock, LOGGER, use_kqueue=False, poll_interval=1.0
    )
    task = asyncio.create_task(drainer.run())
    try:
        await clock.advance(0.0)
        await wait_until(lambda: drainer.passes >= 1)
        path = put(stack)
        assert path.exists()
        for _ in range(3):
            await clock.advance(1.0)
            if not path.exists():
                break
            await asyncio.sleep(0.05)
        await wait_until(lambda: not path.exists())
        assert drainer.watching is False
        assert len(stack.rows()) == 1
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=5)
    assert clock.pending == 0


async def test_the_polling_watcher_works_with_the_real_clock(stack: Stack) -> None:
    drainer = stack.drainer(poll_interval=0.05)
    task = asyncio.create_task(drainer.run())
    try:
        path = put(stack)
        await wait_until(lambda: not path.exists())
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=5)
    assert len(stack.rows()) == 1


async def test_a_directory_that_appears_later_is_picked_up(stack: Stack, tmp_path: Path) -> None:
    late = tmp_path / "later" / "inbox"
    drainer = InboxDrainer(
        late, stack.sink, SystemClockLike(), LOGGER, use_kqueue=False, poll_interval=0.05
    )
    task = asyncio.create_task(drainer.run())
    try:
        await asyncio.sleep(0.15)
        path = write_envelope(late, make_envelope("Stop"))
        await wait_until(lambda: not path.exists())
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=5)


@pytest.mark.skipif(sys.platform != "darwin", reason="kqueue directory watch is macOS-only")
async def test_the_kqueue_watcher_wakes_the_drain_long_before_the_poll(stack: Stack) -> None:
    assert kqueue_available()
    drainer = stack.drainer(use_kqueue=True, poll_interval=60.0)
    task = asyncio.create_task(drainer.run())
    try:
        await wait_until(lambda: drainer.passes >= 1 and drainer.watching)
        started = time.monotonic()
        path = put(stack)
        await wait_until(lambda: not path.exists(), seconds=5)
        assert time.monotonic() - started < 5, "the watch did not wake the drain"
        assert drainer.passes >= 2
        second = put(stack, "Stop")
        await wait_until(lambda: not second.exists(), seconds=5)
        assert len(stack.rows()) == 2
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=5)
    assert drainer._watch is None, "the watch was not closed on cancellation"


@pytest.mark.skipif(sys.platform != "darwin", reason="kqueue directory watch is macOS-only")
async def test_the_kqueue_watcher_follows_a_replaced_directory(stack: Stack) -> None:
    drainer = stack.drainer(use_kqueue=True, poll_interval=60.0)
    task = asyncio.create_task(drainer.run())
    try:
        await wait_until(lambda: drainer.watching)
        moved = stack.inbox.with_name("claude-old")
        stack.inbox.rename(moved)
        stack.inbox.mkdir(mode=0o700)
        await wait_until(lambda: drainer._watch is None or drainer.passes >= 2)
        # The poll interval is a minute, so only a watch on the *new* directory can see this file.
        await asyncio.sleep(0.2)
        await drainer.drain()  # whatever the loop is doing, the inbox is usable again
        path = put(stack)
        await wait_until(lambda: not path.exists(), seconds=10)
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=5)


async def test_a_platform_without_kqueue_falls_back_to_polling(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fylgja.collectors.claude.inbox as inbox_module

    monkeypatch.setattr(inbox_module, "kqueue_available", lambda: False)
    drainer = InboxDrainer(stack.inbox, stack.sink, SystemClockLike(), LOGGER, poll_interval=0.05)
    task = asyncio.create_task(drainer.run())
    try:
        path = put(stack)
        await wait_until(lambda: not path.exists())
        assert drainer.watching is False
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=5)


async def test_a_watch_that_cannot_be_set_up_is_given_up_quietly(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fylgja.collectors.claude.inbox as inbox_module

    class Refusing:
        def __init__(self, directory: Path) -> None:
            raise OSError("no kqueue for you")

    monkeypatch.setattr(inbox_module, "_Watch", Refusing)
    drainer = stack.drainer(use_kqueue=True, poll_interval=0.05)
    task = asyncio.create_task(drainer.run())
    try:
        path = put(stack)
        await wait_until(lambda: not path.exists())
        assert drainer.watching is False
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=5)


# -- statusline readings through the real gate ----------------------------------------------------


async def test_statusline_readings_pass_the_ingest_gate_into_events_and_usage(stack: Stack) -> None:
    snapshot = make_envelope(
        "StatusLine",
        {
            "session_id": SESSION,
            "cost": {"total_cost_usd": 2.5},
            "context_window": {"used_percentage": 31},
            "rate_limits": {
                "five_hour": {"used_percentage": 20.0, "resets_at": time.time() + 3600},
                "seven_day": {"used_percentage": 55.5, "resets_at": time.time() + 86400},
            },
        },
    )
    directory = paths.statusline_dir()
    directory.mkdir(parents=True, mode=0o700)
    (directory / f"{SESSION}.json").write_bytes(hook.encode_envelope(snapshot))
    reader = StatuslineReader(directory, stack.sink, SystemClockLike(), LOGGER)
    assert reader.poll() == 1
    await stack.writer.flush()

    assert stack.gate.stats("claude").rejected_total == 0
    (row,) = stack.rows()
    assert row[2:4] == ("hook_event", f"claude:{SESSION}")
    payload = json.loads(row[5])
    assert (payload["event"], payload["cost_usd"], payload["context_pct"]) == (
        "StatusLine",
        2.5,
        31,
    )
    with closing(sqlite3.connect(f"file:{stack.writer.database}?mode=ro", uri=True)) as connection:
        usage = connection.execute(
            "SELECT provider, window, used_pct, source FROM usage_snapshots "
            "WHERE valid_to IS NULL ORDER BY window"
        ).fetchall()
    assert usage == [
        ("claude", "five_hour", 20.0, "statusline"),
        ("claude", "seven_day", 55.5, "statusline"),
    ]


# -- the collector runs all three loops -----------------------------------------------------------


def _collector(sink: RecordSink, tmp_path: Path) -> ClaudeCollector:
    registry = tmp_path / "registry"
    registry.mkdir()
    settings = ClaudeSettings(
        registry_dir=registry,
        inbox_poll_seconds=0.05,
        statusline_poll_seconds=0.05,
        poll_interval_seconds=0.05,
    )
    context = build_context("claude", sink)
    return ClaudeCollector(
        settings,
        context,
        registry_dir=registry,
        seams=Seams(FakeProcessRunner(), LocalFileSystem()),
        ignore_cwd=(),
    )


async def test_the_collector_drains_the_inbox_and_reads_the_statusline_while_it_runs(
    stack: Stack, tmp_path: Path
) -> None:
    collector = _collector(stack.sink, tmp_path)
    task = asyncio.create_task(collector.run())
    try:
        path = put(stack)
        directory = paths.statusline_dir()
        directory.mkdir(parents=True, mode=0o700)
        snapshot = make_envelope(
            "StatusLine", {"session_id": SESSION, "cost": {"total_cost_usd": 1}}
        )
        (directory / f"{SESSION}.json").write_bytes(hook.encode_envelope(snapshot))
        await wait_until(lambda: not path.exists() and len(stack.rows()) == 2)
        report = await collector.doctor()
        assert report.state is HealthState.OK
        assert "inbox: 1 drained, 0 quarantined" in (report.detail or "")
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=5)
    assert task.cancelled() or task.exception() is None


async def test_a_collector_whose_inbox_cannot_commit_reports_degraded_and_keeps_the_file(
    tmp_path: Path,
) -> None:
    inbox = paths.inbox_dir("claude")
    path = write_envelope(inbox, make_envelope("Stop"))
    broken = RecordingSink(commit_error=RuntimeError("disk full"))
    collector = _collector(broken, tmp_path)
    task = asyncio.create_task(collector.run())
    try:
        await wait_until(lambda: collector.inbox.failure is not None)
        report = await collector.doctor()
        assert report.state is HealthState.DEGRADED
        assert report.error_kind == "inbox_commit_failed"
        assert path.exists()
        broken.commit_error = None
        await wait_until(lambda: not path.exists())
        await wait_until(lambda: collector.inbox.failure is None)
        assert (await collector.doctor()).state is HealthState.OK
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=5)


async def test_a_crashing_loop_is_logged_and_restarted_without_stopping_the_others(
    stack: Stack, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    collector = _collector(stack.sink, tmp_path)
    calls = 0

    def explode() -> int:
        nonlocal calls
        calls += 1
        raise RuntimeError("statusline bug")

    collector.statusline.poll = explode  # type: ignore[method-assign]
    task = asyncio.create_task(collector.run())
    try:
        path = put(stack)
        await wait_until(lambda: not path.exists() and calls >= 2)
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=5)
    errors = [r.getMessage() for r in caplog.records if "statusline loop failed" in r.getMessage()]
    assert errors == ["claude statusline loop failed: RuntimeError"], "logged once, kind only"
