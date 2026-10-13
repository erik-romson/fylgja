"""Builders of the ``fylgja.inspect.*`` answers (``PLAN-final-plan.md`` §4.2,
``docs/api/inspect.md``, ``docs/decisions/0048``).

Pure: no FastAPI import, no I/O. The routes in :mod:`fylgja.api.routes.inspect` hand in what the
daemon holds in memory (the registry, the collector states, the ingest gate's counters, the
configuration) plus the one small state file they read off the event loop.

Every collector is treated alike: nothing here names a collector.

**Redaction** (:func:`redact_settings`). A collector's validated settings are dumped as JSON, then
each value under a key that names a secret (:data:`SECRET_KEY`) becomes ``[redacted]``, every string
goes through the ``[privacy] redact_patterns`` filter, a string that starts with the home directory
has that prefix replaced by ``~``, and a setting without a value is left out. A health ``detail``
and a doctor reason take the same string rules.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, JsonValue

from fylgja.api.inspect_models import (
    AppleScriptSwitch,
    EventsInfo,
    FlowCount,
    GateTotals,
    HealthDetail,
    InspectCollector,
    InspectCollectors,
    InspectExplain,
    InspectPipeline,
    KindCount,
    LabelExplain,
    LiveViewInfo,
    PrivacyInfo,
    ProjectPlacementInfo,
    RankingRule,
    RecordCounts,
    SessionPlacementInfo,
    SummariesInfo,
    WriterInfo,
)
from fylgja.api.now_models import Now
from fylgja.collectors.appcontext.switches import SWITCHES, SwitchRecord
from fylgja.collectors.registry import Origin, RegisteredCollector
from fylgja.collectors.supervisor import CollectorHealth
from fylgja.config import Config
from fylgja.core.daemon import CollectorState
from fylgja.core.ingest import PluginStats, Rejection
from fylgja.core.live_view import Collection, LiveView
from fylgja.core.ranking import IDLE_WINDOW, MAX_ITEMS, SCORES
from fylgja.core.resolver import Resolution
from fylgja.core.summaries import SummaryService
from fylgja.core.writer import Writer
from fylgja.privacy import REDACTED, PrivacyFilter
from fylgja.rankers.base import natural_key

__all__ = [
    "SECRET_KEY",
    "build_collectors",
    "build_explain",
    "build_pipeline",
    "redact_settings",
]

SECRET_KEY: Final = re.compile(
    r"(?i)(^|_)(secret|token|password|passwd|key|auth|credential|cookie)s?($|_)"
)
"""A settings key whose value is never shown: ``api_token``, ``password``, ``keys``,
``auth_header``, …"""

Scrub = Callable[[str], str]


def _scrubber(privacy: PrivacyFilter, home: str) -> Scrub:
    root = home.rstrip("/")

    def scrub(text: str) -> str:
        if root and (text == root or text.startswith(f"{root}/")):
            text = "~" + text[len(root) :]
        return privacy.redact(text)

    return scrub


def _redact_table(table: Mapping[Any, object], scrub: Scrub) -> dict[str, JsonValue]:
    found: dict[str, JsonValue] = {}
    for key, item in table.items():
        if item is None:
            continue
        name = str(key)
        found[scrub(name)] = REDACTED if SECRET_KEY.search(name) else _redact(item, scrub)
    return found


def _redact(value: object, scrub: Scrub) -> JsonValue:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, Mapping):
        return _redact_table(value, scrub)
    if isinstance(value, list | tuple):
        return [_redact(item, scrub) for item in value if item is not None]
    return scrub(str(value))  # pragma: no cover - a JSON dump holds nothing else


def redact_settings(
    settings: BaseModel, *, privacy: PrivacyFilter, home: str
) -> dict[str, JsonValue]:
    """``settings`` as JSON, redacted by the rules in the module docstring."""
    return _redact_table(settings.model_dump(mode="json"), _scrubber(privacy, home))


def _health(health: CollectorHealth, scrub: Scrub) -> HealthDetail:
    return HealthDetail(
        state=health.state,
        since=health.since,
        last_ok_at=health.last_ok_at,
        restarts=health.restarts,
        last_error_kind=health.last_error_kind,
        detail=None if health.detail is None else scrub(health.detail),
        retry_in_seconds=health.retry_in,
    )


def _records(stats: PluginStats) -> RecordCounts:
    kinds = [
        KindCount(
            entity_kind=kind.value,
            record_type=record_type,
            accepted=count,
            last_at=stats.last_at.get((kind, record_type)),
        )
        for (kind, record_type), count in sorted(stats.kinds.items())
    ]
    return RecordCounts(
        accepted=stats.accepted,
        dropped=stats.dropped,
        lost=stats.lost,
        rejected={reason.value: stats.rejected[reason] for reason in Rejection},
        kinds=kinds,
        last_at=max(stats.last_at.values(), default=None),
    )


def _collector(
    item: RegisteredCollector,
    state: CollectorState,
    stats: Mapping[str, PluginStats],
    *,
    privacy: PrivacyFilter,
    home: str,
    scrub: Scrub,
) -> InspectCollector:
    entry = item.entry
    plugin = item.plugin
    shown: Literal["enabled", "disabled", "rejected"]
    if item.rejected:
        shown = "rejected"
    else:
        shown = "enabled" if item.enabled else "disabled"
    counts = stats.get(item.name) if item.runnable else None
    return InspectCollector(
        name=item.name,
        origin="built-in" if entry.origin is Origin.BUILT_IN else "external",
        distribution=entry.distribution,
        distribution_version=entry.version,
        state=shown,
        enabled=item.enabled,
        loaded=item.loaded,
        api_version=None if plugin is None else plugin.api_major,
        capabilities=None if plugin is None else sorted(c.value for c in plugin.capabilities),
        entity_kinds=None if plugin is None else sorted(k.value for k in plugin.allowed_kinds),
        health=_health(state.health, scrub),
        settings=(
            None
            if item.settings is None
            else redact_settings(item.settings, privacy=privacy, home=home)
        ),
        records=None if counts is None else _records(counts),
    )


def _switch(
    name: str, configured: bool, record: SwitchRecord | None, scrub: Scrub
) -> AppleScriptSwitch:
    if record is None:
        return AppleScriptSwitch(name=name, configured=configured)
    return AppleScriptSwitch(
        name=name,
        configured=configured,
        doctor_ok=record.ok,
        doctor_at=record.at,
        doctor_reason=None if record.reason is None else scrub(record.reason),
    )


def build_collectors(
    registry: Sequence[RegisteredCollector],
    states: Sequence[CollectorState],
    stats: Mapping[str, PluginStats],
    config: Config,
    *,
    privacy: PrivacyFilter,
    home: str,
    switch_records: Mapping[str, SwitchRecord],
    generated_at: datetime,
    started_at: datetime | None,
) -> InspectCollectors:
    """The ``fylgja.inspect.collectors/1`` answer.

    ``states`` are :meth:`~fylgja.core.daemon.Daemon.collector_states`, one per ``registry`` item
    in the same order (a rejected duplicate shares its name with the collector it lost to).
    ``stats`` are the ingest gate's counters by collector name; only a collector that runs has
    them. ``switch_records`` are doctor's recorded AppleScript checks, by switch name.
    """
    if len(states) != len(registry):
        raise ValueError("one collector state is needed per registry item")
    scrub = _scrubber(privacy, home)
    collectors = [
        _collector(item, state, stats, privacy=privacy, home=home, scrub=scrub)
        for item, state in zip(registry, states, strict=True)
    ]
    configured = config.actions.applescript.model_dump()
    applescript = [
        _switch(name, configured.get(name) is True, switch_records.get(name), scrub)
        for name in SWITCHES
    ]
    return InspectCollectors(
        generated_at=generated_at,
        started_at=started_at,
        collectors=collectors,
        not_installed=sorted(set(config.collectors) - {item.name for item in registry}),
        applescript=applescript,
    )


def _flows(stats: Mapping[str, PluginStats]) -> list[FlowCount]:
    found = [
        FlowCount(
            collector=name,
            entity_kind=kind.value,
            record_type=record_type,
            accepted=count,
            last_at=counts.last_at.get((kind, record_type)),
        )
        for name, counts in stats.items()
        for (kind, record_type), count in counts.kinds.items()
    ]
    return sorted(found, key=lambda flow: (flow.collector, flow.entity_kind, flow.record_type))


def _gate(stats: Mapping[str, PluginStats]) -> GateTotals:
    return GateTotals(
        accepted=sum(item.accepted for item in stats.values()),
        dropped=sum(item.dropped for item in stats.values()),
        lost=sum(item.lost for item in stats.values()),
        rejected={
            reason.value: sum(item.rejected[reason] for item in stats.values())
            for reason in Rejection
        },
    )


def _writer(writer: Writer) -> WriterInfo:
    sizes = list(writer.stats.batch_sizes)
    return WriterInfo(
        running=writer.running,
        pending=writer.pending,
        transactions=writer.stats.transactions,
        items=writer.stats.items,
        duplicates=writer.stats.duplicates,
        failed_items=writer.stats.failed_items,
        failed_transactions=writer.stats.failed_transactions,
        batch_max=max(sizes, default=0),
        batch_mean=sum(sizes) / len(sizes) if sizes else 0.0,
    )


def build_pipeline(
    stats: Mapping[str, PluginStats],
    writer: Writer,
    view: LiveView,
    summaries: SummaryService,
    config: Config,
    *,
    sse_clients: int,
    sse_max_clients: int,
    lifecycle: Sequence[str],
    generated_at: datetime,
    started_at: datetime | None,
) -> InspectPipeline:
    """The ``fylgja.inspect.pipeline/1`` answer.

    ``stats`` are the ingest gate's counters by collector name. ``sse_clients`` and
    ``sse_max_clients`` come from the route, because :mod:`fylgja.api.sse` imports FastAPI.
    Privacy lists are reported as counts only.
    """
    flows = _flows(stats)
    cached, jobs = summaries.counts()
    settings = config.summaries
    return InspectPipeline(
        generated_at=generated_at,
        started_at=started_at,
        lifecycle=list(lifecycle),
        collectors_running=len({flow.collector for flow in flows}),
        gate=_gate(stats),
        flows=flows,
        writer=_writer(writer),
        live_view=LiveViewInfo(
            version=view.version,
            rows={collection.value: len(view.table(collection)) for collection in Collection},
        ),
        events=EventsInfo(clients=sse_clients, max_clients=sse_max_clients),
        privacy=PrivacyInfo(
            redact_patterns=len(config.privacy.redact_patterns),
            ignore_bundle_ids=len(config.privacy.ignore_bundle_ids),
            ignore_title_patterns=len(config.privacy.ignore_title_patterns),
        ),
        summaries=SummariesInfo(
            enabled=summaries.enabled,
            ttl_seconds=settings.ttl_seconds,
            max_concurrent=settings.max_concurrent,
            running=summaries.running,
            calls=summaries.calls,
            cached=cached,
            jobs=jobs,
        ),
        messaging_enabled=config.actions.message.enabled,
    )


def build_explain(
    now: Now, resolution: Resolution | None, *, generated_at: datetime
) -> InspectExplain:
    """Why the page says what it says: label rules, resolver placements and the Next scores.

    ``resolution`` is ``None`` before the first resolution; the answer then has no ``rule`` and no
    placements, and ``resolved`` is ``False``. Labels follow the natural workspace order,
    projects their path and sessions their key.
    """
    labels = resolution.labels if resolution is not None else {}
    label_rows: list[LabelExplain] = []
    for workspace in sorted(now.workspaces or [], key=lambda w: natural_key(w.name)):
        inferred = labels.get(workspace.name)
        label_rows.append(
            LabelExplain(
                workspace=workspace.name,
                label=workspace.label,
                label_source=workspace.label_source,
                inferred_label=workspace.inferred_label,
                rule=None if inferred is None else inferred.rule,
            )
        )
    projects = [
        ProjectPlacementInfo(
            path=placement.path,
            name=placement.name,
            workspace=placement.workspace,
            via=placement.via,
            confidence=placement.confidence,
            sessions=placement.sessions,
            windows=placement.windows,
            unresolved=placement.unresolved,
        )
        for placement in sorted(
            resolution.projects.values() if resolution is not None else (),
            key=lambda p: p.path,
        )
    ]
    sessions = [
        SessionPlacementInfo(
            session_key=placement.session_key,
            workspace=placement.workspace,
            window_id=placement.window_id,
            via=placement.via,
            confidence=placement.confidence,
            project_via=placement.project_via,
            project_confidence=placement.project_confidence,
            unresolved=placement.unresolved,
            note=placement.note or None,
        )
        for placement in sorted(
            resolution.sessions.values() if resolution is not None else (),
            key=lambda p: p.session_key,
        )
    ]
    return InspectExplain(
        generated_at=generated_at,
        resolved=resolution is not None,
        labels=label_rows,
        projects=projects,
        sessions=sessions,
        ranking=[RankingRule(kind=kind, score=score) for kind, score in SCORES.items()],
        idle_window_seconds=IDLE_WINDOW.total_seconds(),
        max_items=MAX_ITEMS,
        next=list(now.next or []),
        waiting=list(now.waiting or []),
    )
