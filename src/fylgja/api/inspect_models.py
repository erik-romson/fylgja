"""The ``fylgja.inspect.*`` contracts: the models behind the ``GET /inspect/*`` read routes
(``PLAN-final-plan.md`` §4.2, ``docs/api/inspect.md``, ``docs/decisions/0048``).

An inspect answer is diagnostic: it shows how collection works right now (state, redacted
settings, what came in per record kind and when), read from the daemon's memory only. Each route
has its own schema string, ``fylgja.inspect.<name>/1``. ``/now`` (``fylgja.now/1``) and
``/collectors`` stay as they are.

The serialization rule of ``fylgja.now/1`` holds here too: every model is a
:class:`~fylgja.api.now_models.NowModel`, dumped with :func:`~fylgja.api.now_models.dump_json`, so a
field without a value is absent and ``null`` never appears.

This module imports only the standard library, Pydantic, :mod:`fylgja.models` and
:mod:`fylgja.api.now_models`.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import AwareDatetime, Field, JsonValue

from fylgja.api.now_models import LabelSource, NextItem, NowModel, Waiting
from fylgja.models import HealthState

__all__ = [
    "INSPECT_COLLECTORS_SCHEMA",
    "INSPECT_EXPLAIN_SCHEMA",
    "INSPECT_PIPELINE_SCHEMA",
    "AppleScriptSwitch",
    "EventsInfo",
    "FlowCount",
    "GateTotals",
    "HealthDetail",
    "InspectCollector",
    "InspectCollectors",
    "InspectExplain",
    "InspectPipeline",
    "KindCount",
    "LabelExplain",
    "LiveViewInfo",
    "PrivacyInfo",
    "ProjectPlacementInfo",
    "RankingRule",
    "RecordCounts",
    "SessionPlacementInfo",
    "SummariesInfo",
    "WriterInfo",
]

INSPECT_COLLECTORS_SCHEMA: Final = "fylgja.inspect.collectors/1"
"""The version string ``/inspect/collectors`` carries in ``schema``."""

INSPECT_PIPELINE_SCHEMA: Final = "fylgja.inspect.pipeline/1"
"""The version string ``/inspect/pipeline`` carries in ``schema``."""

INSPECT_EXPLAIN_SCHEMA: Final = "fylgja.inspect.explain/1"
"""The version string ``/inspect/explain`` carries in ``schema``."""


class HealthDetail(NowModel):
    """A collector's health as the daemon holds it: the supervisor's periodic ``doctor()`` probe,
    merged with the ingest gate's degraded state."""

    state: HealthState
    since: AwareDatetime | None = None
    """When the collector entered ``state``."""
    last_ok_at: AwareDatetime | None = None
    restarts: int = 0
    """Restarts by the supervisor since the daemon started."""
    last_error_kind: str | None = None
    detail: str | None = None
    """Redacted like the settings (see ``docs/api/inspect.md``)."""
    retry_in_seconds: float | None = None
    """Seconds until the next restart while the collector waits in backoff."""


class KindCount(NowModel):
    """Accepted records of one entity kind and record type."""

    entity_kind: str
    record_type: Literal["event", "snapshot"]
    accepted: int
    last_at: AwareDatetime | None = None
    """When the last one was accepted (the daemon's clock)."""


class RecordCounts(NowModel):
    """What the ingest gate counted for one collector since the daemon started."""

    accepted: int
    dropped: int
    """Dropped by the privacy filter (ignored app or title)."""
    lost: int
    """Accepted, but not queued because the writer was stopped."""
    rejected: dict[str, int]
    """Per gate reason: ``schema``, ``source``, ``entity_kind``, ``timestamp`` (every reason, zero
    included)."""
    kinds: list[KindCount]
    """Sorted by entity kind, then record type."""
    last_at: AwareDatetime | None = None
    """When the collector's last record was accepted; absent before the first one."""


