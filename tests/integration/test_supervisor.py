"""Supervisor: crash isolation, backoff, health, cancellation and shutdown (WP1.3).

Time comes from ``FakeClock`` and jitter from a seeded ``random.Random``. Every test has an
explicit timeout so a hang fails instead of blocking.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import os
import random
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from fylgja.collectors.base import Capability, CollectorContext, CollectorPlugin
from fylgja.collectors.registry import DiscoveredEntry, RegisteredCollector, list_collectors
from fylgja.collectors.supervisor import (
    Backoff,
    BackoffPolicy,
    CollectorHealth,
    Resources,
    Supervisor,
    close_sqlite,
    stop_watcher,
    terminate_process_group,
)
from fylgja.config import load_config
from fylgja.models import HealthReport, HealthState, Record, WindowEvent, WindowPayload

TIMEOUT = pytest.mark.timeout(20)
SEED = 20260101


# --------------------------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------------------------


async def settle(rounds: int = 100) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


class FakeClock:
    """Virtual time: ``sleep`` blocks until ``advance`` reaches its deadline."""

    def __init__(self) -> None:
        self._mono = 0.0
        self._origin = datetime(2026, 1, 1, tzinfo=UTC)
        self._waiters: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    def now(self) -> datetime:
        return self._origin + timedelta(seconds=self._mono)

    def monotonic(self) -> float:
        return self._mono

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (self._mono + seconds, next(self._seq), future))
        await future

    async def advance(self, seconds: float) -> None:
        await self.advance_to(self._mono + seconds)

    async def advance_to(self, target: float) -> None:
        await settle()
        while self._waiters and self._waiters[0][0] <= target:
            deadline, _, future = heapq.heappop(self._waiters)
            self._mono = max(self._mono, deadline)
            if not future.done():
                future.set_result(None)
            await settle()
        self._mono = target
        await settle()


class ListSink:
    def __init__(self) -> None:
        self.records: list[Record] = []

    def emit(self, record: Record) -> None:
        self.records.append(record)

    async def emit_committed(self, record: Record) -> None:
        self.records.append(record)

    def count(self, source: str) -> int:
        return sum(1 for r in self.records if r.source == source)


class EmptyConfig(BaseModel):
    pass


class _EntryPoint:
    name = "x"
    value = "x:y"
    dist = None

    def load(self) -> object:
        raise AssertionError("never loaded")


class FakeCollector:
    def __init__(
        self,
        run: Callable[[], Awaitable[None]],
        doctor: Callable[[], Awaitable[HealthReport]] | None = None,
    ) -> None:
        self._run = run
        self._doctor = doctor

    async def run(self) -> None:
        await self._run()

    async def doctor(self) -> HealthReport:
        if self._doctor is not None:
            return await self._doctor()
        return report(HealthState.OK)


def report(state: HealthState, kind: str | None = None) -> HealthReport:
    return HealthReport(state=state, error_kind=kind, detail=None, checked_at=datetime.now(UTC))


Factory = Callable[[CollectorContext], FakeCollector]


def registered(name: str, factory: Factory, *, enabled: bool = True) -> RegisteredCollector:
    plugin = CollectorPlugin(
        name=name,
        api_major=1,
        capabilities=frozenset({Capability.WINDOWS}),
        config_model=EmptyConfig,
        factory=lambda settings, context: factory(context),
    )
    entry = DiscoveredEntry(name, "x:y", "fylgja-fake", "0", _EntryPoint())
    return RegisteredCollector(entry, enabled=enabled, plugin=plugin, settings=EmptyConfig())


def emit_one(context: CollectorContext, source: str) -> None:
    context.sink.emit(
        WindowEvent(
            source=source,
            entity_id="w1",
            observed_at=context.clock.now(),
            payload=WindowPayload(),
        )
    )


def emitter(name: str, period: float = 1.0) -> RegisteredCollector:
    def factory(context: CollectorContext) -> FakeCollector:
        async def run() -> None:
            while True:
                emit_one(context, name)
                await context.clock.sleep(period)

        return FakeCollector(run)

    return registered(name, factory)


def crasher(name: str) -> RegisteredCollector:
    def factory(context: CollectorContext) -> FakeCollector:
        async def run() -> None:
            raise RuntimeError("boom")

        return FakeCollector(run)

    return registered(name, factory)


def lifetimes(name: str, plan: list[float], starts: list[float]) -> RegisteredCollector:
    """A collector whose n-th run lives ``plan[n]`` seconds and then crashes (the last repeats)."""
    runs = itertools.count()

    def factory(context: CollectorContext) -> FakeCollector:
        index = min(next(runs), len(plan) - 1)

        async def run() -> None:
            starts.append(context.clock.monotonic())
            if plan[index] > 0:
                await context.clock.sleep(plan[index])
            raise RuntimeError("boom")

        return FakeCollector(run)

    return registered(name, factory)


def expected_delays(count: int, policy: BackoffPolicy | None = None) -> list[float]:
    """The schedule, recomputed independently of ``Backoff``."""
    policy = policy or BackoffPolicy()
    rng = random.Random(SEED)
    out = []
    for attempt in range(count):
        base = min(policy.maximum, policy.initial * policy.factor**attempt)
        out.append(min(policy.maximum, base * (1 + rng.uniform(-policy.jitter, policy.jitter))))
    return out


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def wait_dead(*pids: int, limit: float = 5.0) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if not any(alive(p) for p in pids):
            return True
        await asyncio.sleep(0.02)
    return False


def build(
    items: list[RegisteredCollector],
    clock: FakeClock,
    sink: ListSink | None = None,
    **kwargs: object,
) -> Supervisor:
    return Supervisor(
        items,
        sink or ListSink(),
        clock=clock,
        rng=random.Random(SEED),
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------------------------
# Backoff
# --------------------------------------------------------------------------------------------


@TIMEOUT
def test_backoff_schedule_doubles_to_the_cap_with_bounded_jitter() -> None:
    backoff = Backoff(BackoffPolicy(), random.Random(SEED))
    delays = [backoff.next_delay() for _ in range(12)]
    assert delays == expected_delays(12)
    for attempt, delay in enumerate(delays):
        base = min(300.0, 2.0**attempt)
        assert 0.8 * base - 1e-9 <= delay <= min(300.0, 1.2 * base) + 1e-9
    assert max(delays) <= 300.0
    assert all(d >= 240.0 for d in delays[8:])
    assert backoff.attempt == 12


@TIMEOUT
def test_backoff_reset_starts_over() -> None:
    backoff = Backoff(BackoffPolicy(jitter=0.0), random.Random(SEED))
    assert [backoff.next_delay() for _ in range(4)] == [1.0, 2.0, 4.0, 8.0]
    backoff.reset()
    assert backoff.next_delay() == 1.0


# --------------------------------------------------------------------------------------------
# Crash isolation, restarts, health
# --------------------------------------------------------------------------------------------


@TIMEOUT
async def test_a_crashing_collector_does_not_stop_its_neighbours() -> None:
    clock = FakeClock()
    sink = ListSink()
    supervisor = build([crasher("crash"), emitter("steady-a"), emitter("steady-b")], clock, sink)
    supervisor.start()
    await settle()
    first = sink.count("steady-a")
    assert first == 1
    for _ in range(5):
        await clock.advance(1.0)
    assert sink.count("steady-a") == first + 5
    assert sink.count("steady-b") == first + 5
    health = supervisor.health()
    assert health["steady-a"].state in {HealthState.STARTING, HealthState.OK}
    assert health["steady-a"].restarts == 0
    assert health["crash"].restarts >= 1
    assert health["crash"].last_error_kind == "RuntimeError"
    await supervisor.shutdown(timeout=2.0)


@TIMEOUT
async def test_tasks_are_named_after_their_collectors() -> None:
    clock = FakeClock()
    supervisor = build([emitter("alpha"), emitter("beta")], clock)
    supervisor.start()
    await settle()
    task = supervisor.task("alpha")
    assert task is not None
    assert task.get_name() == "alpha"
    assert {t.get_name() for t in asyncio.all_tasks()} >= {"alpha", "beta"}
    await supervisor.shutdown(timeout=2.0)


@TIMEOUT
async def test_restarts_follow_the_backoff_schedule_and_cap() -> None:
    clock = FakeClock()
    starts: list[float] = []
    supervisor = build([lifetimes("flaky", [0], starts)], clock)
    supervisor.start()
    await settle()
    expected = expected_delays(11)
    for index, delay in enumerate(expected):
        health = supervisor.health()["flaky"]
        assert health.state is HealthState.FAILED
        assert health.retry_in == pytest.approx(delay)
        assert health.restarts == index
        assert len(starts) == index + 1
        deadline = clock.monotonic() + (health.retry_in or 0.0)
        # Just before the deadline nothing restarts, at the deadline it does.
        await clock.advance_to(deadline - 0.01)
        assert len(starts) == index + 1
        await clock.advance_to(deadline)
        assert len(starts) == index + 2
        assert starts[-1] == deadline
    assert max(expected) <= 300.0
    assert expected[-1] >= 240.0
    await supervisor.shutdown(timeout=2.0)


@TIMEOUT
async def test_backoff_resets_after_ten_minutes_healthy() -> None:
    clock = FakeClock()
    starts: list[float] = []
    # Three immediate crashes, then a run that lives 601 s, then immediate crashes again.
    supervisor = build([lifetimes("flaky", [0, 0, 0, 601, 0], starts)], clock)
    supervisor.start()
    await settle()
    delays = expected_delays(3)
    for delay in delays:
        await clock.advance(delay)
    assert len(starts) == 4
    await clock.advance(601)
    health = supervisor.health()["flaky"]
    assert health.state is HealthState.FAILED
    # The long run reset the schedule, so the delay is the first one again, not the fifth.
    assert 0.8 <= (health.retry_in or 0.0) <= 1.2
    await supervisor.shutdown(timeout=2.0)


@TIMEOUT
async def test_a_run_just_short_of_ten_minutes_does_not_reset() -> None:
    clock = FakeClock()
    starts: list[float] = []
    supervisor = build([lifetimes("flaky", [0, 599, 0], starts)], clock)
    supervisor.start()
    await settle()
    first = expected_delays(2)
    await clock.advance(first[0])
    await clock.advance(599)
    health = supervisor.health()["flaky"]
    assert health.retry_in == pytest.approx(first[1])
    assert first[1] >= 1.6
    await supervisor.shutdown(timeout=2.0)


@TIMEOUT
async def test_a_collector_that_returns_is_restarted_as_exited() -> None:
    clock = FakeClock()
    runs: list[int] = []

    def factory(context: CollectorContext) -> FakeCollector:
        async def run() -> None:
            runs.append(1)

        return FakeCollector(run)

    supervisor = build([registered("short", factory)], clock)
    supervisor.start()
    await settle()
    health = supervisor.health()["short"]
    assert health.last_error_kind == "exited"
    await clock.advance(health.retry_in or 0.0)
    assert len(runs) == 2
    await supervisor.shutdown(timeout=2.0)


@TIMEOUT
async def test_a_factory_that_raises_is_retried() -> None:
    clock = FakeClock()
    calls: list[int] = []

    def factory(context: CollectorContext) -> FakeCollector:
        calls.append(1)
        raise ValueError("bad config")

    supervisor = build([registered("broken", factory)], clock)
    supervisor.start()
    await settle()
    assert supervisor.health()["broken"].last_error_kind == "ValueError"
    await clock.advance(2.0)
    assert len(calls) == 2
    await supervisor.shutdown(timeout=2.0)


@TIMEOUT
async def test_health_goes_from_starting_to_ok_through_doctor() -> None:
    clock = FakeClock()
    changes: list[CollectorHealth] = []
    supervisor = build([emitter("steady")], clock, on_health=changes.append)
    initial = supervisor.health()["steady"]
    assert initial.state is HealthState.STARTING
    supervisor.start()
    await settle()
    assert supervisor.health()["steady"].state is HealthState.STARTING
    await clock.advance(1.0)
    ok = supervisor.health()["steady"]
    assert ok.state is HealthState.OK
    assert ok.last_ok_at == clock.now()
    assert ok.since <= ok.last_ok_at
    assert [c.state for c in changes][-1] is HealthState.OK
    reports = supervisor.health_reports()
    assert reports["steady"].state is HealthState.OK
    await supervisor.shutdown(timeout=2.0)


@TIMEOUT
async def test_doctor_results_map_to_health() -> None:
    clock = FakeClock()
    answers = iter(
        [report(HealthState.DEGRADED, "slow"), report(HealthState.OK), report(HealthState.FAILED)]
    )

    def factory(context: CollectorContext) -> FakeCollector:
        async def run() -> None:
            await asyncio.Event().wait()

        async def doctor() -> HealthReport:
            return next(answers)

        return FakeCollector(run, doctor)

    supervisor = build([registered("watched", factory)], clock)
    supervisor.start()
    await clock.advance(1.0)
    degraded = supervisor.health()["watched"]
    assert degraded.state is HealthState.DEGRADED
    assert degraded.last_error_kind == "slow"
    await clock.advance(30.0)
    assert supervisor.health()["watched"].state is HealthState.OK
    await clock.advance(30.0)
    assert supervisor.health()["watched"].state is HealthState.FAILED
    await supervisor.shutdown(timeout=2.0)


@TIMEOUT
async def test_a_raising_doctor_degrades_the_collector() -> None:
    clock = FakeClock()

    def factory(context: CollectorContext) -> FakeCollector:
        async def run() -> None:
            await asyncio.Event().wait()

        async def doctor() -> HealthReport:
            raise OSError("nope")

        return FakeCollector(run, doctor)

    supervisor = build([registered("watched", factory)], clock)
    supervisor.start()
    await clock.advance(1.0)
    health = supervisor.health()["watched"]
    assert health.state is HealthState.DEGRADED
    assert health.last_error_kind == "doctor_error"
    assert health.detail == "doctor raised OSError"
    await supervisor.shutdown(timeout=2.0)


@TIMEOUT
async def test_a_hanging_doctor_times_out() -> None:
    clock = FakeClock()

    def factory(context: CollectorContext) -> FakeCollector:
        async def run() -> None:
            await asyncio.Event().wait()

        async def doctor() -> HealthReport:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        return FakeCollector(run, doctor)

    supervisor = build(
        [registered("watched", factory)], clock, policy=BackoffPolicy(probe_timeout=0.05)
    )
    supervisor.start()
    await clock.advance(1.0)
    await asyncio.sleep(0.3)
    health = supervisor.health()["watched"]
    assert health.state is HealthState.DEGRADED
    assert health.last_error_kind == "doctor_timeout"
    await supervisor.shutdown(timeout=2.0)


@TIMEOUT
async def test_a_failing_health_callback_does_not_hurt_the_collector() -> None:
    clock = FakeClock()
    calls: list[str] = []

    def callback(health: CollectorHealth) -> None:
        calls.append(health.state.value)
        raise RuntimeError("callback bug")

    sink = ListSink()
    supervisor = build([emitter("steady")], clock, sink, on_health=callback)
    supervisor.start()
    await clock.advance(3.0)
    assert sink.count("steady") == 4
    assert calls
    await supervisor.shutdown(timeout=2.0)


@TIMEOUT
async def test_non_runnable_entries_report_their_registry_health() -> None:
    clock = FakeClock()
    disabled = registered("off", lambda context: FakeCollector(asyncio.Event().wait), enabled=False)
    failed_health = report(HealthState.FAILED, "import")
    entry = DiscoveredEntry("broken", "x:y", "fylgja-fake", "0", _EntryPoint())
    broken = RegisteredCollector(entry, enabled=True, health=failed_health)
    plain = RegisteredCollector(
        DiscoveredEntry("plain", "x:y", None, None, _EntryPoint()), enabled=False
    )
    supervisor = build([disabled, broken, plain, emitter("on")], clock)
    supervisor.start()
    await settle()
    health = supervisor.health()
    assert health["broken"].state is HealthState.FAILED
    assert health["broken"].last_error_kind == "import"
    assert health["plain"].state is HealthState.DISABLED
    assert supervisor.task("broken") is None
    assert supervisor.task("plain") is None
    assert supervisor.task("on") is not None
    rows = {
        row.name: row
        for row in list_collectors(
            load_config(),
            registry=[disabled, broken, plain],
            health=supervisor.health_reports(),
        )
    }
    assert rows["broken"].state == "enabled"
    await supervisor.shutdown(timeout=2.0)
    assert supervisor.health()["broken"].state is HealthState.FAILED


@TIMEOUT
async def test_sink_factory_gives_each_collector_its_own_sink() -> None:
    clock = FakeClock()
    sinks: dict[str, ListSink] = {}

    def sink_for(name: str) -> ListSink:
        return sinks.setdefault(name, ListSink())

    supervisor = build([emitter("a"), emitter("b")], clock, sink_factory=sink_for)
    supervisor.start()
    await settle()
    assert set(sinks) == {"a", "b"}
    assert [r.source for r in sinks["a"].records] == ["a"]
    await supervisor.shutdown(timeout=2.0)


@TIMEOUT
async def test_context_given_to_the_factory_names_the_collector(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    seen: list[CollectorContext] = []

    def factory(context: CollectorContext) -> FakeCollector:
        seen.append(context)

        async def run() -> None:
            context.logger.warning("hello")
            await asyncio.Event().wait()

        return FakeCollector(run)

    supervisor = build([registered("alpha", factory)], clock)
    supervisor.start()
    await settle()
    assert seen[0].paths.inbox_dir.name == "alpha"
    assert seen[0].clock is clock
    assert any(getattr(r, "collector", None) == "alpha" for r in caplog.records)
    await supervisor.shutdown(timeout=2.0)


@TIMEOUT
async def test_start_twice_and_duplicate_names_are_rejected() -> None:
    clock = FakeClock()
    supervisor = build([emitter("a")], clock)
    supervisor.start()
    with pytest.raises(RuntimeError, match="already started"):
        supervisor.start()
    await supervisor.shutdown(timeout=2.0)
    with pytest.raises(ValueError, match="duplicate collector"):
        build([emitter("a"), emitter("a")], clock)


# --------------------------------------------------------------------------------------------
# Cancellation releases resources
# --------------------------------------------------------------------------------------------


class FakeWatcher:
    def __init__(self, asynchronous: bool = False) -> None:
        self.stopped = 0
        self._asynchronous = asynchronous

    def stop(self) -> None | Awaitable[None]:
        if self._asynchronous:
            return self._stop_async()
        self.stopped += 1
        return None

    async def _stop_async(self) -> None:
        await asyncio.sleep(0)
        self.stopped += 1


class Held:
    """What the resource-holding collector acquired."""

    def __init__(self) -> None:
        self.shell_pid = 0
        self.child_pid = 0
        self.connection: sqlite3.Connection | None = None
        self.watcher = FakeWatcher()
        self.async_watcher = FakeWatcher(asynchronous=True)
        self.ready = asyncio.Event()
        self.extra_cleanups: list[str] = []


def holder(name: str, held: Held, *, crash_after_acquire: bool = False) -> RegisteredCollector:
    def factory(context: CollectorContext) -> FakeCollector:
        async def run() -> None:
            async with Resources() as resources:
                process = await resources.spawn(
                    "/bin/sh",
                    "-c",
                    "sleep 300 & echo $!; wait",
                    stdout=asyncio.subprocess.PIPE,
                )
                assert process.stdout is not None
                line = await process.stdout.readline()
                held.shell_pid = process.pid
                held.child_pid = int(line)
                held.connection = resources.sqlite(":memory:")
                resources.watch(held.watcher)
                resources.watch(held.async_watcher)

                async def cleanup() -> None:
                    held.extra_cleanups.append("done")

                resources.add_cleanup(cleanup)
                held.ready.set()
                if crash_after_acquire:
                    raise RuntimeError("boom")
                await asyncio.Event().wait()

        return FakeCollector(run)

    return registered(name, factory)


def assert_released(held: Held) -> None:
    assert held.connection is not None
    with pytest.raises(sqlite3.ProgrammingError):
        held.connection.execute("select 1")
    assert held.watcher.stopped == 1
    assert held.async_watcher.stopped == 1
    assert held.extra_cleanups == ["done"]


@TIMEOUT
async def test_cancellation_releases_every_resource() -> None:
    clock = FakeClock()
    held = Held()
    supervisor = build([holder("holder", held)], clock)
    supervisor.start()
    await asyncio.wait_for(held.ready.wait(), 10)
    assert alive(held.shell_pid)
    assert alive(held.child_pid)
    await supervisor.shutdown(timeout=5.0)
    assert not supervisor.abandoned
    assert await wait_dead(held.shell_pid, held.child_pid)
    assert_released(held)
    assert supervisor.health()["holder"].state is HealthState.DISABLED


@TIMEOUT
async def test_a_crash_releases_every_resource_before_the_restart() -> None:
    clock = FakeClock()
    held = Held()
    supervisor = build([holder("holder", held, crash_after_acquire=True)], clock)
    supervisor.start()
    await asyncio.wait_for(held.ready.wait(), 10)
    for _ in range(100):
        if supervisor.health()["holder"].state is HealthState.FAILED:
            break
        await asyncio.sleep(0.02)
    assert supervisor.health()["holder"].state is HealthState.FAILED
    assert await wait_dead(held.shell_pid, held.child_pid)
    assert_released(held)
    await supervisor.shutdown(timeout=5.0)


@TIMEOUT
async def test_cancelling_during_backoff_is_clean() -> None:
    clock = FakeClock()
    supervisor = build([crasher("crash")], clock)
    supervisor.start()
    await settle()
    assert supervisor.health()["crash"].state is HealthState.FAILED
    await supervisor.shutdown(timeout=2.0)
    task = supervisor.task("crash")
    assert task is not None
    assert task.done()
    assert supervisor.health()["crash"].state is HealthState.DISABLED


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


@TIMEOUT
async def test_terminate_process_group_kills_children_after_the_leader_exited(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "child.pid"
    process = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        f"sleep 300 & echo $! > {pid_file}",
        start_new_session=True,
    )
    await process.wait()
    child = int(pid_file.read_text())
    assert alive(child)
    await terminate_process_group(process)
    assert await wait_dead(child)


@TIMEOUT
async def test_terminate_process_group_escalates_to_sigkill() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import signal,time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(300)\n",
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert (await process.stdout.readline()).strip() == b"ready"
    await terminate_process_group(process, grace=0.2)
    assert process.returncode is not None
    assert not alive(process.pid)


@TIMEOUT
async def test_terminate_process_group_on_a_reaped_process_is_a_no_op() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass", start_new_session=True
    )
    await process.wait()
    await terminate_process_group(process)


@TIMEOUT
def test_close_sqlite_is_idempotent() -> None:
    connection = sqlite3.connect(":memory:")
    close_sqlite(connection)
    close_sqlite(connection)
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("select 1")


@TIMEOUT
async def test_stop_watcher_handles_sync_and_async_stops() -> None:
    sync_watcher = FakeWatcher()
    async_watcher = FakeWatcher(asynchronous=True)
    await stop_watcher(sync_watcher)
    await stop_watcher(async_watcher)
    assert (sync_watcher.stopped, async_watcher.stopped) == (1, 1)


@TIMEOUT
async def test_resources_release_in_reverse_order_and_survive_a_failing_release() -> None:
    order: list[str] = []
    resources = Resources()

    async def first() -> None:
        order.append("first")

    async def broken() -> None:
        order.append("broken")
        raise OSError("release failed")

    async def use() -> None:
        async with resources:
            resources.add_cleanup(first)
            resources.add_cleanup(broken)

    with pytest.raises(OSError, match="release failed"):
        await use()

    assert order == ["broken", "first"]


@TIMEOUT
async def test_resources_aclose_releases() -> None:
    watcher = FakeWatcher()
    resources = Resources()
    await resources.__aenter__()
    resources.watch(watcher)
    await resources.aclose()
    assert watcher.stopped == 1


# --------------------------------------------------------------------------------------------
# Shutdown
# --------------------------------------------------------------------------------------------


@TIMEOUT
async def test_shutdown_is_prompt_with_well_behaved_collectors() -> None:
    clock = FakeClock()
    supervisor = build([emitter(f"c{i}") for i in range(5)], clock)
    supervisor.start()
    await settle()
    began = time.monotonic()
    await supervisor.shutdown(timeout=5.0)
    assert time.monotonic() - began < 1.0
    assert all(t.done() for t in (supervisor.task(f"c{i}") for i in range(5)) if t is not None)
    await supervisor.shutdown(timeout=5.0)


@TIMEOUT
async def test_shutdown_without_start_returns() -> None:
    supervisor = build([emitter("a")], FakeClock())
    await supervisor.shutdown(timeout=1.0)
    assert supervisor.health()["a"].state is HealthState.STARTING


@TIMEOUT
async def test_shutdown_abandons_a_collector_that_swallows_cancellation() -> None:
    clock = FakeClock()
    release = asyncio.Event()
    swallowed = asyncio.Event()

    def factory(context: CollectorContext) -> FakeCollector:
        async def run() -> None:
            while not release.is_set():
                try:
                    await asyncio.wait_for(release.wait(), 3600)
                except asyncio.CancelledError:
                    swallowed.set()

        return FakeCollector(run)

    sink = ListSink()
    supervisor = build([registered("stubborn", factory), emitter("polite")], clock, sink)
    supervisor.start()
    await settle()
    deadline = 0.3
    began = time.monotonic()
    await supervisor.shutdown(timeout=deadline)
    elapsed = time.monotonic() - began
    assert elapsed < deadline + 1.0
    assert swallowed.is_set()
    health = supervisor.health()
    assert health["stubborn"].state is HealthState.FAILED
    assert health["stubborn"].last_error_kind == "shutdown_timeout"
    assert health["polite"].state is HealthState.DISABLED
    assert len(supervisor.abandoned) == 1
    assert supervisor.abandoned[0].get_name() == "stubborn"
    # Letting it go finishes the abandoned task without a restart or a state change.
    release.set()
    await asyncio.wait_for(asyncio.gather(*supervisor.abandoned), 5)
    assert supervisor.health()["stubborn"].state is HealthState.FAILED
    assert supervisor.health()["stubborn"].last_error_kind == "shutdown_timeout"
