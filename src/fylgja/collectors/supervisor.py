"""Collector supervisor (``PLAN-final-plan.md`` WP1.3, §3.1, §3.3; ``PLAN.md`` §4.1, §4.9).

One asyncio task per runnable collector, named after the collector. A crash is contained to its
own task and followed by a restart after an exponential backoff (1 s doubling to 5 min, ±20 %
jitter, reset after 10 min of healthy running). Health is tracked per collector and exposed through
:meth:`Supervisor.health` and :meth:`Supervisor.health_reports`; persisting it is the ingest
side's job (it receives every change through ``on_health``). The supervisor knows no collector by
name: it only sees :class:`~fylgja.collectors.registry.RegisteredCollector` items.

The cancellation helpers at the end (:class:`Resources` and the functions it builds on) are for
collectors: a cancelled collector kills its subprocess groups, closes its SQLite connections and
stops its file watchers, so it leaves nothing behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import random
import signal
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Protocol, Self

from fylgja.collectors.base import Clock, Collector, RecordSink
from fylgja.collectors.context import SystemClock, build_context
from fylgja.collectors.registry import RegisteredCollector
from fylgja.models import HealthReport, HealthState

__all__ = [
    "Backoff",
    "BackoffPolicy",
    "CollectorHealth",
    "RandomSource",
    "Resources",
    "Stoppable",
    "Supervisor",
    "close_sqlite",
    "stop_watcher",
    "terminate_process_group",
]

logger = logging.getLogger("fylgja.supervisor")

_DOCTOR_ERROR: Final = "doctor_error"
_DOCTOR_TIMEOUT: Final = "doctor_timeout"
_EXITED: Final = "exited"
_SHUTDOWN_TIMEOUT: Final = "shutdown_timeout"


class RandomSource(Protocol):
    """The slice of :class:`random.Random` the backoff uses."""

    def uniform(self, a: float, b: float) -> float: ...


@dataclass(frozen=True)
class BackoffPolicy:
    """Restart timing and probe timing. Defaults are the plan's numbers."""

    initial: float = 1.0
    maximum: float = 300.0
    factor: float = 2.0
    jitter: float = 0.2
    reset_after: float = 600.0
    probe_delay: float = 1.0
    probe_interval: float = 30.0
    probe_timeout: float = 10.0


class Backoff:
    """Exponential delays with jitter. Pure apart from the injected random source."""

    def __init__(self, policy: BackoffPolicy, rng: RandomSource) -> None:
        self._policy = policy
        self._rng = rng
        self._attempt = 0

    @property
    def attempt(self) -> int:
        """How many delays were handed out since the last reset."""
        return self._attempt

    def next_delay(self) -> float:
        """The next delay: ``initial * factor**n`` capped, jittered, and never above the cap."""
        policy = self._policy
        base = min(policy.maximum, policy.initial * policy.factor**self._attempt)
        self._attempt += 1
        jittered = base * (1.0 + self._rng.uniform(-policy.jitter, policy.jitter))
        return min(policy.maximum, jittered)

    def reset(self) -> None:
        self._attempt = 0


@dataclass(frozen=True)
class CollectorHealth:
    """Health of one collector, as ``/collectors`` and doctor show it."""

    name: str
    state: HealthState
    since: datetime
    last_ok_at: datetime | None = None
    restarts: int = 0
    last_error_kind: str | None = None
    detail: str | None = None
    retry_in: float | None = None
    """Seconds until the next restart while the collector waits in backoff, else ``None``."""

    def to_report(self, checked_at: datetime) -> HealthReport:
        return HealthReport(
            state=self.state,
            detail=self.detail,
            error_kind=self.last_error_kind,
            checked_at=checked_at,
        )


HealthCallback = Callable[[CollectorHealth], None]
SinkFactory = Callable[[str], RecordSink]


class _Entry:
    def __init__(self, item: RegisteredCollector, backoff: Backoff, health: CollectorHealth):
        self.item = item
        self.backoff = backoff
        self.health = health
        self.task: asyncio.Task[None] | None = None
        self.abandoned = False