class InspectCollector(NowModel):
    """One collector the registry knows."""

    name: str
    origin: Literal["built-in", "external"]
    distribution: str | None = None
    distribution_version: str | None = None
    state: Literal["enabled", "disabled", "rejected"]
    """``rejected``: a duplicate entry-point name, never loaded."""
    enabled: bool
    loaded: bool
    """Its code was imported (a disabled external is not)."""
    api_version: int | None = None
    capabilities: list[str] | None = None
    entity_kinds: list[str] | None = None
    """The entity kinds the ingest gate lets it emit."""
    health: HealthDetail
    settings: dict[str, JsonValue] | None = None
    """Its validated ``[collectors.<name>]`` settings, redacted (see ``docs/api/inspect.md``);
    absent when they were not validated."""
    records: RecordCounts | None = None
    """Absent for a collector that does not run."""


class AppleScriptSwitch(NowModel):
    """One ``[actions.applescript]`` switch and doctor's last recorded check of it."""

    name: str
    configured: bool
    """``[actions.applescript] <name>``."""
    doctor_ok: bool | None = None
    """Absent when ``fylgja doctor`` has not checked this switch."""
    doctor_at: AwareDatetime | None = None
    doctor_reason: str | None = None


class NotifyInfo(NowModel):
    """The attention-alert notifier's status (R18). It holds no argv: a command can carry a secret
    topic."""

    enabled: bool
    """``[notify] enabled``."""
    kinds: list[str]
    """The waiting kinds that alert."""
    grace_seconds: float
    reminder_seconds: float
    desk: bool
    """A ``desk_command`` is set."""
    away: bool
    """An ``away_command`` is set."""
    open: int
    """Episodes whose entry is still listed, announced or not."""
    sent: int
    """Sends whose command exited 0."""
    failed: int
    """Sends that timed out, found no command or failed."""
    last_failure_kind: str | None = None
    """``command_timeout``, ``command_missing`` or ``command_failed``; absent before a failure."""
    last_failure_at: AwareDatetime | None = None
    """The wall time of the last failed send."""
    evaluation_failures: int
    """Evaluations that raised; each one skipped its send."""
    last_evaluation_failure_at: AwareDatetime | None = None
    """The wall time of the last evaluation that raised; absent before one."""


class InspectCollectors(NowModel):
    """The answer of ``GET /inspect/collectors``."""

    schema_: Literal["fylgja.inspect.collectors/1"] = Field(
        default=INSPECT_COLLECTORS_SCHEMA, alias="schema"
    )
    """The contract version."""
    generated_at: AwareDatetime
    started_at: AwareDatetime | None = None
    """When the daemon started; the counters count from here."""
    collectors: list[InspectCollector]
    """In registry order: by name, then distribution."""
    not_installed: list[str]
    """``[collectors.<name>]`` tables with no entry point behind them, sorted."""
    applescript: list[AppleScriptSwitch]
    """Reported, never run: in the order doctor checks them."""
    notify: NotifyInfo
    """The alert notifier; not a collector, so it is no row of ``collectors``."""


class FlowCount(NowModel):
    """One cell of the collector × kind matrix: what the gate accepted."""

    collector: str
    entity_kind: str
    record_type: Literal["event", "snapshot"]
    accepted: int
    last_at: AwareDatetime | None = None
    """When the last one was accepted (the daemon's clock)."""


class GateTotals(NowModel):
    """The ingest gate's counters, summed over every collector."""

    accepted: int
    dropped: int
    """Dropped by the privacy filter (ignored app or title)."""
    lost: int
    """Accepted, but not queued because the writer was stopped."""
    rejected: dict[str, int]
    """Per gate reason (every reason, zero included)."""


class WriterInfo(NowModel):
    """The single writer: its queue and its transactions since the daemon started."""

    running: bool
    pending: int
    """Items queued, not yet in a transaction."""
    transactions: int
    items: int
    duplicates: int
    failed_items: int
    failed_transactions: int
    batch_max: int
    """The largest of the recent transactions' item counts; ``0`` when there are none."""
    batch_mean: float
    """The mean of the recent transactions' item counts; ``0`` when there are none."""


