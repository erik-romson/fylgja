"""Core record models that collectors emit and the daemon ingests (``PLAN-final-plan.md`` §3.3).

Every record is a frozen Pydantic model that carries ``source``, ``entity_kind``, ``entity_id``,
``observed_at`` and ``dedup_key``. A record is either an :class:`Event` (one entity changed) or a
:class:`Snapshot` (a list of items, optionally *complete*). Records form a discriminated union on
``entity_kind`` (and, one level above, on ``record_type``).

Adding an entity kind is a local change in this module:

1. add a member to :class:`EntityKind`;
2. add its payload model;
3. add a ``<Kind>Event`` and a ``<Kind>Snapshot`` class;
4. list both classes in :data:`EventRecord` and :data:`SnapshotRecord`;
5. list the kind in ``CAPABILITY_ENTITY_KINDS`` in :mod:`fylgja.collectors.base`.

The tests fail when a step is missing.

This module imports only the standard library and Pydantic.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    model_validator,
)

__all__ = [
    "RECORD_ADAPTER",
    "BuildEvent",
    "BuildPayload",
    "BuildSnapshot",
    "BuildWorkflow",
    "ContextItemEvent",
    "ContextItemPayload",
    "ContextItemSnapshot",
    "EntityKind",
    "Event",
    "EventRecord",
    "FocusEvent",
    "FocusPayload",
    "FocusSnapshot",
    "HealthReport",
    "HealthState",
    "PresenceEvent",
    "PresencePayload",
    "PresenceSnapshot",
    "ProjectEvent",
    "ProjectPayload",
    "ProjectSnapshot",
    "Record",
    "SessionActivity",
    "SessionActivityEvent",
    "SessionActivitySnapshot",
    "SessionEvent",
    "SessionPayload",
    "SessionSnapshot",
    "Snapshot",
    "SnapshotItem",
    "SnapshotRecord",
    "Stage",
    "TodoEvent",
    "TodoPayload",
    "TodoSnapshot",
    "UsageEvent",
    "UsagePayload",
    "UsageSnapshot",
    "WindowEvent",
    "WindowPayload",
    "WindowSnapshot",
    "WorkspaceEvent",
    "WorkspacePayload",
    "WorkspaceSnapshot",
    "compute_dedup_key",
    "parse_record",
]


class EntityKind(StrEnum):
    """What a record is about. The value is the discriminator in the JSON form."""

    WINDOW = "window"
    WORKSPACE = "workspace"
    FOCUS = "focus"
    SESSION = "session"
    SESSION_ACTIVITY = "session_activity"
    TODO = "todo"
    CONTEXT_ITEM = "context_item"
    PRESENCE = "presence"
    PROJECT = "project"
    BUILD = "build"
    USAGE = "usage"


class HealthState(StrEnum):
    STARTING = "starting"
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"
    DISABLED = "disabled"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HealthReport(_Frozen):
    """Result of ``Collector.doctor()``."""

    state: HealthState
    detail: str | None = None
    error_kind: str | None = None
    checked_at: AwareDatetime


# --------------------------------------------------------------------------------------------
# Payloads. Every field except the identifying ones is optional so that parsers can emit what
# they know; the core merges and closes entities.
# --------------------------------------------------------------------------------------------


class WindowPayload(_Frozen):
    workspace: str | None = None
    app_bundle_id: str | None = None
    app_name: str | None = None
    app_pid: int | None = None
    title: str | None = None


class WorkspacePayload(_Frozen):
    name: str
    focused: bool = False
    monitor: str | None = None


class FocusPayload(_Frozen):
    window_id: str | None = None
    workspace: str | None = None
    app_bundle_id: str | None = None
    title: str | None = None


class SessionPayload(_Frozen):
    kind: str
    native_id: str
    pid: int | None = None
    cwd: str | None = None
    name: str | None = None
    model: str | None = None
    branch: str | None = None
    host_kind: str | None = None
    host_ref: str | None = None
    parent_key: str | None = None
    remote_url: str | None = None
    started_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    process_key: str | None = None
    """The OS process (pid plus start time) hosting the session; stable across ``/clear`` (A11)."""
    predecessor_key: str | None = None
    """The session key of the session this one replaced in the same process (A11)."""
    entrypoint: str | None = None
    version: str | None = None
    """The agent's own version; messaging is gated on it per target (A9)."""
    peer_protocol: int | None = None


class Stage(_Frozen):
    name: str | None = None
    index: int | None = None
    total: int | None = None


