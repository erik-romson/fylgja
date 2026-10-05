"""The daemon: one in-process composition of every part (``PLAN-final-plan.md`` §3.1, WP1.9).

Data flows **registry → supervisor → ingest gate → writer → state engine → live view → API**:

* the **registry** reads the ``fylgja.collectors`` entry points and the configuration and imports
  only what may run (:mod:`fylgja.collectors.registry`);
* the **supervisor** runs one task per runnable collector and hands each a sink;
* every sink is the **ingest gate**'s (validation, privacy filter, rejection accounting), and the
  gate queues what it accepts for the single **writer**;
* between the gate and the writer sits the state engine's tap: the **resolver** sees every record
  (the ancestor chains it needs are never stored) and so does :class:`SessionMemory`, which keeps
  what ``/now`` shows but the store has no column for (the facts block, the entrypoint and the
  version). The **session state machine** (:mod:`fylgja.core.sessions`, WP2.3) filters the records
  before that: it turns hook events into session state, overlays it on the registry's activity and
  marks a finished turn ``seen`` when its window gains focus;
* the writer commits and hands each transaction's changes to the **live view**; a change of
  sessions, windows or workspaces schedules a resolver refresh, which writes the placements and
  inferred labels back through the writer;
* the API reads :meth:`Daemon.state` and never touches SQLite.

**Start and stop order.** Sinks start before sources, so nothing a collector emits is lost or
applied out of order: the writer (the database is opened and migrated), then the live view (rebuilt
from the store), then the state engine (resolver refresh and maintenance tasks), and last the
supervisor (the collectors). Stopping runs in reverse: the collectors stop first, then the state
engine, and the writer commits everything still queued before it closes.
:attr:`Daemon.lifecycle` records each step.

The daemon knows no collector by name: it sees registry items, capabilities and health only.
Nothing here imports FastAPI; the API is built on top of it (:mod:`fylgja.api.app`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from fylgja.collectors.base import Capability, Clock
from fylgja.collectors.context import SystemClock
from fylgja.collectors.registry import (
    CollectorRow,
    EntryPointSource,
    Origin,
    RegisteredCollector,
    list_collectors,
    load_registry,
)
from fylgja.collectors.seams import FileSystem, ProcessRunner
from fylgja.collectors.supervisor import BackoffPolicy, CollectorHealth, RandomSource, Supervisor
from fylgja.config import Config
from fylgja.core import hosts
from fylgja.core.hosts import HostData
from fylgja.core.ingest import IngestGate, PluginSink
from fylgja.core.live_view import Collection, LiveSnapshot, LiveUpdate, LiveView
from fylgja.core.project_roots import ProjectLocator
from fylgja.core.resolver import Placement, ProjectPlacement, Resolution, Resolver
from fylgja.core.retention import MaintenanceJob
from fylgja.core.sessions import SessionStateEngine
from fylgja.core.system import LocalFileSystem, SubprocessRunner
from fylgja.core.writer import Writer, WriterClosed, session_key, split_session_key
from fylgja.models import (
    HealthReport,
    HealthState,
    Record,
    SessionActivityEvent,
    SessionActivitySnapshot,
    SessionEvent,
    SessionPayload,
    SessionSnapshot,
)
from fylgja.privacy import PrivacyFilter
from fylgja.store import db
from fylgja.store.rows import HealthRow

__all__ = [
    "AVAILABLE_STATES",
    "CollectorState",
    "Daemon",
    "HostLoader",
    "SessionExtras",
    "SessionMemory",
    "StateView",
]

logger = logging.getLogger("fylgja.daemon")

AVAILABLE_STATES: Final = frozenset({HealthState.STARTING, HealthState.OK})
"""Health states whose data ``/now`` shows. A degraded, failed or disabled collector's data is
absent, never an error (``PLAN.md`` §13)."""

RESOLVE_DEBOUNCE: Final = 0.25
"""Seconds a burst of changes is coalesced before the resolver refreshes."""
MAINTENANCE_TICK: Final = 30.0
"""Seconds between periodic resolver refreshes (host data changes without a live-view change) and
re-evaluations of the ingest gate's rejection windows."""
SHUTDOWN_TIMEOUT: Final = 5.0

