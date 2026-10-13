"""The ingest gate in front of the writer (``PLAN-final-plan.md`` §3.1, §3.3, §4.4; ``PLAN.md``
§4.1, §4.9).

Every record a collector emits passes, in order:

1. **validation** — the record is dumped and validated again against the record union, so a
   record built with ``model_construct``, mutated after validation, or not a record at all is
   rejected (``schema``);
2. ``source`` must equal the plugin's name (``source``);
3. ``entity_kind`` must be allowed by the plugin's declared capabilities (``entity_kind``);
4. ``observed_at`` must lie in [now − 30 d, now + 5 min], both ends included (``timestamp``);
5. the **privacy filter** (:mod:`fylgja.privacy`) drops ignored apps and titles and redacts text.
   A dropped *focus* event still tells the focus-span engine that focus left the previous window, at
   what time and nothing else (``on_private_focus``), so time in an ignored app is never counted for
   the window before it;
6. **dedup** happens in the writer: ``events.dedup_key`` is unique and a duplicate changes
   nothing.

Accepted records are counted per plugin, entity kind and record type, with the time of the last
one (:attr:`PluginStats.kinds`, :attr:`PluginStats.last_at`); ``GET /inspect/collectors`` shows them
(``docs/decisions/0048``). The counters live in memory and start again at zero with the daemon.

Rejections are counted per plugin and reason. More than 20 rejections within a minute mark the
plugin ``degraded`` (``last_error_kind = "ingest_rejections"``); it recovers once its last minute
holds no more than that. The gate merges this with the supervisor's health
(:meth:`IngestGate.observe_health`) and hands the result to the writer, which persists it in
``collector_health`` and the live view.

:meth:`IngestGate.sink` returns the :class:`~fylgja.collectors.base.RecordSink` a plugin's
``CollectorContext`` carries (pass it as the supervisor's ``sink_factory``). ``emit`` never
raises; ``emit_committed`` raises :class:`RecordRejected` for a rejected record and returns once
an accepted record is committed (or at once when the privacy filter dropped it).

No record content is ever logged.
"""

from __future__ import annotations

import logging
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Literal, NoReturn, Protocol

from pydantic import BaseModel

from fylgja.collectors.base import Clock, CollectorPlugin
from fylgja.collectors.context import SystemClock
from fylgja.collectors.supervisor import CollectorHealth
from fylgja.core.writer import WriterClosed
from fylgja.models import RECORD_ADAPTER, EntityKind, FocusEvent, HealthState, Record
from fylgja.privacy import PrivacyFilter
from fylgja.store.rows import HealthRow

__all__ = [
    "DEGRADED_ERROR_KIND",
    "IngestGate",
    "KindKey",
    "PluginSink",
    "PluginStats",
    "RecordRejected",
    "RecordType",
    "RecordWriter",
    "Rejection",
]

logger = logging.getLogger("fylgja.ingest")

DEGRADED_ERROR_KIND: Final = "ingest_rejections"
MAX_AGE: Final = timedelta(days=30)
MAX_SKEW: Final = timedelta(minutes=5)
REJECTION_LIMIT: Final = 20
REJECTION_WINDOW: Final = 60.0


class Rejection(StrEnum):
    SCHEMA = "schema"
    SOURCE = "source"
    ENTITY_KIND = "entity_kind"
    TIMESTAMP = "timestamp"


RecordType = Literal["event", "snapshot"]
KindKey = tuple[EntityKind, RecordType]
"""``(entity_kind, record_type)``; the record type is ``event`` or ``snapshot``."""


class RecordRejected(ValueError):
    """``emit_committed`` was given a record the gate rejects."""

    def __init__(self, plugin: str, reason: Rejection) -> None:
        super().__init__(f"record from {plugin!r} rejected: {reason.value}")
        self.plugin = plugin
        self.reason = reason


class RecordWriter(Protocol):
    """The slice of :class:`fylgja.core.writer.Writer` the gate uses."""

    def submit(self, record: Record, *, received_at: datetime | None = None) -> None: ...

    async def submit_committed(
        self, record: Record, *, received_at: datetime | None = None
    ) -> object: ...

    def save_health(self, row: HealthRow) -> None: ...