class SessionActivity(_Frozen):
    """Normalized activity of one session (§4.6). Every field is optional."""

    state: str | None = None
    waiting_reason: str | None = None
    current_tool: str | None = None
    current_target: str | None = None
    open_task: str | None = None
    stage: Stage | None = None
    last_prompt: str | None = None
    last_reply: str | None = None
    title: str | None = None
    branch: str | None = None
    facts: dict[str, JsonValue] | None = None
    """Lidskjalv-compatible ``facts`` block (Claude, WP1.7); ``/now`` ``sessions[].facts``."""


class TodoPayload(_Frozen):
    text: str
    status: Literal["open", "next", "done"] = "open"
    scope_kind: str | None = None
    scope_ref: str | None = None
    source_ref: str | None = None
    link: str | None = None
    created_at: AwareDatetime | None = None
    done_at: AwareDatetime | None = None


class ContextItemPayload(_Frozen):
    context_kind: str
    app_bundle_id: str | None = None
    title: str | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)


class PresencePayload(_Frozen):
    state: Literal["active", "idle", "locked", "asleep"]


class ProjectPayload(_Frozen):
    path: str
    repo_root: str | None = None
    remote_url: str | None = None
    repo_name: str | None = None
    branch: str | None = None
    dirty: bool | None = None
    ahead: int | None = None
    behind: int | None = None
    pr_number: int | None = None
    pr_state: str | None = None
    review_decision: str | None = None
    last_commit_at: AwareDatetime | None = None


class BuildWorkflow(_Frozen):
    workflow: str
    state: str | None = None
    status: str | None = None
    conclusion: str | None = None
    run_id: int | None = None
    url: str | None = None
    run_started_at: AwareDatetime | None = None
    run_duration_s: float | None = None
    typical_duration_s: float | None = None


class BuildPayload(_Frozen):
    state: Literal["pass", "fail", "running", "cancelled", "none"]
    project_path: str | None = None
    head_sha: str | None = None
    url: str | None = None
    workflows: tuple[BuildWorkflow, ...] = ()
    error_kind: str | None = None


class UsagePayload(_Frozen):
    provider: str
    window: str | None = None
    used_pct: float | None = None
    resets_at: AwareDatetime | None = None
    cost_usd: float | None = None
    cost_period: str | None = None
    usage_source: str | None = None