_ENGINE_TRIGGERS: Final = frozenset(
    {Collection.SESSIONS, Collection.WINDOWS, Collection.WORKSPACES}
)
"""Live-view collections whose change makes the resolver refresh."""

HostLoader = Callable[[], Awaitable[HostData]]


# --------------------------------------------------------------------------------------------
# What /now is built from
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectorState:
    """One collector as the daemon sees it now."""

    name: str
    origin: Origin
    enabled: bool
    capabilities: frozenset[Capability]
    health: CollectorHealth

    @property
    def available(self) -> bool:
        """Its data is shown: enabled, loaded, and starting or ok."""
        return self.enabled and bool(self.capabilities) and self.health.state in AVAILABLE_STATES

    @property
    def degraded(self) -> bool:
        return self.health.state in (HealthState.DEGRADED, HealthState.FAILED)


@dataclass(frozen=True)
class SessionExtras:
    """What the daemon remembers about a live session beyond its stored row."""

    facts: Mapping[str, Any] | None = None
    """The Lidskjalv-compatible facts block of the latest activity (already redacted)."""
    entrypoint: str | None = None
    version: str | None = None


@dataclass(frozen=True)
class StateView:
    """Everything ``/now`` is built from, taken at one moment on the event loop."""

    generated_at: datetime
    live: LiveSnapshot
    collectors: tuple[CollectorState, ...]
    sessions: Mapping[str, SessionExtras]
    placements: Mapping[str, Placement]
    projects: Mapping[str, ProjectPlacement]
    labels: Mapping[str, str]
    """The inferred label of each workspace from the latest resolution (also for workspaces that
    have no stored row)."""


# --------------------------------------------------------------------------------------------
# Session memory
# --------------------------------------------------------------------------------------------


class SessionMemory:
    """The facts block, entrypoint and version of each live session, from the records themselves.

    The store has no column for these, so they are kept in memory, keyed by session key and
    remembered per source: a complete snapshot of a source forgets the sessions of that source it
    does not list. After a restart they return with the collector's next emission.
    """

    def __init__(self) -> None:
        self._facts: dict[str, tuple[str, dict[str, Any]]] = {}
        self._payloads: dict[str, tuple[str, str | None, str | None]] = {}

    def observe(self, record: Record) -> None:
        if isinstance(record, SessionSnapshot):
            for item in record.items:
                self._payload(record.source, item.payload)
            if record.complete:
                listed = {session_key(i.payload.kind, i.payload.native_id) for i in record.items}
                self._prune(self._payloads, record.source, listed)
        elif isinstance(record, SessionEvent):
            self._payload(record.source, record.payload)
        elif isinstance(record, SessionActivitySnapshot):
            for activity in record.items:
                self._activity(record.source, activity.entity_id, activity.payload.facts)
            if record.complete:
                listed = {self._key(i.entity_id, record.source) for i in record.items}
                self._prune(self._facts, record.source, listed)
        elif isinstance(record, SessionActivityEvent):
            self._activity(record.source, record.entity_id, record.payload.facts)

    @staticmethod
    def _key(entity_id: str, source: str) -> str:
        return session_key(*split_session_key(entity_id, default_kind=source))

    def _payload(self, source: str, payload: SessionPayload) -> None:
        key = session_key(payload.kind, payload.native_id)
        self._payloads[key] = (source, payload.entrypoint, payload.version)

    def _activity(self, source: str, entity_id: str, facts: Mapping[str, Any] | None) -> None:
        key = self._key(entity_id, source)
        if facts is None:
            self._facts.pop(key, None)
        else:
            self._facts[key] = (source, dict(facts))

    @staticmethod
    def _prune(table: dict[str, Any], source: str, listed: set[str]) -> None:
        for key in [k for k, value in table.items() if value[0] == source and k not in listed]:
            del table[key]

    def forget(self, key: str) -> None:
        self._facts.pop(key, None)
        self._payloads.pop(key, None)

    def get(self, key: str) -> SessionExtras | None:
        facts = self._facts.get(key)
        payload = self._payloads.get(key)
        if facts is None and payload is None:
            return None
        return SessionExtras(
            facts=None if facts is None else facts[1],
            entrypoint=None if payload is None else payload[1],
            version=None if payload is None else payload[2],
        )

    def keys(self) -> set[str]:
        return {*self._facts, *self._payloads}