@dataclass
class PluginStats:
    accepted: int = 0
    dropped: int = 0
    """Records the privacy filter dropped (ignored app or title). Not rejections."""
    lost: int = 0
    """Accepted records ``emit`` could not queue because the writer was stopped."""
    rejected: Counter[Rejection] = field(default_factory=Counter)
    kinds: Counter[KindKey] = field(default_factory=Counter)
    """Accepted records per entity kind and record type, since the daemon started."""
    last_at: dict[KindKey, datetime] = field(default_factory=dict)
    """When each entity kind and record type was last accepted (the gate's clock)."""

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected.values())


class PluginSink:
    """The sink of one plugin. It satisfies :class:`~fylgja.collectors.base.RecordSink`."""

    def __init__(self, gate: IngestGate, name: str) -> None:
        self._gate = gate
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def emit(self, record: Record) -> None:
        self._gate.emit(self._name, record)

    async def emit_committed(self, record: Record) -> None:
        await self._gate.emit_committed(self._name, record)


class IngestGate:
    """Validation, privacy and rejection accounting in front of the writer."""

    def __init__(
        self,
        writer: RecordWriter,
        plugins: Iterable[CollectorPlugin] = (),
        *,
        privacy: PrivacyFilter | None = None,
        clock: Clock | None = None,
        max_age: timedelta = MAX_AGE,
        max_skew: timedelta = MAX_SKEW,
        rejection_limit: int = REJECTION_LIMIT,
        rejection_window: float = REJECTION_WINDOW,
        on_private_focus: Callable[[datetime], None] | None = None,
    ) -> None:
        self._writer = writer
        self._on_private_focus = on_private_focus
        self._privacy = PrivacyFilter() if privacy is None else privacy
        self._clock: Clock = SystemClock() if clock is None else clock
        self._max_age = max_age
        self._max_skew = max_skew
        self._limit = rejection_limit
        self._window = rejection_window
        self._allowed: dict[str, frozenset[EntityKind]] = {}
        self._stats: dict[str, PluginStats] = {}
        self._recent: dict[str, deque[float]] = {}
        self._degraded_since: dict[str, datetime] = {}
        self._health: dict[str, CollectorHealth] = {}
        for plugin in plugins:
            self.add_plugin(plugin)

    # -- plugins ------------------------------------------------------------------------------

    def add_plugin(self, plugin: CollectorPlugin) -> None:
        if plugin.name in self._allowed:
            raise ValueError(f"plugin {plugin.name!r} is already registered")
        self._allowed[plugin.name] = plugin.allowed_kinds
        self._stats[plugin.name] = PluginStats()
        self._recent[plugin.name] = deque()

    def sink(self, name: str) -> PluginSink:
        """The sink for plugin ``name``; usable as the supervisor's ``sink_factory``."""
        if name not in self._allowed:
            raise ValueError(f"unknown plugin {name!r}")
        return PluginSink(self, name)

    def stats(self, name: str) -> PluginStats:
        return self._stats[name]

    def all_stats(self) -> Mapping[str, PluginStats]:
        """Every registered plugin's counters, read-only (read on the event loop thread)."""
        return MappingProxyType(self._stats)

    def is_degraded(self, name: str) -> bool:
        return name in self._degraded_since

    # -- the gate -----------------------------------------------------------------------------

    def check(self, name: str, record: object) -> Record | None:
        """The record as it may be stored, ``None`` when the privacy filter drops it.

        Raises :class:`RecordRejected` (and counts the rejection) otherwise.
        """
        allowed = self._allowed.get(name)
        if allowed is None:
            raise ValueError(f"unknown plugin {name!r}")
        valid = self._validate(record)
        if valid is None:
            self._reject(name, Rejection.SCHEMA)
        elif valid.source != name:
            self._reject(name, Rejection.SOURCE)
        elif valid.entity_kind not in allowed:
            self._reject(name, Rejection.ENTITY_KIND)
        elif not self._in_window(valid.observed_at):
            self._reject(name, Rejection.TIMESTAMP)
        filtered = self._privacy.filter(valid)
        stats = self._stats[name]
        if filtered is None:
            stats.dropped += 1
            hook = self._on_private_focus
            if hook is not None and isinstance(valid, FocusEvent):
                self._tell_private_focus(hook, valid.observed_at)
        else:
            stats.accepted += 1
            key: KindKey = (filtered.entity_kind, filtered.record_type)
            stats.kinds[key] += 1
            stats.last_at[key] = self._clock.now()
        self._evaluate(name)
        return filtered

    @staticmethod
    def _tell_private_focus(hook: Callable[[datetime], None], at: datetime) -> None:
        try:
            hook(at)
        except Exception:
            logger.exception("private focus hook failed")

    def emit(self, name: str, record: object) -> None:
        """Gate and queue ``record`` without waiting. Never raises for a bad record."""
        try:
            accepted = self.check(name, record)
        except RecordRejected:
            return
        if accepted is None:
            return
        try:
            self._writer.submit(accepted, received_at=self._clock.now())
        except WriterClosed as exc:
            self._stats[name].lost += 1
            logger.warning("record from %s not queued: %s", name, type(exc).__name__)

    async def emit_committed(self, name: str, record: object) -> None:
        """Gate ``record`` and return once it is committed."""
        accepted = self.check(name, record)
        if accepted is None:
            return
        await self._writer.submit_committed(accepted, received_at=self._clock.now())

    def _validate(self, record: object) -> Record | None:
        try:
            if isinstance(record, BaseModel):
                data: Any = record.model_dump(mode="json", warnings="error")
            elif isinstance(record, dict):
                data = record
            else:
                return None
            return RECORD_ADAPTER.validate_python(data)
        except Exception:
            return None

    def _in_window(self, observed_at: datetime) -> bool:
        now = self._clock.now()
        return now - self._max_age <= observed_at <= now + self._max_skew

    # -- rejections and health ----------------------------------------------------------------

    def _reject(self, name: str, reason: Rejection) -> NoReturn:
        self._stats[name].rejected[reason] += 1
        self._recent[name].append(self._clock.monotonic())
        logger.debug("rejected a record from %s: %s", name, reason.value)
        self._evaluate(name)
        raise RecordRejected(name, reason)

    def refresh(self) -> None:
        """Re-evaluate every plugin's rejection window (lets a quiet plugin recover)."""
        for name in self._allowed:
            self._evaluate(name)

    def _evaluate(self, name: str) -> None:
        recent = self._recent[name]
        cutoff = self._clock.monotonic() - self._window
        while recent and recent[0] <= cutoff:
            recent.popleft()
        degraded = len(recent) > self._limit
        if degraded == (name in self._degraded_since):
            return
        if degraded:
            self._degraded_since[name] = self._clock.now()
            logger.warning(
                "collector %s degraded: more than %d rejected records in %.0f s",
                name,
                self._limit,
                self._window,
            )
        else:
            del self._degraded_since[name]
            logger.info("collector %s no longer rejected at a high rate", name)
        self._publish(name)

    def observe_health(self, health: CollectorHealth) -> None:
        """The supervisor's ``on_health`` callback: merge and persist."""
        self._health[health.name] = health
        self._publish(health.name)

    def merged_health(self, name: str) -> CollectorHealth | None:
        """The supervisor's health of ``name`` with the gate's degraded state applied."""
        base = self._health.get(name)
        since = self._degraded_since.get(name)
        if since is None:
            if base is None and name in self._allowed:
                return CollectorHealth(name, HealthState.OK, since=self._clock.now())
            return base
        detail = f"more than {self._limit} rejected records in {self._window:.0f} s"
        if base is None:
            return CollectorHealth(
                name,
                HealthState.DEGRADED,
                since=since,
                last_error_kind=DEGRADED_ERROR_KIND,
                detail=detail,
            )
        if base.state in (HealthState.OK, HealthState.STARTING):
            return replace(
                base,
                state=HealthState.DEGRADED,
                since=since,
                last_error_kind=DEGRADED_ERROR_KIND,
                detail=detail,
            )
        return base

    def _publish(self, name: str) -> None:
        health = self.merged_health(name)
        if health is None:
            return
        row = HealthRow(
            name=health.name,
            state=health.state.value,
            since=health.since,
            last_ok_at=health.last_ok_at,
            restarts=health.restarts,
            last_error_kind=health.last_error_kind,
        )
        try:
            self._writer.save_health(row)
        except WriterClosed as exc:
            logger.warning("health of %s not saved: %s", name, type(exc).__name__)