class Supervisor:
    """Runs every runnable collector and keeps it running."""

    def __init__(
        self,
        collectors: Iterable[RegisteredCollector],
        sink: RecordSink,
        *,
        clock: Clock | None = None,
        rng: RandomSource | None = None,
        policy: BackoffPolicy | None = None,
        on_health: HealthCallback | None = None,
        sink_factory: SinkFactory | None = None,
    ) -> None:
        self._sink = sink
        self._sink_factory = sink_factory
        self._clock: Clock = SystemClock() if clock is None else clock
        self._rng: RandomSource = random.Random() if rng is None else rng
        self._policy = BackoffPolicy() if policy is None else policy
        self._on_health = on_health
        self._entries: dict[str, _Entry] = {}
        self._started = False
        self._stopping = False
        self._abandoned: list[asyncio.Task[None]] = []
        for item in collectors:
            if item.name in self._entries:
                raise ValueError(f"duplicate collector name {item.name!r}")
            self._entries[item.name] = _Entry(
                item, Backoff(self._policy, self._rng), self._initial_health(item)
            )

    # -- public API -------------------------------------------------------------------------

    @property
    def abandoned(self) -> tuple[asyncio.Task[None], ...]:
        """Tasks that ignored cancellation past the shutdown deadline."""
        return tuple(self._abandoned)

    def health(self) -> dict[str, CollectorHealth]:
        return {name: entry.health for name, entry in self._entries.items()}

    def health_reports(self) -> dict[str, HealthReport]:
        """Health in the shape ``registry.list_collectors(health=...)`` takes."""
        now = self._clock.now()
        return {name: entry.health.to_report(now) for name, entry in self._entries.items()}

    def task(self, name: str) -> asyncio.Task[None] | None:
        return self._entries[name].task

    def start(self) -> None:
        """Create one task per runnable collector. Must run inside the event loop."""
        if self._started:
            raise RuntimeError("supervisor already started")
        self._started = True
        for name, entry in self._entries.items():
            if entry.item.runnable:
                entry.task = asyncio.create_task(self._supervise(entry), name=name)

    async def shutdown(self, timeout: float = 5.0) -> None:  # noqa: ASYNC109 - spec'd signature
        """Cancel every task and wait at most ``timeout`` seconds (real time).

        A collector that ignores cancellation is abandoned and marked ``failed``; the call still
        returns within the bound.
        """
        self._stopping = True
        tasks = [e.task for e in self._entries.values() if e.task is not None and not e.task.done()]
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for entry in self._entries.values():
            if entry.task is not None and entry.task in pending:
                entry.abandoned = True
                self._abandoned.append(entry.task)
                logger.error("collector %s ignored cancellation; abandoned", entry.item.name)
                self._set(
                    entry,
                    state=HealthState.FAILED,
                    last_error_kind=_SHUTDOWN_TIMEOUT,
                    detail="ignored cancellation at shutdown",
                    retry_in=None,
                )
            elif entry.task is not None and entry.health.state is not HealthState.DISABLED:
                # A task cancelled before its first step never ran its ``finally``.
                self._set(entry, state=HealthState.DISABLED, detail="stopped", retry_in=None)

    # -- health bookkeeping -----------------------------------------------------------------

    def _initial_health(self, item: RegisteredCollector) -> CollectorHealth:
        now = self._clock.now()
        if item.runnable:
            return CollectorHealth(item.name, HealthState.STARTING, since=now)
        if item.health is not None:
            return CollectorHealth(
                item.name,
                item.health.state,
                since=now,
                last_error_kind=item.health.error_kind,
                detail=item.health.detail,
            )
        return CollectorHealth(item.name, HealthState.DISABLED, since=now)

    def _set(
        self,
        entry: _Entry,
        *,
        state: HealthState | None = None,
        **changes: Any,
    ) -> None:
        previous = entry.health
        updated = replace(previous, **changes)
        if state is not None and state is not previous.state:
            updated = replace(updated, state=state, since=self._clock.now())
        if state is HealthState.OK:
            updated = replace(updated, last_ok_at=self._clock.now())
        if updated == previous:
            return
        entry.health = updated
        if self._on_health is not None:
            try:
                self._on_health(updated)
            except Exception:
                logger.exception("health callback failed for %s", entry.item.name)

    # -- the per-collector task -------------------------------------------------------------

    async def _supervise(self, entry: _Entry) -> None:
        name = entry.item.name
        first = True
        try:
            while not self._stopping:
                if not first:
                    self._set(
                        entry,
                        state=HealthState.STARTING,
                        restarts=entry.health.restarts + 1,
                        retry_in=None,
                        detail=None,
                    )
                first = False
                started = self._clock.monotonic()
                kind = await self._run_once(entry)
                if self._stopping:
                    break
                if self._clock.monotonic() - started >= self._policy.reset_after:
                    entry.backoff.reset()
                delay = entry.backoff.next_delay()
                logger.warning("collector %s stopped (%s); restart in %.1fs", name, kind, delay)
                self._set(
                    entry,
                    state=HealthState.FAILED,
                    last_error_kind=kind,
                    detail="restarting after failure",
                    retry_in=delay,
                )
                await self._clock.sleep(delay)
        finally:
            if not entry.abandoned:
                self._set(entry, state=HealthState.DISABLED, detail="stopped", retry_in=None)

    async def _run_once(self, entry: _Entry) -> str:
        """Run the collector until it fails or exits. Returns the error kind."""
        item = entry.item
        plugin = item.plugin
        if plugin is None or item.settings is None:  # pragma: no cover - runnable guarantees it
            return "not_loaded"
        probe: asyncio.Task[None] | None = None
        try:
            sink = self._sink if self._sink_factory is None else self._sink_factory(item.name)
            context = build_context(item.name, sink, clock=self._clock)
            collector = plugin.factory(item.settings, context)
            probe = asyncio.create_task(self._probe(entry, collector), name=f"{item.name}:probe")
            await collector.run()
        except Exception as exc:
            logger.error("collector %s crashed: %s", item.name, type(exc).__name__, exc_info=True)
            return type(exc).__name__
        finally:
            if probe is not None:
                probe.cancel()
                await asyncio.gather(probe, return_exceptions=True)
        return _EXITED

    async def _probe(self, entry: _Entry, collector: Collector) -> None:
        policy = self._policy
        await self._clock.sleep(policy.probe_delay)
        while True:
            try:
                report = await asyncio.wait_for(collector.doctor(), policy.probe_timeout)
            except TimeoutError:
                self._set(
                    entry,
                    state=HealthState.DEGRADED,
                    last_error_kind=_DOCTOR_TIMEOUT,
                    detail="doctor timed out",
                )
            except Exception as exc:
                logger.warning("doctor of %s failed: %s", entry.item.name, type(exc).__name__)
                self._set(
                    entry,
                    state=HealthState.DEGRADED,
                    last_error_kind=_DOCTOR_ERROR,
                    detail=f"doctor raised {type(exc).__name__}",
                )
            else:
                self._set(
                    entry,
                    state=report.state,
                    last_error_kind=report.error_kind,
                    detail=report.detail,
                )
            await self._clock.sleep(policy.probe_interval)