# --------------------------------------------------------------------------------------------
# Plumbing between the gate and the writer
# --------------------------------------------------------------------------------------------

RecordObserver = Callable[[Record], None]
RecordFilter = Callable[[Record], Record]


class _Tap:
    """The ingest gate's writer: shows every accepted record to the state engine, then queues it.

    A filter may replace the record before it is queued and observed; an observer only looks. One
    that raises is logged and skipped; it never costs the record.
    """

    def __init__(
        self,
        writer: Writer,
        observers: Sequence[RecordObserver],
        filters: Sequence[RecordFilter] = (),
    ) -> None:
        self._writer = writer
        self._observers = tuple(observers)
        self._filters = tuple(filters)

    def _filter(self, record: Record) -> Record:
        for apply in self._filters:
            try:
                record = apply(record)
            except Exception:
                logger.exception("state engine filter failed")
        return record

    def _observe(self, record: Record) -> None:
        for observer in self._observers:
            try:
                observer(record)
            except Exception:
                logger.exception("state engine observer failed")

    def submit(self, record: Record, *, received_at: datetime | None = None) -> None:
        record = self._filter(record)
        self._writer.submit(record, received_at=received_at)
        self._observe(record)

    async def submit_committed(
        self, record: Record, *, received_at: datetime | None = None
    ) -> object:
        record = self._filter(record)
        self._observe(record)
        return await self._writer.submit_committed(record, received_at=received_at)

    def save_health(self, row: HealthRow) -> None:
        self._writer.save_health(row)


class _UnroutedSink:
    """The supervisor's fallback sink. Every collector gets its own gate sink, so this one only
    drops what reaches it."""

    def emit(self, record: Record) -> None:
        logger.warning("dropped a record that had no collector sink")

    async def emit_committed(self, record: Record) -> None:
        self.emit(record)


# --------------------------------------------------------------------------------------------
# The daemon
# --------------------------------------------------------------------------------------------


