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
* the API reads :meth:`Daemon.state` and never touches SQLite. Its one write besides the
  resolver's are a message receipt (:attr:`Daemon.messages`) and the todo writes
  (:attr:`Daemon.todos`), which also go through the writer;
* the **summary service** (:attr:`Daemon.summaries`, WP8.1) is the only part that calls a model.
  It is off by default; its results go through the writer too;
* the **notifier** (:attr:`Daemon.notifier`, docs/decisions/0055) sends attention alerts through a
  configured command. It exists only while ``[notify] enabled = true``, runs on its own task and
  reads the same ``waiting[]`` list ``/now`` shows (:meth:`Daemon._attention`); every live-view
  update and every tick wake it;
* the **span window** (:attr:`Daemon.span_window`) holds the focus spans of the last 14 days in
  memory: read once at start, then fed by the span engine. :meth:`Daemon.activity` builds working
  time from it (docs/decisions/0050);
* the **rankers** (:attr:`Daemon.rankers`) are read from the ``fylgja.rankers`` entry points by
  the collector enable rule (docs/decisions/0052). :meth:`Daemon.ranked` orders the navigation
  items (docs/decisions/0051) with one of them, memoized, and falls back to the ``natural`` floor
  when the ranker fails. The daemon knows no ranker by name: only ``[items] default_ranker`` from
  the configuration and the reserved floor.

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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
from fylgja.core import hosts, spans
from fylgja.core.activity import Activity, build_activity
from fylgja.core.context_items import ContextEntry, ContextMemory
from fylgja.core.hosts import HostData
from fylgja.core.ingest import IngestGate, PluginSink
from fylgja.core.items import Item
from fylgja.core.labels import LabelService
from fylgja.core.live_view import Collection, LiveSnapshot, LiveUpdate, LiveView
from fylgja.core.next_lists import NextLists
from fylgja.core.notify import Notifier, NotifyStats, Snapshot, candidates, on_screen
from fylgja.core.project_roots import ProjectLocator
from fylgja.core.resolver import Placement, ProjectPlacement, Resolution, Resolver
from fylgja.core.retention import MaintenanceJob
from fylgja.core.sessions import HOOK_ONLY_STATES, SessionStateEngine
from fylgja.core.shown import shown_sessions, shown_waiting
from fylgja.core.span_window import SpanWindow
from fylgja.core.summaries import ModelGate, SummaryRunner, SummaryService
from fylgja.core.system import LocalFileSystem, SubprocessRunner
from fylgja.core.todos import TodoService
from fylgja.core.usage_view import UsageMemory, UsageMeta
from fylgja.core.waiting import start_of_day
from fylgja.core.window_folders import WindowFolders
from fylgja.core.writer import Writer, WriterClosed, session_key, split_session_key
from fylgja.messaging.service import MessageService, WriterReceipts
from fylgja.models import (
    HealthReport,
    HealthState,
    HookEvent,
    Record,
    SessionActivityEvent,
    SessionActivitySnapshot,
    SessionEvent,
    SessionPayload,
    SessionSnapshot,
)
from fylgja.privacy import PrivacyFilter
from fylgja.rankers.base import FLOOR_NAME, Ranked, RankInput, natural_key
from fylgja.rankers.registry import EntryPointSource as RankerEntryPoints
from fylgja.rankers.registry import (
    Ranking,
    RegisteredRanker,
    floor_ranking,
    load_rankers,
    run_ranker,
)
from fylgja.store import db, queries
from fylgja.store.rows import DaySessionRow, HealthRow, TodoRow

