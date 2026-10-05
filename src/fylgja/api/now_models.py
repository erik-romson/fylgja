"""The ``fylgja.now/1`` contract: the models behind ``GET /now`` (``PLAN-final-plan.md`` §4.2,
§4.6, §4.7; ``PLAN.md`` §6, §13).

**Serialization rule.** A field without a value is **absent**; ``null`` never appears in a
response. Every model here is dumped with ``exclude_none=True`` (:func:`dump_json`), and the
generated JSON Schema (:func:`now_json_schema`) says the same thing: an optional field is not
``required`` and its type never includes ``null``. Lists that are always provided are present even
when empty.

**Sections that may be absent.** ``workspaces`` and ``focused`` come from collectors with the
``windows`` capability, ``sessions`` from ``sessions`` collectors, and ``projects`` from either of
those or ``project_state``. While no such collector is available (enabled and ``starting`` or
``ok``) the section is absent; the entries of a single unavailable collector are left out of a
section that others still fill. A disabled, degraded or failed collector never makes ``/now`` an
error: it is listed in ``collectors[]`` and, when degraded or failed, named in ``degraded``.

**Filled later.** Every optional section and field that later work fills is already part of
``fylgja.now/1`` and stays absent until then, so filling it changes nothing here:

* ``usage``, ``presence``, ``waiting[]``, ``next[]`` and ``next_list_id`` (top level);
* ``focused.context[]`` and ``workspaces[].windows[].context[]`` (application context);
* ``workspaces[].todos[]``, ``sessions[].todos[]`` and ``projects[].todos[]`` (todos);
* ``workspaces[].story`` and ``sessions[].story`` (summaries);
* ``sessions[].tokens``, ``cost_usd``, ``context_pct``, ``pipeline``, ``stage`` and ``children[]``;
* ``projects[].pr``, ``build``, ``dirty_count`` and the git fields the project collector fills.

The §4.7 sections (``projects[].build``, ``sessions[].tokens`` and ``usage``) carry ``source``,
``observed_at``, ``freshness`` and, when the last attempt failed, ``error_kind``.

Changing anything here changes the generated schema; ``tests/unit/test_now_schema_guard.py`` then
fails until :data:`NOW_SCHEMA` is bumped and ``docs/api/now.schema.json`` is regenerated with
``scripts/gen_now_schema.py``.

This module imports only the standard library, Pydantic and :mod:`fylgja.models`, so the CLI and
tests can use it without FastAPI.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, TypeAdapter
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import core_schema

from fylgja.models import HealthState

__all__ = [
    "JSON_SCHEMA_DIALECT",
    "NOW_SCHEMA",
    "Build",
    "BuildWorkflow",
    "CollectorInfo",
    "ContextItem",
    "Facts",
    "FactsTask",
    "Focused",
    "Freshness",
    "Host",
    "LabelSource",
    "NextItem",
    "Now",
    "NowModel",
    "OneLinerRule",
    "Presence",
    "Project",
    "PullRequest",
    "Session",
    "SessionState",
    "SessionStory",
    "Stage",
    "SummaryState",
    "Todo",
    "Tokens",
    "Usage",
    "UsageCost",
    "UsageProvider",
    "Waiting",
    "Window",
    "Workspace",
    "WorkspaceStory",
    "dump_json",
    "now_json_schema",
]

NOW_SCHEMA: Final = "fylgja.now/1"
"""The version string ``/now`` carries in ``schema``. Bump it with any change to these models."""

JSON_SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"


class NowModel(BaseModel):
    """Base of every ``fylgja.now/1`` model: frozen, closed, documented by its docstrings."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        use_attribute_docstrings=True,
        serialize_by_alias=True,
        validate_by_alias=True,
        validate_by_name=True,
    )


# --------------------------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------------------------