class Daemon:
    """Composes the parts from configuration; see the module docstring.

    Every seam can be replaced: ``entry_points`` (the registry's source), ``clock``, ``database``,
    ``fs`` and ``runner`` (what the resolver and host adapters read with), ``host_loader``,
    ``policy`` and ``rng`` (the supervisor's backoff), and the resolver's ``resolve_debounce`` and
    the ``tick`` interval.
    """

    def __init__(
        self,
        config: Config,
        *,
        entry_points: EntryPointSource | None = None,
        clock: Clock | None = None,
        database: Path | None = None,
        fs: FileSystem | None = None,
        runner: ProcessRunner | None = None,
        host_loader: HostLoader | None = None,
        home: str | None = None,
        policy: BackoffPolicy | None = None,
        rng: RandomSource | None = None,
        resolve_debounce: float = RESOLVE_DEBOUNCE,
        tick: float = MAINTENANCE_TICK,
    ) -> None:
        self.config = config
        self.clock: Clock = SystemClock() if clock is None else clock
        self._debounce = resolve_debounce
        self._tick = tick
        self._home = str(Path.home()) if home is None else home
        filesystem: FileSystem = LocalFileSystem() if fs is None else fs
        process_runner: ProcessRunner = SubprocessRunner() if runner is None else runner
        self._host_loader: HostLoader = (
            hosts.host_loader(process_runner, filesystem, home=self._home)
            if host_loader is None
            else host_loader
        )
        self.lifecycle: list[str] = []
        """``start:<part>`` and ``stop:<part>``, in the order they happened."""
        self.started_at: datetime | None = None
        self._created_at = self.clock.now()

        # Compose in data-flow order.
        self.registry: tuple[RegisteredCollector, ...] = load_registry(config, entry_points)
        self.view = LiveView()
        self.writer = Writer(database, clock=self.clock, on_commit=self.view.apply)
        self.memory = SessionMemory()
        self.session_state = SessionStateEngine(self.writer.submit, self._window_of)
        self.resolver = Resolver(
            self.writer,
            self.view,
            hosts=self._load_hosts,
            locator=ProjectLocator(filesystem),
            clock=self.clock,
        )
        self.resolver.on_ids_changed = self._mark_dirty
        self.gate = IngestGate(
            _Tap(
                self.writer,
                (self.resolver.observe, self.memory.observe),
                (self.session_state.apply,),
            ),
            [item.plugin for item in self.registry if item.runnable and item.plugin is not None],
            privacy=PrivacyFilter.from_config(config.privacy),
            clock=self.clock,
        )
        self.supervisor = Supervisor(
            [item for item in self.registry if not item.rejected],
            _UnroutedSink(),
            clock=self.clock,
            rng=rng,
            policy=policy,
            on_health=self.gate.observe_health,
            sink_factory=self.gate.sink,
        )
        for health in self.supervisor.health().values():
            self.gate.observe_health(health)  # every collector's health is known from the start
        self.maintenance = MaintenanceJob(self.writer, clock=self.clock)

        self._resolution: Resolution | None = None
        self._dirty = asyncio.Event()
        self._engine_idle = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._unsubscribe: Callable[[], None] | None = None
        self._phase = "new"

    # -- state ------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._phase == "running"

    @property
    def started(self) -> bool:
        """``start()`` was called (the daemon may have stopped since)."""
        return self._phase != "new"

    @property
    def resolution(self) -> Resolution | None:
        """The latest resolver result (``via`` and ``confidence`` live only here)."""
        return self._resolution

    def sink(self, name: str) -> PluginSink:
        """The ingest-gate sink of collector ``name`` (what its context carries)."""
        return self.gate.sink(name)

    # -- lifecycle --------------------------------------------------------------------------

    async def start(self) -> None:
        """Start the writer, the live view, the state engine and then the collectors."""
        if self._phase != "new":
            raise RuntimeError("the daemon was already started")
        self._phase = "starting"
        try:
            await self.writer.start()
            self.lifecycle.append("start:writer")
            self._rebuild_view()
            self.session_state.seed(
                (row.session_key, row.source, row.state, row.state_since, row.waiting_reason)
                for row in self.view.sessions.values()
            )
            self.lifecycle.append("start:live_view")
            self._unsubscribe = self.view.subscribe(self._on_update)
            self._mark_dirty()  # place what the store already holds
            self._tasks = [
                asyncio.create_task(self._engine_loop(), name="fylgja-state-engine"),
                asyncio.create_task(self._tick_loop(), name="fylgja-tick"),
                asyncio.create_task(self.maintenance.run(), name="fylgja-maintenance"),
            ]
            self.lifecycle.append("start:state_engine")
            self.supervisor.start()
            self.lifecycle.append("start:supervisor")
        except BaseException:
            logger.error("daemon start failed; stopping what was started")
            await self._teardown(SHUTDOWN_TIMEOUT)
            self._phase = "stopped"
            raise
        self.started_at = self.clock.now()
        self._phase = "running"
        logger.info("daemon started with %d collector(s)", len(self.registry))

    async def stop(self, timeout: float = SHUTDOWN_TIMEOUT) -> None:  # noqa: ASYNC109 - a deadline
        """Stop the collectors, then the state engine, then commit and close the writer."""
        if self._phase != "running":
            return
        self._phase = "stopping"
        await self._teardown(timeout)
        self._phase = "stopped"
        logger.info("daemon stopped")

    async def _teardown(self, timeout: float) -> None:  # noqa: ASYNC109 - a deadline
        if "start:supervisor" in self.lifecycle:
            await self.supervisor.shutdown(timeout)
            self.lifecycle.append("stop:supervisor")
        if self._tasks or self._unsubscribe is not None:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks = []
            if self._unsubscribe is not None:
                self._unsubscribe()
                self._unsubscribe = None
            self.lifecycle.append("stop:state_engine")
        if "start:writer" in self.lifecycle:
            await self.writer.stop()
            self.lifecycle.append("stop:writer")

    def _rebuild_view(self) -> None:
        connection = db.connect_reader(self.writer.database)
        try:
            self.view.rebuild(connection)
        finally:
            connection.close()

    async def settle(self, rounds: int = 10, timeout: float = 5.0) -> None:  # noqa: ASYNC109
        """Return once everything queued is committed and the resolver has caught up.

        Meant for tests and orderly shutdown; with a debounce on a fake clock it only flushes.
        """
        if not self.running:
            return
        for _ in range(rounds):
            await self.writer.flush()
            if self._engine_idle.is_set() and not self._dirty.is_set():
                return
            if self._debounce > 0 and not isinstance(self.clock, SystemClock):
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._engine_idle.wait(), timeout)

    # -- state engine -----------------------------------------------------------------------

    def _on_update(self, update: LiveUpdate) -> None:
        for change in update.changes:
            if change.collection is Collection.SESSIONS and change.row is None:
                self.memory.forget(change.key)
                self.resolver.forget(change.key)
                self.session_state.forget(change.key)
        if update.collections & _ENGINE_TRIGGERS:
            self._mark_dirty()

    def _mark_dirty(self) -> None:
        self._engine_idle.clear()
        self._dirty.set()

    async def _engine_loop(self) -> None:
        while True:
            if not self._dirty.is_set():
                self._engine_idle.set()
                await self._dirty.wait()
            if self._debounce > 0:
                await self.clock.sleep(self._debounce)
            self._dirty.clear()
            await self._refresh()

    async def _refresh(self) -> None:
        try:
            self._resolution = await self.resolver.refresh()
        except WriterClosed:
            return
        except Exception:
            logger.exception("resolver refresh failed")

    async def _tick_loop(self) -> None:
        while True:
            await self.clock.sleep(self._tick)
            self.gate.refresh()
            self._mark_dirty()

    def _window_of(self, key: str) -> str | None:
        """The resolver's window of a live session (what a focus change is matched against)."""
        row = self.view.sessions.get(key)
        return None if row is None else row.window_id

    async def _load_hosts(self) -> HostData:
        """Host data for a refresh; nothing is read while no session needs placing."""
        if not self.view.sessions:
            return HostData(home=self._home)
        return await self._host_loader()

    # -- reading ----------------------------------------------------------------------------

    def collector_states(self) -> tuple[CollectorState, ...]:
        """Every collector the registry knows, with its current health (gate and supervisor)."""
        supervised = self.supervisor.health()
        states: list[CollectorState] = []
        for item in self.registry:
            health = self._health_of(item, supervised)
            plugin = item.plugin
            states.append(
                CollectorState(
                    name=item.name,
                    origin=item.entry.origin,
                    enabled=item.enabled,
                    capabilities=frozenset() if plugin is None else plugin.capabilities,
                    health=health,
                )
            )
        return tuple(states)

    def _health_of(
        self, item: RegisteredCollector, supervised: Mapping[str, CollectorHealth]
    ) -> CollectorHealth:
        if item.rejected:
            report = item.health
            return CollectorHealth(
                item.name,
                HealthState.FAILED,
                since=self._created_at,
                last_error_kind=None if report is None else report.error_kind,
                detail=None if report is None else report.detail,
            )
        merged = self.gate.merged_health(item.name)
        if merged is not None:
            return merged
        return supervised.get(item.name) or CollectorHealth(
            item.name, HealthState.DISABLED, since=self._created_at
        )

    def health_reports(self) -> dict[str, HealthReport]:
        """Current health per collector, in the shape ``collectors list`` takes. A rejected
        duplicate carries its own report in the registry and is left out here."""
        now = self.clock.now()
        return {
            state.name: state.health.to_report(now)
            for item, state in zip(self.registry, self.collector_states(), strict=True)
            if not item.rejected
        }

    def collector_rows(self) -> list[CollectorRow]:
        """The ``collectors list`` rows (WP1.2): registry metadata with live health."""
        return list_collectors(self.config, registry=self.registry, health=self.health_reports())

    def state(self) -> StateView:
        """What ``/now`` is built from. Call it on the event loop thread."""
        snapshot = self.view.snapshot()
        resolution = self._resolution
        return StateView(
            generated_at=self.clock.now(),
            live=snapshot,
            collectors=self.collector_states(),
            sessions=_extras(self.memory, snapshot.sessions.keys()),
            placements={} if resolution is None else resolution.sessions,
            projects={} if resolution is None else resolution.projects,
            labels={}
            if resolution is None
            else {name: label.text for name, label in resolution.labels.items()},
        )


def _extras(memory: SessionMemory, keys: Iterable[str]) -> dict[str, SessionExtras]:
    found: dict[str, SessionExtras] = {}
    for key in keys:
        extras = memory.get(key)
        if extras is not None:
            found[key] = extras
    return found