class LiveViewInfo(NowModel):
    """The in-memory live view."""

    version: int
    rows: dict[str, int]
    """Collection name → row count."""


class PrivacyInfo(NowModel):
    """The ``[privacy]`` configuration as counts only: a pattern can name a customer."""

    redact_patterns: int
    ignore_bundle_ids: int
    ignore_title_patterns: int


class SummariesInfo(NowModel):
    """The summary service."""

    enabled: bool
    ttl_seconds: int
    max_concurrent: int
    running: int
    """Jobs calling the model right now."""
    calls: int
    """Model calls started since the daemon started."""
    cached: int
    jobs: int
    """Jobs queued or running."""


class EventsInfo(NowModel):
    """The ``/events`` change stream."""

    clients: int
    max_clients: int


class InspectPipeline(NowModel):
    """The answer of ``GET /inspect/pipeline``."""

    schema_: Literal["fylgja.inspect.pipeline/1"] = Field(
        default=INSPECT_PIPELINE_SCHEMA, alias="schema"
    )
    """The contract version."""
    generated_at: AwareDatetime
    started_at: AwareDatetime | None = None
    """When the daemon started; the counters count from here."""
    lifecycle: list[str]
    """The daemon's start and stop steps so far, in order (``start:writer``, …)."""
    collectors_running: int
    """Distinct collectors in ``flows``; counted here, not in the page."""
    gate: GateTotals
    flows: list[FlowCount]
    """Sorted by collector, entity kind, record type."""
    writer: WriterInfo
    live_view: LiveViewInfo
    events: EventsInfo
    privacy: PrivacyInfo
    summaries: SummariesInfo
    messaging_enabled: bool
    """``[actions.message] enabled``."""
    notify: NotifyInfo
    """The alert notifier (the same block as in ``/inspect/collectors``)."""


class LabelExplain(NowModel):
    """How one workspace label was chosen."""

    workspace: str
    label: str
    """The effective label, as ``/now`` shows it."""
    label_source: LabelSource
    inferred_label: str | None = None
    rule: str | None = None
    """The inference rule: ``project``, ``app-title``, ``app`` or ``none``; absent before the first
    resolution or when the resolver made no label for the workspace."""


class ProjectPlacementInfo(NowModel):
    """Where the resolver placed one project."""

    path: str
    name: str
    workspace: str | None = None
    via: str
    confidence: str
    sessions: int
    windows: int
    unresolved: str | None = None


class SessionPlacementInfo(NowModel):
    """Where the resolver placed one session."""

    session_key: str
    workspace: str | None = None
    window_id: str | None = None
    via: str
    confidence: str
    project_via: str
    project_confidence: str
    unresolved: str | None = None
    note: str | None = None
    """The resolver's note; absent when empty."""


class RankingRule(NowModel):
    """One kind of Next candidate and its score (``core/ranking.py``)."""

    kind: str
    score: float


class InspectExplain(NowModel):
    """The answer of ``GET /inspect/explain``."""

    schema_: Literal["fylgja.inspect.explain/1"] = Field(
        default=INSPECT_EXPLAIN_SCHEMA, alias="schema"
    )
    """The contract version."""
    generated_at: AwareDatetime
    resolved: bool
    """``False`` until the resolver has run once."""
    labels: list[LabelExplain]
    """Sorted by workspace in natural order."""
    projects: list[ProjectPlacementInfo]
    """Sorted by path."""
    sessions: list[SessionPlacementInfo]
    """Sorted by session key."""
    ranking: list[RankingRule]
    """The Next scores, in rule order."""
    idle_window_seconds: float
    """Rule 5: a workspace is a way back when none of its sessions did anything for this long."""
    max_items: int
    """A Next list never holds more items than this."""
    next: list[NextItem]
    waiting: list[Waiting]