# --------------------------------------------------------------------------------------------
# Dedup keys
# --------------------------------------------------------------------------------------------


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_dedup_key(
    source: str,
    entity_kind: str,
    entity_id: str,
    observed_at: datetime,
    payload: Any,
) -> str:
    """Deterministic key over ``(source, entity_kind, entity_id, observed_at, payload)``.

    ``payload`` must be JSON-compatible (``model_dump(mode="json")`` output). The key is a SHA-256
    over the canonical JSON, so it is identical in every process. ``observed_at`` is normalised to
    UTC. Never uses ``hash()``.
    """
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    canonical = _canonical_json(
        [
            source,
            str(entity_kind),
            entity_id,
            observed_at.astimezone(UTC).isoformat(),
            payload,
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------
# Record bases
# --------------------------------------------------------------------------------------------


class _RecordBase(_Frozen):
    source: str = Field(min_length=1)
    entity_kind: EntityKind
    entity_id: str = Field(min_length=1)
    observed_at: AwareDatetime
    dedup_key: str = ""

    def _hash_payload(self) -> Any:
        raise NotImplementedError

    @model_validator(mode="after")
    def _fill_dedup_key(self) -> Self:
        if not self.dedup_key:
            key = compute_dedup_key(
                self.source,
                self.entity_kind,
                self.entity_id,
                self.observed_at,
                self._hash_payload(),
            )
            object.__setattr__(self, "dedup_key", key)
            self.__pydantic_fields_set__.add("dedup_key")
        return self


class Event[P: BaseModel](_RecordBase):
    """One entity changed (or was observed) at ``observed_at``."""

    record_type: Literal["event"] = "event"
    payload: P

    def _hash_payload(self) -> Any:
        return self.payload.model_dump(mode="json")


class SnapshotItem[P: BaseModel](_Frozen):
    entity_id: str = Field(min_length=1)
    payload: P


class Snapshot[P: BaseModel](_RecordBase):
    """A list of items observed together.

    ``entity_id`` names the scope (for example ``"all"``). When ``complete`` is true the core closes
    every entity of this kind and source that is missing from ``items``.
    """

    record_type: Literal["snapshot"] = "snapshot"
    items: tuple[SnapshotItem[P], ...] = ()
    complete: bool = False

    def _hash_payload(self) -> Any:
        return {
            "complete": self.complete,
            "items": [item.model_dump(mode="json") for item in self.items],
        }


# --------------------------------------------------------------------------------------------
# One Event and one Snapshot class per kind (needed for the discriminated union)
# --------------------------------------------------------------------------------------------


class WindowEvent(Event[WindowPayload]):
    entity_kind: Literal[EntityKind.WINDOW] = EntityKind.WINDOW


class WindowSnapshot(Snapshot[WindowPayload]):
    entity_kind: Literal[EntityKind.WINDOW] = EntityKind.WINDOW


class WorkspaceEvent(Event[WorkspacePayload]):
    entity_kind: Literal[EntityKind.WORKSPACE] = EntityKind.WORKSPACE


class WorkspaceSnapshot(Snapshot[WorkspacePayload]):
    entity_kind: Literal[EntityKind.WORKSPACE] = EntityKind.WORKSPACE


class FocusEvent(Event[FocusPayload]):
    entity_kind: Literal[EntityKind.FOCUS] = EntityKind.FOCUS


class FocusSnapshot(Snapshot[FocusPayload]):
    entity_kind: Literal[EntityKind.FOCUS] = EntityKind.FOCUS


class SessionEvent(Event[SessionPayload]):
    entity_kind: Literal[EntityKind.SESSION] = EntityKind.SESSION


class SessionSnapshot(Snapshot[SessionPayload]):
    entity_kind: Literal[EntityKind.SESSION] = EntityKind.SESSION


class SessionActivityEvent(Event[SessionActivity]):
    entity_kind: Literal[EntityKind.SESSION_ACTIVITY] = EntityKind.SESSION_ACTIVITY


class SessionActivitySnapshot(Snapshot[SessionActivity]):
    entity_kind: Literal[EntityKind.SESSION_ACTIVITY] = EntityKind.SESSION_ACTIVITY


class TodoEvent(Event[TodoPayload]):
    entity_kind: Literal[EntityKind.TODO] = EntityKind.TODO


class TodoSnapshot(Snapshot[TodoPayload]):
    entity_kind: Literal[EntityKind.TODO] = EntityKind.TODO


class ContextItemEvent(Event[ContextItemPayload]):
    entity_kind: Literal[EntityKind.CONTEXT_ITEM] = EntityKind.CONTEXT_ITEM


class ContextItemSnapshot(Snapshot[ContextItemPayload]):
    entity_kind: Literal[EntityKind.CONTEXT_ITEM] = EntityKind.CONTEXT_ITEM


class PresenceEvent(Event[PresencePayload]):
    entity_kind: Literal[EntityKind.PRESENCE] = EntityKind.PRESENCE


class PresenceSnapshot(Snapshot[PresencePayload]):
    entity_kind: Literal[EntityKind.PRESENCE] = EntityKind.PRESENCE


class ProjectEvent(Event[ProjectPayload]):
    entity_kind: Literal[EntityKind.PROJECT] = EntityKind.PROJECT


class ProjectSnapshot(Snapshot[ProjectPayload]):
    entity_kind: Literal[EntityKind.PROJECT] = EntityKind.PROJECT


class BuildEvent(Event[BuildPayload]):
    entity_kind: Literal[EntityKind.BUILD] = EntityKind.BUILD


class BuildSnapshot(Snapshot[BuildPayload]):
    entity_kind: Literal[EntityKind.BUILD] = EntityKind.BUILD


class UsageEvent(Event[UsagePayload]):
    entity_kind: Literal[EntityKind.USAGE] = EntityKind.USAGE


class UsageSnapshot(Snapshot[UsagePayload]):
    entity_kind: Literal[EntityKind.USAGE] = EntityKind.USAGE


# --------------------------------------------------------------------------------------------
# The unions
# --------------------------------------------------------------------------------------------

EventRecord = Annotated[
    WindowEvent
    | WorkspaceEvent
    | FocusEvent
    | SessionEvent
    | SessionActivityEvent
    | TodoEvent
    | ContextItemEvent
    | PresenceEvent
    | ProjectEvent
    | BuildEvent
    | UsageEvent,
    Field(discriminator="entity_kind"),
]

SnapshotRecord = Annotated[
    WindowSnapshot
    | WorkspaceSnapshot
    | FocusSnapshot
    | SessionSnapshot
    | SessionActivitySnapshot
    | TodoSnapshot
    | ContextItemSnapshot
    | PresenceSnapshot
    | ProjectSnapshot
    | BuildSnapshot
    | UsageSnapshot,
    Field(discriminator="entity_kind"),
]

Record = Annotated[EventRecord | SnapshotRecord, Field(discriminator="record_type")]

RECORD_ADAPTER: Final[TypeAdapter[Record]] = TypeAdapter(Record)
"""Validates and serialises any record: ``RECORD_ADAPTER.validate_json(text)``."""


def parse_record(data: str | bytes | dict[str, Any]) -> Record:
    """Parse a record from JSON text or a decoded mapping. Raises ``ValidationError``."""
    if isinstance(data, dict):
        return RECORD_ADAPTER.validate_python(data)
    return RECORD_ADAPTER.validate_json(data)