__all__ = [
    "AVAILABLE_STATES",
    "RANK_STEP",
    "CollectorState",
    "Daemon",
    "HostLoader",
    "ItemBuilder",
    "RankResult",
    "SessionExtras",
    "SessionMemory",
    "StateView",
    "floor_time",
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

_END_OF_TIME: Final = datetime.max.replace(tzinfo=UTC)
"""The ``until`` of a read with no upper bound."""

RANK_STEP: Final = 5
"""Seconds the time of a ranking is floored to: a ranker gets that time, and every request within
one step (with no change of the live view or the span window) gets the same answer."""


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
    contexts: Mapping[str, tuple[ContextEntry, ...]] = field(default_factory=dict)
    """The context items of each open window (WP5.3), by window id."""
    usage: UsageMeta = field(default_factory=UsageMeta)
    """When each usage row was last observed and why an attempt failed (§4.7)."""
    touched: Mapping[str, datetime] = field(default_factory=dict)
    """When each project last had focus, by project path (focus spans of the last day; WP6.1)."""
    focus_today: Mapping[str, float] = field(default_factory=dict)
    """Seconds of focus today per workspace (as of the latest resolver refresh; WP6.2)."""
    next_lists: NextLists | None = None
    """Where the ranked "what next" lists get their ids and are logged (WP6.2). The one stateful
    part of the view: without it ``/now`` has no ``next[]``."""
    summaries: SummaryService | None = None
    """The cached summaries (WP8.1): ``sessions[].story`` and ``workspaces[].story`` come from
    here while summaries are enabled."""


# --------------------------------------------------------------------------------------------
# What a ranking is built from and what it answers
# --------------------------------------------------------------------------------------------

ItemBuilder = Callable[[datetime], tuple[Sequence[Item], Sequence[str]]]
"""Builds the navigation items and the workspace names of the current picture as of the given
(floored) time. The API supplies it (``fylgja.api.deps.item_builder`` over
``fylgja.api.item_inputs.build_items``), so the daemon imports no API module."""


def floor_time(moment: datetime, step: int = RANK_STEP) -> datetime:
    """``moment`` floored to a whole multiple of ``step`` seconds since the epoch (UTC)."""
    seconds = int(moment.timestamp() // step) * step
    return datetime.fromtimestamp(seconds, UTC)


@dataclass(frozen=True)
class RankResult:
    """The navigation items in rank order, as one ranker answered (:meth:`Daemon.ranked`)."""

    ranker: str
    """The ranker that was asked for: a loaded ranker or the ``natural`` floor."""
    data: RankInput
    """What the ranker got: ``data.now`` is the floored time of the answer."""
    ranked: tuple[Ranked, ...]
    """Best first; ``ranked[i]`` belongs to ``items[i]``."""
    items: tuple[Item, ...]
    workspace_order: tuple[str, ...]
    """The workspace names in the ranker's order (a permutation of ``data.workspaces``)."""
    fallback: str | None = None
    """``natural`` when the ranker failed and the floor answered instead."""

    @property
    def now(self) -> datetime:
        return self.data.now

    @property
    def activity(self) -> Activity:
        return self.data.activity


_RankKey = tuple[int, int, int, datetime]
"""``(days, live view version, span window version, floored now)``."""


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
        self._terminal: dict[str, str] = {}

    def terminal_id(self, key: str, name: str) -> str | None:
        """The terminal id ``name`` (``ITERM_SESSION_ID``, ...) a hook event of ``key`` carried."""
        return self._terminal.get(f"{key}\0{name}")

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
        elif isinstance(record, HookEvent) and record.payload.ids:
            key = self._key(record.entity_id, record.source)
            for name, value in record.payload.ids.items():
                self._terminal[f"{key}\0{name}"] = value

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
        for stale in [k for k in self._terminal if k.startswith(f"{key}\0")]:
            del self._terminal[stale]

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

    Every seam can be replaced: ``entry_points`` (the registry's source), ``ranker_entry_points``
    (the ranker registry's source), ``clock``, ``database``, ``fs`` and ``runner`` (what the
    resolver and host adapters read with), ``host_loader``, ``policy`` and ``rng`` (the
    supervisor's backoff), the resolver's ``resolve_debounce``, the ``tick`` interval and the
    ``summary_runner`` (the model call of the summary service).
    """

    def __init__(
        self,
        config: Config,
        *,
        entry_points: EntryPointSource | None = None,
        ranker_entry_points: RankerEntryPoints | None = None,
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
        summary_runner: SummaryRunner | None = None,
    ) -> None:
        self.config = config
        self.clock: Clock = SystemClock() if clock is None else clock
        self._debounce = resolve_debounce
        self._tick = tick
        self._home = str(Path.home()) if home is None else home
        filesystem: FileSystem = LocalFileSystem() if fs is None else fs
        self.fs: FileSystem = filesystem
        """The filesystem seam of the host adapters, the locator and (later) file excerpts."""
        process_runner: ProcessRunner = SubprocessRunner() if runner is None else runner
        self.runner: ProcessRunner = process_runner
        """The process runner of the host adapters and of the focus actions."""
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
        self.contexts = ContextMemory()
        self.locator = ProjectLocator(filesystem)
        """Git roots and draupnir groups, shared by the resolver and the window folders."""
        self.window_folders = WindowFolders(self.contexts, self.locator)
        """The git root of an IDE or terminal window's folder (docs/decisions/0049)."""
        self.usage = UsageMemory()
        self.session_state = SessionStateEngine(self.writer.submit, self._window_of)
        self.resolver = Resolver(
            self.writer,
            self.view,
            hosts=self._load_hosts,
            locator=self.locator,
            clock=self.clock,
            focus_time=self.focus_time,
        )
        self.spans = spans.SpanEngine(
            self.writer.enqueue,
            self._attribute,
            idle_threshold_seconds=config.spans.idle_threshold_seconds,
        )
        """Focus spans (WP5.2): derived from focus, idle and lock events, written by the writer."""
        self.span_window = SpanWindow(
            session_break=timedelta(minutes=config.items.work_session_break_minutes)
        )
        """The focus spans of the last 14 days in memory, loaded at start and kept current from
        the span engine (docs/decisions/0050)."""
        self.spans.listeners.append(self.span_window.apply)
        self.spans.on_away = self.view.touch  # clients refresh what "away" changes
        self.resolver.on_ids_changed = self._mark_dirty
        self.contexts.on_change = self.view.touch
        self._touched: dict[str, datetime] = {}
        self._focus_today: dict[str, float] = {}
        self.privacy = PrivacyFilter.from_config(config.privacy)
        """The ``[privacy]`` filter of the ingest gate; ``GET /inspect/collectors`` redacts the
        collector settings with it too (docs/decisions/0048)."""
        self.gate = IngestGate(
            _Tap(
                self.writer,
                (
                    self.resolver.observe,
                    self.memory.observe,
                    self.spans.observe,
                    self.contexts.observe,
                    self.usage.observe,
                ),
                (self.session_state.apply,),
            ),
            [item.plugin for item in self.registry if item.runnable and item.plugin is not None],
            privacy=self.privacy,
            clock=self.clock,
            on_private_focus=self.spans.private_focus,
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
        self.messages = MessageService.from_config(
            config,
            receipts=WriterReceipts(self.writer),
            clock=self.clock,
            runner=process_runner,
            fs=LocalFileSystem(),
        )
        """The opt-in message action (WP3.6); ``[actions.message] enabled = false`` by default.
        It reads the live session registry itself, right before each send."""
        self.todos = TodoService(
            self.writer,
            self.view,
            clock=self.clock,
            privacy=PrivacyFilter.from_config(config.privacy),
        )
        """Fylgja's own todos (WP4.1); every write goes through the writer."""
        self.labels = LabelService(
            self.writer, clock=self.clock, privacy=PrivacyFilter.from_config(config.privacy)
        )
        """Manual workspace labels (WP4.2); every write goes through the writer."""
        self.next_lists = NextLists(self.writer, clock=self.clock)
        """The ranked "what next" lists: ids, ``suggestions_log`` and feedback (WP6.2)."""
        self.model_gate = ModelGate(
            config.summaries.max_concurrent, clock=self.clock, runner=summary_runner
        )
        """The model runner and its ``[summaries] max_concurrent`` slots, shared by the summary
        service and the journal. Always built; it does not look at ``[summaries] enabled``."""
        self.summaries = SummaryService(
            self.writer,
            config.summaries,
            clock=self.clock,
            gate=self.model_gate,
            privacy=PrivacyFilter.from_config(config.privacy),
            on_change=self.view.touch,
        )
        """Session and workspace summaries by a local model (WP8.1); off unless
        ``[summaries] enabled = true``. The only place a model is called."""
        self.notifier: Notifier | None = (
            Notifier(
                config.notify,
                runner=process_runner,
                clock=self.clock,
                port=config.api.port,
                read=self._attention,
                debounce=resolve_debounce,
            )
            if config.notify.enabled
            else None
        )
        """Attention alerts (docs/decisions/0055); built only while ``[notify] enabled = true``,
        so a disabled daemon starts no task for them and never runs their command."""
        self.rankers: tuple[RegisteredRanker, ...] = load_rankers(config, ranker_entry_points)
        """Every discovered ranker after the enable rule (docs/decisions/0052); only the enabled
        ones were imported."""
        self._loaded_rankers: dict[str, RegisteredRanker] = {
            item.name: item for item in self.rankers if item.loaded
        }
        self._rank_input: tuple[_RankKey, RankInput] | None = None
        self._ranked: dict[str, tuple[_RankKey, RankResult]] = {}
        """One memo entry per ranker name."""

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
    def home(self) -> str:
        """The home directory the host adapters and the inspect routes use."""
        return self._home

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
            now = self.clock.now()
            await self.writer.execute(lambda tx: spans.recover_stale(tx, now))
            await self.span_window.load(
                self._read_spans_since, now, read_before=self._read_spans_before
            )
            self._rebuild_view()
            await self.summaries.load()
            stored = [row for row in self.view.sessions.values() if row.state in HOOK_ONLY_STATES]
            hooked = await asyncio.to_thread(
                self._read_hooked_keys, [row.session_key for row in stored]
            )
            self.session_state.seed(
                (row.session_key, row.source, row.state, row.state_since, row.waiting_reason)
                for row in stored
                if row.session_key in hooked
            )
            self.lifecycle.append("start:live_view")
            self._unsubscribe = self.view.subscribe(self._on_update)
            self._mark_dirty()  # place what the store already holds
            self._tasks = [
                asyncio.create_task(self._engine_loop(), name="fylgja-state-engine"),
                asyncio.create_task(self._tick_loop(), name="fylgja-tick"),
                asyncio.create_task(self.maintenance.run(), name="fylgja-maintenance"),
            ]
            if self.notifier is not None:
                self._tasks.append(asyncio.create_task(self.notifier.run(), name="fylgja-notify"))
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
        configured = self.config.items.default_ranker
        if self.effective_default_ranker != configured:
            logger.warning(
                "the default ranker %s is not loaded; ranking with the %s floor",
                configured,
                FLOOR_NAME,
            )

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
        await self.summaries.stop()  # a running model call is killed, nothing more is queued
        if "start:writer" in self.lifecycle:
            self.spans.stop(self.clock.now())  # the open span ends with the daemon
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
            if change.collection is Collection.WINDOWS and change.row is None:
                self.contexts.forget_window(change.key)
            if change.collection is Collection.SESSIONS and change.row is None:
                self.memory.forget(change.key)
                self.resolver.forget(change.key)
                self.session_state.forget(change.key)
        if update.collections & _ENGINE_TRIGGERS:
            self._mark_dirty()
        if self.notifier is not None:  # also focus changes and away transitions (view.touch)
            self.notifier.wake()

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
        last = self.clock.now()
        limit = timedelta(seconds=self._tick + self.config.spans.idle_threshold_seconds)
        while True:
            await self.clock.sleep(self._tick)
            now = self.clock.now()
            if now - last > limit:
                # The wall clock jumped further than a tick plus the idle threshold: the machine
                # slept. Sleep is not focus time (docs/decisions/0049).
                self.spans.gap(last, now)
            last = now
            self.gate.refresh()
            self._mark_dirty()
            if self.notifier is not None:  # a private-window focus reaches it only here
                self.notifier.wake()

    def _attention(self) -> Snapshot:
        """What the notifier evaluates (R1, R5, R8): the ``waiting[]`` list ``/now`` shows, the
        families in the focused window and whether the person is away.

        The focused window is the span tracker's, which is current: a privacy-ignored window
        leaves it empty, and while the person is idle or the screen is locked nothing is on
        screen."""
        state = self.state()
        sessions = shown_sessions(state)
        tracker = self.spans.tracker
        away = bool(tracker.suspended)
        focused = tracker.focused
        window = None if away or focused is None else focused.window_id
        return Snapshot(
            now=self.clock.monotonic(),
            wall_now=self.clock.now(),
            candidates=candidates(shown_waiting(state) or (), sessions),
            on_screen=on_screen(window, sessions),
            away=away,
        )

    def notify_stats(self) -> NotifyStats:
        """The notifier's counters; the zero default while alerts are off."""
        return NotifyStats() if self.notifier is None else self.notifier.stats()

    def _window_of(self, key: str) -> str | None:
        """The resolver's window of a live session (what a focus change is matched against)."""
        row = self.view.sessions.get(key)
        return None if row is None else row.window_id

    def _attribute(self, window_id: str) -> tuple[str | None, str | None]:
        """The project and session of a focused window.

        The resolver's placements come first: the live session in that window that was active
        most recently gives its project and its key. A window no session is placed in falls back
        to the git root of the folder its context names (an IDE project, a terminal tab's cwd),
        with no session (docs/decisions/0049). ``(None, None)`` when neither is known."""
        here = [
            row
            for row in self.view.sessions.values()
            if row.window_id == window_id and row.ended_at is None
        ]
        if not here:
            return self.window_folders.folder_of(window_id), None
        newest = max(here, key=lambda row: (row.state_since or row.first_seen, row.session_key))
        return newest.project_path, newest.session_key

    async def focus_time(self) -> Mapping[str, Mapping[str, float]]:
        """Seconds of focus per project and workspace over the last day (the resolver's input)."""
        now = self.clock.now()
        since = now - timedelta(days=1)
        found = await asyncio.to_thread(self._read_spans, since, now)
        self._set_touched(spans.last_focus_by_project(found, now))
        self._set_focus_today(spans.focus_seconds_by_workspace(found, start_of_day(now), now, now))
        return spans.focus_time_by_project(found, since, now, now)

    def _set_touched(self, touched: dict[str, datetime]) -> None:
        """Remember when each project last had focus; a project newly touched changes what
        waiting-on-me shows, so clients are told."""
        newly = touched.keys() - self._touched.keys()
        self._touched = touched
        if newly:
            self.view.touch()

    def _set_focus_today(self, seconds: dict[str, float]) -> None:
        """Remember the focus seconds of today per workspace; a workspace newly in the list can
        change the ranking's way back (rule 5), so clients are told."""
        newly = seconds.keys() - self._focus_today.keys()
        self._focus_today = seconds
        if newly:
            self.view.touch()

    def _read_hooked_keys(self, keys: Sequence[str]) -> set[str]:
        """Those of ``keys`` that have a stored hook event (a worker thread, as the span reads)."""
        if not keys:
            return set()
        connection = db.connect_reader(self.writer.database)
        try:
            return queries.hooked_session_keys(connection, keys)
        finally:
            connection.close()

    def _read_spans(self, since: datetime, until: datetime) -> list[spans.Span]:
        connection = db.connect_reader(self.writer.database)
        try:
            return queries.load_spans(
                connection, since=db.to_db_time(since), until=db.to_db_time(until)
            )
        finally:
            connection.close()

    def _read_spans_since(self, since: datetime) -> list[spans.Span]:
        """The spans that ended after ``since`` or are still open (the span window's load)."""
        connection = db.connect_reader(self.writer.database)
        try:
            return queries.load_spans(
                connection, since=db.to_db_time(since), until=db.to_db_time(_END_OF_TIME)
            )
        finally:
            connection.close()

    def _read_spans_before(self, before: datetime, limit: int) -> list[spans.Span]:
        """At most ``limit`` spans that ended at or before ``before``, newest first."""
        connection = db.connect_reader(self.writer.database)
        try:
            return queries.load_spans_before(connection, before=db.to_db_time(before), limit=limit)
        finally:
            connection.close()

    def activity(self, now: datetime | None = None) -> Activity:
        """Working time as of ``now`` (default: the clock's now): work sessions, the away gap and
        active seconds, from the span window (docs/decisions/0050). Reads nothing; call it on the
        event loop thread."""
        moment = self.clock.now() if now is None else now
        return build_activity(
            self.span_window.view(moment),
            now=moment,
            suspended=bool(self.spans.tracker.suspended),
            idle_threshold=timedelta(seconds=self.config.spans.idle_threshold_seconds),
            session_break=timedelta(minutes=self.config.items.work_session_break_minutes),
        )

    # -- ranking ----------------------------------------------------------------------------

    @property
    def loaded_rankers(self) -> tuple[str, ...]:
        """The names of the rankers that can rank, in :attr:`rankers` order."""
        return tuple(self._loaded_rankers)

    @property
    def effective_default_ranker(self) -> str:
        """``[items] default_ranker`` when that ranker is loaded, else the ``natural`` floor."""
        configured = self.config.items.default_ranker
        return configured if configured in self._loaded_rankers else FLOOR_NAME

    def can_rank(self, name: str) -> bool:
        """``name`` is a loaded ranker or the ``natural`` floor."""
        return name == FLOOR_NAME or name in self._loaded_rankers

    def _key(self, days: int) -> _RankKey:
        moment = floor_time(self.clock.now())
        return (days, self.view.version, self.span_window.version, moment)

    def rank_input(self, build: ItemBuilder, *, days: int) -> RankInput:
        """What every ranker gets for the current picture, built once per key.

        The key is ``(days, live view version, span window version, floor(now, 5 s))``, and the
        floored time is what ``build`` and :meth:`activity` are given, so equal keys mean equal
        inputs. ``build`` is called only when the key changed. Call it on the event loop thread.
        """
        key = self._key(days)
        memo = self._rank_input
        if memo is not None and memo[0] == key:
            return memo[1]
        moment = key[3]
        items, workspaces = build(moment)
        data = RankInput(
            now=moment,
            items=tuple(items),
            activity=self.activity(moment),
            workspaces=tuple(sorted(set(workspaces), key=natural_key)),
        )
        self._rank_input = (key, data)
        return data

    def ranked(self, name: str, build: ItemBuilder, *, days: int) -> RankResult:
        """The items of the current picture in the order of ranker ``name``, memoized.

        ``name`` is a loaded ranker or the ``natural`` floor (:meth:`can_rank`; anything else is
        a :class:`LookupError`). The memo holds one answer per ranker name under the key of
        :meth:`rank_input`. A ranker that raises or answers wrongly (another key set, a number that
        is not finite, a workspace order that is not a permutation) is logged by name and the
        floor answers instead, with ``fallback = "natural"``.
        """
        if not self.can_rank(name):
            raise LookupError(f"ranker {name!r} is not loaded")
        key = self._key(days)
        memo = self._ranked.get(name)
        if memo is not None and memo[0] == key:
            return memo[1]
        data = self.rank_input(build, days=days)
        ranker = self._loaded_rankers.get(name)
        ranking: Ranking = floor_ranking(data) if ranker is None else run_ranker(ranker, data)
        if ranking.failure is not None:
            logger.warning(
                "ranker %s failed (%s); answered with the %s floor",
                name,
                ranking.failure,
                FLOOR_NAME,
            )
        by_key = {item.key: item for item in data.items}
        result = RankResult(
            ranker=name,
            data=data,
            ranked=ranking.ranked,
            items=tuple(by_key[row.key] for row in ranking.ranked),
            workspace_order=ranking.workspace_order,
            fallback=None if ranking.failure is None else FLOOR_NAME,
        )
        self._ranked[name] = (key, result)
        return result

    def _read_history(self, since: datetime, until: datetime) -> spans.History:
        connection = db.connect_reader(self.writer.database)
        try:
            start, stop = db.to_db_time(since), db.to_db_time(until)
            found = queries.load_spans(connection, since=start, until=stop)
            worked_on = queries.load_worked_on(connection, since=start, until=stop)
        finally:
            connection.close()
        summary = spans.summarize(found, since, until, self.clock.now())
        return spans.History(summary, tuple(worked_on))

    async def history(self, since: datetime, until: datetime) -> spans.History:
        """Focus spans and totals over ``[since, until)`` and the projects the prompt history
        marked as worked on (``GET /history``). Reads the store; open spans run to now."""
        return await asyncio.to_thread(self._read_history, since, until)

    def _read_day(
        self, since: datetime, until: datetime
    ) -> tuple[list[DaySessionRow], list[TodoRow]]:
        connection = db.connect_reader(self.writer.database)
        try:
            start, stop = db.to_db_time(since), db.to_db_time(until)
            return (
                queries.load_day_sessions(connection, since=start, until=stop),
                queries.load_day_todos(connection, since=start, until=stop),
            )
        finally:
            connection.close()

    async def day_activity(
        self, since: datetime, until: datetime
    ) -> tuple[list[DaySessionRow], list[TodoRow]]:
        """The sessions active in ``[since, until)`` with their one-liners, and the todos created
        or finished in it (the inputs of the daily narrative, WP8.2). Reads the store."""
        return await asyncio.to_thread(self._read_day, since, until)

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

    def _touched_now(self) -> dict[str, datetime]:
        """The projects' last focus, with the project of the open focus span as of now."""
        touched = dict(self._touched)
        open_span = self.spans.tracker.open_span
        if open_span is not None and open_span.project_path is not None:
            touched[open_span.project_path] = self.clock.now()
        return touched

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
            contexts=self.contexts.by_window(snapshot.windows.keys()),
            usage=self.usage.meta(),
            touched=self._touched_now(),
            focus_today=self._focus_today,
            next_lists=self.next_lists,
            summaries=self.summaries,
        )


def _extras(memory: SessionMemory, keys: Iterable[str]) -> dict[str, SessionExtras]:
    found: dict[str, SessionExtras] = {}
    for key in keys:
        extras = memory.get(key)
        if extras is not None:
            found[key] = extras
    return found