class SessionState(StrEnum):
    """Every state a session can be in, from every source, defined once for ``fylgja.now/1``.

    ``unknown`` stands for a state a source reports that is not in this vocabulary."""

    # The Claude registry until hooks arrive (its ``busy`` is reported as ``working``), and the
    # ``busy``/``idle`` of Codex and OpenCode.
    IDLE = "idle"
    BUSY = "busy"
    WAITING = "waiting"
    # Claude hooks (the session state machine).
    WORKING = "working"
    RUNNING_TOOL = "running_tool"
    WAITING_PERMISSION = "waiting_permission"
    WAITING_INPUT = "waiting_input"
    TURN_DONE = "turn_done"
    SEEN = "seen"
    ERROR = "error"
    ENDED = "ended"
    # norn runs, with their terminal statuses.
    RUNNING = "running"
    STALE = "stale"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    # Anything else.
    UNKNOWN = "unknown"


class Freshness(StrEnum):
    """§4.7: ``fresh`` within twice the source's poll interval, ``stale`` after that, and
    ``unavailable`` (with ``error_kind``) when the last attempt failed."""

    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class LabelSource(StrEnum):
    MANUAL = "manual"
    INFERRED = "inferred"
    NONE = "none"


class OneLinerRule(StrEnum):
    """The §4.6 rule that produced a session's one-liner (``fylgja.core.one_liner.RULES``)."""

    WAITING = "waiting"
    STAGE = "stage"
    TOOL = "tool"
    TASK = "task"
    TITLE_PROMPT = "title_prompt"
    TITLE_REPLY = "title_reply"
    NONE = "none"


class SummaryState(StrEnum):
    """The state of a cached summary (§4.2 "Summary responses")."""

    FRESH = "fresh"
    STALE = "stale"
    PENDING = "pending"
    MISSING = "missing"
    FAILED = "failed"
    DISABLED = "disabled"


# --------------------------------------------------------------------------------------------
# Shared pieces
# --------------------------------------------------------------------------------------------


class ContextItem(NowModel):
    """What is open in a window: a tab, a chat, a file, a note (filled by application context)."""

    kind: str
    """What the item is, for example ``tab``, ``chat``, ``file``, ``note`` or ``cmux_tab``."""
    source: str | None = None
    """The collector that produced it."""
    title: str | None = None
    """Redacted."""
    data: dict[str, JsonValue] | None = None
    """Parsed fields of the item (project, path, URL, profile, channel, ...)."""
    unread: int | bool | None = None
    """An explicit unread marker of the app (A15): a count, or ``true`` when it has no count."""
    confidence: str | None = None
    link: str | None = None
    """A link back into the app (for example ``obsidian://``)."""
    observed_at: AwareDatetime | None = None


class Todo(NowModel):
    """An open or next todo of a workspace, project or session (filled by the todo work)."""

    id: str
    text: str
    status: Literal["open", "next", "done"]
    scope_kind: str | None = None
    """``workspace``, ``project`` or ``session``."""
    scope_ref: str | None = None
    source: str | None = None
    """``fylgja`` for native todos, else the import source."""
    source_ref: str | None = None
    link: str | None = None
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    done_at: AwareDatetime | None = None


class Stage(NowModel):
    """A pipeline stage (norn)."""

    name: str | None = None
    index: int | None = None
    total: int | None = None


# --------------------------------------------------------------------------------------------
# focused
# --------------------------------------------------------------------------------------------


class Focused(NowModel):
    """The focused window."""

    window_id: str | None = None
    workspace: str | None = None
    """The AeroSpace workspace."""
    app_name: str | None = None
    bundle_id: str | None = None
    title: str | None = None
    """Redacted."""
    session_key: str | None = None
    """The session in the focused window, when exactly one is placed there."""
    since: AwareDatetime | None = None
    """When focus moved here."""
    context: list[ContextItem] | None = None
    """Filled by application context."""


# --------------------------------------------------------------------------------------------
# workspaces
# --------------------------------------------------------------------------------------------


class Window(NowModel):
    id: str
    """The AeroSpace window id."""
    app: str | None = None
    """The app name."""
    bundle_id: str | None = None
    title: str | None = None
    """Redacted."""
    focused: bool = False
    context: list[ContextItem] | None = None
    """Filled by application context."""