# --------------------------------------------------------------------------------------------
# Cancellation helpers for collectors
# --------------------------------------------------------------------------------------------


class Stoppable(Protocol):
    """A file watcher or anything else with a ``stop()`` that may be sync or async."""

    def stop(self) -> None | Awaitable[None]: ...


def _killpg(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


async def terminate_process_group(
    process: asyncio.subprocess.Process, *, grace: float = 2.0
) -> None:
    """Stop ``process`` and everything in its process group, then reap it.

    The process must have been started with ``start_new_session=True`` (its pid is then the group
    id). SIGTERM goes to the group first, SIGKILL follows after ``grace`` seconds or when the
    leader is gone, so children that outlive the leader are not left behind.
    """
    pgid = process.pid
    if process.returncode is None:
        _killpg(pgid, signal.SIGTERM)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), grace)
    _killpg(pgid, signal.SIGKILL)
    await process.wait()


def close_sqlite(connection: sqlite3.Connection) -> None:
    """Close a connection; closing twice is fine."""
    with contextlib.suppress(sqlite3.Error):
        connection.close()


async def stop_watcher(watcher: Stoppable) -> None:
    """Call ``watcher.stop()``, awaiting the result when it is awaitable."""
    result = watcher.stop()
    if inspect.isawaitable(result):
        await result


class Resources:
    """Everything a collector must release when it is cancelled or crashes.

    Use it as ``async with Resources() as res:`` inside ``run()``. Resources are released in
    reverse order of acquisition, and one failing release does not stop the others.
    """

    def __init__(self, *, grace: float = 2.0) -> None:
        self._stack = AsyncExitStack()
        self._grace = grace

    async def __aenter__(self) -> Self:
        await self._stack.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        return await self._stack.__aexit__(exc_type, exc, tb)

    async def spawn(self, *argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        """Start a subprocess in its own process group; the group is killed on exit."""
        kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(*argv, **kwargs)
        grace = self._grace
        self._stack.push_async_callback(lambda: terminate_process_group(process, grace=grace))
        return process

    def sqlite(self, database: str | Path, **kwargs: Any) -> sqlite3.Connection:
        """Open a connection that is closed on exit."""
        connection: sqlite3.Connection = sqlite3.connect(database, **kwargs)
        self._stack.callback(close_sqlite, connection)
        return connection

    def watch[W: Stoppable](self, watcher: W) -> W:
        """Register a started watcher; it is stopped on exit."""
        self._stack.push_async_callback(lambda: stop_watcher(watcher))
        return watcher

    def add_cleanup(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register any other async release step."""
        self._stack.push_async_callback(callback)

    async def aclose(self) -> None:
        await self._stack.aclose()