class WorkspaceStory(NowModel):
    """A cached workspace summary (filled by the summary service)."""

    text: str | None = None
    generated_at: AwareDatetime | None = None
    state: SummaryState


class Workspace(NowModel):
    """An AeroSpace workspace (never a cmux workspace)."""

    name: str
    label: str
    """The effective label: manual, else inferred, else ``—``."""
    label_source: LabelSource
    inferred_label: str | None = None
    one_liner: str
    """The effective label, `` — `` and the most relevant live context (§4.6)."""
    focused: bool = False
    monitor: str | None = None
    windows: list[Window]
    projects: list[str]
    """Paths of the projects of the sessions placed here (see ``projects[]``)."""
    sessions: list[str]
    """Keys of the sessions placed here (see ``sessions[]``)."""
    todos: list[Todo] | None = None
    """Filled by the todo work."""
    story: WorkspaceStory | None = None
    """Filled by the summary service."""


# --------------------------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------------------------


class Host(NowModel):
    """Where a session runs (A1): the host app, its window and, in cmux, the exact surface."""

    kind: str | None = None
    """``cmux``, ``jetbrains``, ``iterm2`` or ``other``."""
    ref: str | None = None
    """cmux: ``<cmux workspace id>/<surface id>``; JetBrains: the IDE project path."""
    window_id: str | None = None
    workspace: str | None = None
    """The AeroSpace workspace of the window."""
    cmux_workspace: str | None = None
    """The cmux workspace id (a cmux tab, not an AeroSpace workspace)."""
    cmux_surface: str | None = None
    via: str | None = None
    """How the join was made."""
    confidence: str | None = None
    unresolved: str | None = None
    """Why no window was found, when none was."""


class FactsTask(NowModel):
    status: str
    content: str


class Facts(NowModel):
    """The Lidskjalv-compatible facts block of a Claude session (a port of ``_SUMMARY_JQ``).

    ``tasks`` is Lidskjalv's ``todos``; the session id is ``sessions[].native_id``."""

    title: str | None = None
    prompt: str | None = None
    said: str | None = None
    mode: str | None = None
    branch: str | None = None
    version: str | None = None
    model: str | None = None
    turns: int | None = None
    started: int | None = None
    """Epoch seconds of the first transcript line."""
    last_at: int | None = None
    """Epoch seconds of the last transcript line."""
    prompts: list[str] | None = None
    tools: dict[str, int] | None = None
    files: list[str] | None = None
    tasks: list[FactsTask] | None = None


class Tokens(NowModel):
    """§4.7: token sums over the transcript's assistant ``message.usage``."""

    input: int
    output: int
    cache_read: int
    cache_write: int
    source: str
    observed_at: AwareDatetime
    freshness: Freshness
    error_kind: str | None = None


class SessionStory(NowModel):
    """A cached ARC/NOW story of a session (filled by the summary service)."""

    arc: str | None = None
    now: str | None = None
    generated_at: AwareDatetime | None = None
    state: SummaryState


class Session(NowModel):
    """A live agent session of any kind."""

    key: str
    """``kind:native_id``."""
    kind: str
    """``claude``, ``codex``, ``opencode``, ``norn``, or a plugin's own kind."""
    native_id: str
    name: str | None = None
    title: str | None = None
    """Redacted."""
    cwd: str | None = None
    project_path: str | None = None
    pid: int | None = None
    state: SessionState | None = None
    state_since: AwareDatetime | None = None
    waiting_reason: str | None = None
    current_tool: str | None = None
    current_target: str | None = None
    """What the current tool works on: a file path, a command head or a URL host."""
    host: Host | None = None
    entrypoint: str | None = None
    """How the agent was started (Claude: ``cli``, ``sdk-py``, ...)."""
    model: str | None = None
    branch: str | None = None
    version: str | None = None
    remote_url: str | None = None
    lane_key: str | None = None
    """Equals ``process_key`` (A11): Lidskjalv's lane survives ``/clear``."""
    process_key: str | None = None
    """The OS process (pid plus start time) hosting the session."""
    predecessor_key: str | None = None
    """The session this one replaced in the same process (``/clear``)."""
    parent_key: str | None = None
    children: list[Session] | None = None
    """Sessions nested under this one (norn runs and their agent sessions)."""
    facts: Facts | None = None
    one_liner: str | None = None
    """What the session is doing, in one sentence (§4.6), present once it has activity."""
    one_liner_from: OneLinerRule | None = None
    tokens: Tokens | None = None
    cost_usd: float | None = None
    context_pct: float | None = None
    pipeline: str | None = None
    stage: Stage | None = None
    todos: list[Todo] | None = None
    story: SessionStory | None = None
    first_seen: AwareDatetime | None = None
    last_seen: AwareDatetime | None = None


# --------------------------------------------------------------------------------------------
# projects
# --------------------------------------------------------------------------------------------


class PullRequest(NowModel):
    number: int
    state: str | None = None
    review_decision: str | None = None
    checks: str | None = None
    """The aggregate state of the PR's checks."""
    url: str | None = None


class BuildWorkflow(NowModel):
    workflow: str
    state: str | None = None
    status: str | None = None
    conclusion: str | None = None
    run_id: int | None = None
    url: str | None = None
    run_started_at: AwareDatetime | None = None
    run_duration_s: float | None = None
    typical_duration_s: float | None = None


class Build(NowModel):
    """§4.7: the GitHub Actions runs of the project's branch, folded like Lidskjalv's builds."""

    state: Literal["pass", "fail", "running", "cancelled", "none"]
    head_sha: str | None = None
    url: str | None = None
    workflows: list[BuildWorkflow] = Field(default_factory=list)
    source: str
    observed_at: AwareDatetime
    freshness: Freshness
    error_kind: str | None = None


class Project(NowModel):
    """A project: a git root, or a working directory outside any repository."""

    path: str
    name: str
    """Its display name: the draupnir group, the repository name or the directory name."""
    repo_root: str | None = None
    repo_name: str | None = None
    remote_url: str | None = None
    group: str | None = None
    """The draupnir workspace the clone belongs to."""
    workspace: str | None = None
    """The AeroSpace workspace the project is mostly in."""
    sessions: list[str]
    """Keys of the live sessions in the project."""
    branch: str | None = None
    dirty: bool | None = None
    dirty_count: int | None = None
    ahead: int | None = None
    behind: int | None = None
    pr: PullRequest | None = None
    build: Build | None = None
    last_commit_at: AwareDatetime | None = None
    todos: list[Todo] | None = None
    updated_at: AwareDatetime | None = None


# --------------------------------------------------------------------------------------------
# usage, presence, waiting, next, collectors
# --------------------------------------------------------------------------------------------


class UsageProvider(NowModel):
    provider: str
    window: str | None = None
    used_pct: float | None = None
    resets_at: AwareDatetime | None = None
    source: str | None = None
    observed_at: AwareDatetime | None = None
    freshness: Freshness | None = None
    error_kind: str | None = None


class UsageCost(NowModel):
    period: str | None = None
    usd: float
    source: str | None = None
    observed_at: AwareDatetime | None = None
    freshness: Freshness | None = None
    error_kind: str | None = None


class Usage(NowModel):
    """§4.7: quota per provider and spend."""

    providers: list[UsageProvider] = Field(default_factory=list)
    cost: UsageCost | None = None
    source: str
    observed_at: AwareDatetime
    freshness: Freshness
    error_kind: str | None = None


class Presence(NowModel):
    """Idle, lock and call state (filled by the presence collector)."""

    state: Literal["active", "idle", "locked", "asleep"]
    since: AwareDatetime | None = None
    in_call: bool | None = None
    source: str | None = None
    observed_at: AwareDatetime | None = None


class Waiting(NowModel):
    """Something waiting on the person (filled by waiting-on-me), most urgent first."""

    kind: str
    """``permission``, ``input``, ``turn_done``, ``build_failed``, ``review_requested`` or
    ``unread``."""
    target: str
    """A session key, project path or window id."""
    target_kind: str | None = None
    reason: str
    since: AwareDatetime | None = None


class NextItem(NowModel):
    """One ranked suggestion (filled by the ranking); its list is ``next_list_id``."""

    suggestion_id: str
    kind: str
    target_ref: str
    score: float
    reason: str
    rank: int | None = None


class CollectorInfo(NowModel):
    """One collector and its health."""

    name: str
    origin: Literal["built-in", "external"]
    state: HealthState
    since: AwareDatetime | None = None
    last_ok_at: AwareDatetime | None = None
    restarts: int = 0
    last_error_kind: str | None = None
    detail: str | None = None


# --------------------------------------------------------------------------------------------
# /now
# --------------------------------------------------------------------------------------------


class Now(NowModel):
    """The whole picture in one call (``GET /now``)."""

    schema_: Literal["fylgja.now/1"] = Field(default=NOW_SCHEMA, alias="schema")
    """The contract version."""
    generated_at: AwareDatetime
    focused: Focused | None = None
    workspaces: list[Workspace] | None = None
    sessions: list[Session] | None = None
    projects: list[Project] | None = None
    usage: Usage | None = None
    presence: Presence | None = None
    waiting: list[Waiting] | None = None
    next: list[NextItem] | None = None
    next_list_id: str | None = None
    """The ``list_id`` of ``next[]`` (for ``POST /next/feedback``)."""
    collectors: list[CollectorInfo]
    degraded: list[str]
    """Names of the collectors that are degraded or failed."""


# --------------------------------------------------------------------------------------------
# Serialization and schema
# --------------------------------------------------------------------------------------------

_ADAPTERS: dict[type[NowModel], TypeAdapter[list[Any]]] = {}


def dump_json(value: NowModel | Sequence[NowModel], *, kind: type[NowModel] | None = None) -> bytes:
    """JSON of a model or a list of models under the serialization rule (no ``null``).

    ``kind`` names the item model of a list (needed for an empty one).
    """
    if isinstance(value, NowModel):
        return value.model_dump_json(exclude_none=True).encode("utf-8")
    item = kind if kind is not None else (type(value[0]) if value else NowModel)
    adapter = _ADAPTERS.get(item)
    if adapter is None:
        adapter = TypeAdapter(list[item])  # type: ignore[valid-type]
        _ADAPTERS[item] = adapter
    return adapter.dump_json(list(value), exclude_none=True, by_alias=True)


class _AbsentNotNull(GenerateJsonSchema):
    """The schema of the serialization rule: optional means absent, never ``null``.

    A field is ``required`` exactly when every response carries it: it has no default, or its
    default is a value (``false``, ``0``, ``[]``, the version string) that is always written.
    """

    def field_is_required(
        self,
        field: core_schema.ModelField | core_schema.DataclassField | core_schema.TypedDictField,
        total: bool,
    ) -> bool:
        schema = field["schema"]
        if schema["type"] != "default":
            return True
        return "default_factory" in schema or schema.get("default") is not None

    def nullable_schema(self, schema: core_schema.NullableSchema) -> JsonSchemaValue:
        return self.generate_inner(schema["schema"])

    def default_schema(self, schema: core_schema.WithDefaultSchema) -> JsonSchemaValue:
        if "default" in schema and schema["default"] is None:
            return self.generate_inner(schema["schema"])
        return super().default_schema(schema)


def now_json_schema() -> dict[str, Any]:
    """The JSON Schema of ``fylgja.now/1`` as ``docs/api/now.schema.json`` holds it."""
    schema = Now.model_json_schema(
        by_alias=True, mode="serialization", schema_generator=_AbsentNotNull
    )
    return {"$schema": JSON_SCHEMA_DIALECT, "$id": NOW_SCHEMA, **schema}
