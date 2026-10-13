"""Builds ``fylgja.now/1`` from the daemon's state (``PLAN-final-plan.md`` §4.2, §4.6).

:func:`build_now` is a pure function of a :class:`~fylgja.core.daemon.StateView`: the live view,
the collectors' health, the session memory and the latest resolution. It runs on the event loop
thread for every request and reads nothing else.

* **Availability.** Only the data of available collectors (enabled, ``starting`` or ``ok``) is
  shown; see :mod:`fylgja.api.now_models` for when a whole section is absent.
* **Sessions** are the live ones, ordered by key. ``one_liner`` and ``one_liner_from`` are
  computed from the stored activity and the session's title with the §4.6 rules, exactly as the
  writer stored them; ``facts``, ``entrypoint`` and ``version`` come from the session memory, and
  ``host.via`` and ``host.confidence`` from the resolution when it matches the stored join.
* **Workspaces** are every present AeroSpace workspace plus any a window or session names, in
  natural order (``2`` before ``10``). Each carries its effective label and the §4.6 workspace
  one-liner.
* **Projects** are the projects of the live sessions, ordered by path. The project collector's
  state fills ``branch``, ``dirty``, ``ahead``, ``behind``, ``last_commit_at``, ``pr`` (absent
  when the branch has none) and ``build`` (§4.7), whose ``freshness`` is judged against the poll
  interval stored with it (:func:`fylgja.core.freshness.freshness`).
* **Tokens, cost and quota** (WP5.4, §4.7): a session's ``tokens``, ``cost_usd`` and ``context_pct``
  come from its stored activity interval, and ``usage`` from the stored provider rows joined with
  what the daemon remembers of their observation (:mod:`fylgja.api.now_usage`).
* **Waiting-on-me** (WP6.1): ``waiting[]`` is :func:`fylgja.core.waiting.waiting_on_me` over the
  shown sessions, the stored projects, the window context items and the projects touched today,
  most urgent first. It is present (possibly empty) while any of those sections is shown.
* **What next** (WP6.2): ``next[]`` is :func:`fylgja.core.ranking.rank_next` over ``waiting[]``, the
  open todos, the shown sessions, the focused workspace and the focus of today, published through
  ``StateView.next_lists`` (which gives it its ``next_list_id`` and logs it). It is present with
  ``waiting[]``, and absent when the view carries no ``next_lists``. An unchanged situation is the
  same list with the same ids.
* **Todos** (WP4.1): the open and next items of the live view fill ``workspaces[].todos``,
  ``sessions[].todos`` and ``projects[].todos`` by scope; ``next`` items come first. An entry
  with no todos has no ``todos`` field.
* **Stories** (WP8.1): while summaries are enabled, a session or workspace with a cached summary
  carries it as ``story``, its state judged against the entry's current inputs
  (:mod:`fylgja.api.summary_inputs`). Nothing is generated here.

Titles, names and prompt text arrive redacted (the ingest gate ran before the store); nothing here
logs them.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import ValidationError

from fylgja.api.now_models import (
    Build,
    CollectorInfo,
    ContextItem,
    Facts,
    Focused,
    Host,
    LabelSource,
    NextItem,
    Now,
    OneLinerRule,
    Project,
    PullRequest,
    Session,
    SessionState,
    Stage,
    Todo,
    Waiting,
    Window,
    Workspace,
)
from fylgja.api.now_usage import build_tokens, build_usage
from fylgja.api.summary_inputs import with_session_story, with_workspace_story
from fylgja.collectors.base import Capability
from fylgja.collectors.registry import Origin
from fylgja.core.context_items import ContextEntry
from fylgja.core.daemon import CollectorState, SessionExtras, StateView
from fylgja.core.freshness import freshness
from fylgja.core.labels import EMPTY_LABEL, effective_label, project_name
from fylgja.core.next_lists import Published
from fylgja.core.one_liner import WorkspaceSession, session_one_liner, workspace_one_liner
from fylgja.core.ranking import rank_next
from fylgja.core.resolver import Placement, ProjectPlacement
from fylgja.core.waiting import waiting_on_me
from fylgja.models import SessionActivity
from fylgja.models import Stage as ActivityStage
from fylgja.rankers.base import natural_key
from fylgja.store.rows import (
    ActivityRow,
    FocusRow,
    ProjectRow,
    SessionRow,
    TodoRow,
    WindowRow,
    WorkspaceRow,
)

__all__ = ["build_now", "collector_info", "natural_key", "next_items", "todo_model"]

logger = logging.getLogger("fylgja.api.now")

_STATES: Final[Mapping[str, SessionState]] = {state.value: state for state in SessionState}
_FACT_FIELDS: Final = frozenset(Facts.model_fields)
_TODO_STATUSES: Final[Mapping[str, Literal["open", "next", "done"]]] = {
    "open": "open",
    "next": "next",
    "done": "done",
}
_TODO_SHOWN: Final = frozenset({"open", "next"})
"""The statuses ``/now`` shows (a done todo is not on the picture)."""


def _window_key(window: WindowRow) -> tuple[int, int, str, str]:
    return natural_key(window.window_id)


# --------------------------------------------------------------------------------------------
# Pieces
# --------------------------------------------------------------------------------------------


TodoIndex = Mapping[tuple[str, str], list[Todo]]
"""Todos by ``(scope_kind, scope_ref)``."""


def todo_model(row: TodoRow) -> Todo:
    """The ``/now`` model of a stored todo."""
    return Todo(
        id=row.id,
        text=row.text,
        status=_TODO_STATUSES[row.status],
        scope_kind=row.scope_kind,
        scope_ref=row.scope_ref,
        source=row.source,
        source_ref=row.source_ref,
        link=row.link,
        created_at=row.created_at,
        updated_at=row.updated_at,
        done_at=row.done_at,
    )


def _todo_index(rows: Iterable[TodoRow]) -> TodoIndex:
    """Open and next todos grouped by scope; ``next`` first, then oldest first (the sort is
    stable, so todos created in the same instant keep the order they were added in)."""
    ordered = sorted(
        (r for r in rows if r.status in _TODO_SHOWN and r.scope_kind and r.scope_ref),
        key=lambda r: (r.status != "next", r.created_at),
    )
    index: dict[tuple[str, str], list[Todo]] = {}
    for row in ordered:
        index.setdefault((str(row.scope_kind), str(row.scope_ref)), []).append(todo_model(row))
    return index


def _todos_of(index: TodoIndex, kind: str, ref: str) -> list[Todo] | None:
    return index.get((kind, ref)) or None


ContextIndex = Mapping[str, list[ContextItem]]
"""Context items by window id (WP5.3)."""

_TITLE_TIER: Final = 1
"""The tier of the items made from a window title."""


def _context_index(
    contexts: Mapping[str, Sequence[ContextEntry]], available: set[str]
) -> tuple[ContextIndex, Mapping[str, str]]:
    """The ``/now`` context items of each window, and the title summary of each window.

    Only the items of available collectors are shown. The summary is the title of the window's
    title-tier item: the third source of the workspace one-liner (§4.6).
    """
    items: dict[str, list[ContextItem]] = {}
    summaries: dict[str, str] = {}
    for window_id, entries in contexts.items():
        shown = [entry for entry in entries if entry.source in available]
        if not shown:
            continue
        items[window_id] = [
            ContextItem(
                kind=entry.kind,
                source=entry.source,
                title=entry.title,
                data=dict(entry.data) or None,
                unread=entry.unread,
                confidence=entry.confidence,
                link=entry.link,
                observed_at=entry.observed_at,
            )
            for entry in shown
        ]
        summary = next((e.title for e in shown if e.tier == _TITLE_TIER and e.title), None)
        if summary:
            summaries[window_id] = summary
    return items, summaries


def _available_contexts(
    contexts: Mapping[str, Sequence[ContextEntry]], available: set[str]
) -> dict[str, list[ContextEntry]]:
    """The context entries of available collectors, by window id."""
    shown = {
        window_id: [entry for entry in entries if entry.source in available]
        for window_id, entries in contexts.items()
    }
    return {window_id: entries for window_id, entries in shown.items() if entries}


def _state(value: str | None) -> SessionState | None:
    return None if value is None else _STATES.get(value, SessionState.UNKNOWN)


def _facts(raw: Mapping[str, Any] | None) -> Facts | None:
    if not raw:
        return None
    try:
        return Facts.model_validate({k: v for k, v in raw.items() if k in _FACT_FIELDS})
    except ValidationError:
        logger.debug("a facts block did not fit the /now model and was left out")
        return None


def _stage(raw: Mapping[str, Any] | None) -> Stage | None:
    if not raw:
        return None
    try:
        return Stage.model_validate(raw)
    except ValidationError:
        return None


def _activity_stage(raw: Mapping[str, Any] | None) -> ActivityStage | None:
    if not raw:
        return None
    try:
        return ActivityStage.model_validate(raw)
    except ValidationError:
        return None


def _activity_of(row: SessionRow, activity: ActivityRow) -> SessionActivity:
    """The activity the writer derived the stored one-liner from (with the session's title)."""
    return SessionActivity(
        state=activity.state,
        waiting_reason=activity.waiting_reason,
        current_tool=activity.last_tool,
        current_target=activity.current_target,
        open_task=activity.open_task,
        stage=_activity_stage(activity.stage),
        last_prompt=activity.last_prompt,
        last_reply=activity.last_reply,
        title=row.title,
        branch=row.branch,
    )


def _host(row: SessionRow, placement: Placement | None) -> Host | None:
    stored = (row.host_kind, row.host_ref, row.window_id, row.workspace)
    if (
        placement is not None
        and (
            placement.host_kind,
            placement.host_ref,
            placement.window_id,
            placement.workspace,
        )
        == stored
    ):
        return Host(
            kind=placement.host_kind,
            ref=placement.host_ref,
            window_id=placement.window_id,
            workspace=placement.workspace,
            cmux_workspace=placement.cmux_workspace_id,
            cmux_surface=placement.cmux_surface_id,
            via=placement.via,
            confidence=placement.confidence,
            unresolved=placement.unresolved,
        )
    if all(value is None for value in stored):
        return None
    return Host(
        kind=row.host_kind, ref=row.host_ref, window_id=row.window_id, workspace=row.workspace
    )


def _session(
    row: SessionRow,
    activity: ActivityRow | None,
    extras: SessionExtras | None,
    placement: Placement | None,
    todos: list[Todo] | None = None,
    now: datetime | None = None,
) -> Session:
    one_liner: str | None = None
    rule: OneLinerRule | None = None
    if activity is not None:
        text, rule_name = session_one_liner(_activity_of(row, activity))
        one_liner, rule = text, OneLinerRule(rule_name)
    return Session(
        key=row.session_key,
        kind=row.kind,
        native_id=row.native_id,
        name=row.name,
        title=row.title,
        cwd=row.cwd,
        project_path=row.project_path,
        pid=row.pid,
        state=_state(row.state),
        state_since=row.state_since,
        waiting_reason=row.waiting_reason,
        current_tool=None if activity is None else activity.last_tool,
        current_target=None if activity is None else activity.current_target,
        host=_host(row, placement),
        entrypoint=None if extras is None else extras.entrypoint,
        model=row.model,
        branch=row.branch,
        version=None if extras is None else extras.version,
        remote_url=row.remote_url,
        lane_key=row.process_key,
        process_key=row.process_key,
        predecessor_key=row.predecessor_key,
        parent_key=row.parent_key,
        facts=None if extras is None else _facts(extras.facts),
        one_liner=one_liner,
        one_liner_from=rule,
        tokens=None if now is None else build_tokens(activity, row.last_seen, now),
        cost_usd=None if activity is None else activity.cost_usd,
        context_pct=None if activity is None else activity.context_pct,
        pipeline=row.title if activity is not None and activity.stage else None,
        stage=None if activity is None else _stage(activity.stage),
        todos=todos,
        first_seen=row.first_seen,
        last_seen=row.last_seen,
    )


def _nested(sessions: Sequence[Session]) -> list[Session]:
    """The top-level sessions, with every session whose ``parent_key`` names another listed
    session moved into that session's ``children[]`` (norn runs and the agent sessions they drive).

    A session whose parent is not listed (an ended run) stays a top-level session.
    """
    present = {s.key for s in sessions}
    children: dict[str, list[Session]] = {}
    for session in sessions:
        parent = session.parent_key
        if parent is not None and parent != session.key and parent in present:
            children.setdefault(parent, []).append(session)
    nested = {s.key for group in children.values() for s in group}
    return [
        session.model_copy(update={"children": children[session.key]})
        if session.key in children
        else session
        for session in sessions
        if session.key not in nested
    ]


def _focused(
    focus: FocusRow,
    windows: Mapping[str, WindowRow],
    sessions: Sequence[SessionRow],
    context: list[ContextItem] | None = None,
) -> Focused:
    window = None if focus.window_id is None else windows.get(focus.window_id)
    hosted = [s.session_key for s in sessions if focus.window_id and s.window_id == focus.window_id]
    return Focused(
        window_id=focus.window_id,
        workspace=focus.workspace if window is None or not window.workspace else window.workspace,
        app_name=None if window is None else window.app_name,
        bundle_id=focus.app_bundle_id if window is None else window.app_bundle_id,
        title=focus.title if window is None else window.title,
        session_key=hosted[0] if len(hosted) == 1 else None,
        since=focus.observed_at,
        context=context,
    )


def _workspace(
    name: str,
    row: WorkspaceRow | None,
    inferred_fallback: str | None,
    windows: Sequence[WindowRow],
    sessions: Sequence[tuple[SessionRow, ActivityRow | None, Session]],
    focus: FocusRow | None,
    todos: list[Todo] | None = None,
    contexts: ContextIndex | None = None,
    summaries: Mapping[str, str] | None = None,
) -> Workspace:
    inferred = (None if row is None else row.inferred_label) or inferred_fallback
    if inferred == EMPTY_LABEL:
        inferred = None  # nothing was inferred: the field is absent
    label, source = effective_label(None if row is None else row.manual_label, inferred)
    members = [
        WorkspaceSession(
            key=built.key,
            one_liner=built.one_liner,
            state=stored.state,
            since=stored.state_since,
            active_at=(activity.valid_from if activity is not None else stored.state_since)
            or stored.first_seen,
        )
        for stored, activity, built in sessions
    ]
    focused_window = None if focus is None else focus.window_id
    title_context = (
        None
        if summaries is None
        or focused_window is None
        or all(w.window_id != focused_window for w in windows)
        else summaries.get(focused_window)
    )
    return Workspace(
        name=name,
        label=label,
        label_source=LabelSource(source),
        inferred_label=inferred,
        one_liner=workspace_one_liner(label, members, title_context=title_context),
        focused=(row is not None and row.focused)
        or (focus is not None and focus.workspace == name),
        monitor=None if row is None else row.monitor,
        windows=[
            Window(
                id=w.window_id,
                app=w.app_name,
                bundle_id=w.app_bundle_id,
                title=w.title,
                focused=focused_window is not None and w.window_id == focused_window,
                context=None if contexts is None else contexts.get(w.window_id),
            )
            for w in windows
        ],
        projects=sorted({s.project_path for s, _, _ in sessions if s.project_path}),
        sessions=[built.key for _, _, built in sessions],
        todos=todos,
    )


BUILD_SOURCE: Final = "gh"
DEFAULT_POLL_SECONDS: Final = 300.0
"""The poll interval a stored build falls back to when it carries none."""


def _pull_request(row: ProjectRow | None) -> PullRequest | None:
    """The PR of the branch; ``pr_number`` 0 is "looked up, there is none"."""
    if row is None or not row.pr_number:
        return None
    return PullRequest(
        number=row.pr_number,
        state=row.pr_state or None,
        review_decision=row.review_decision or None,
    )


def _build(row: ProjectRow | None, now: datetime) -> Build | None:
    """§4.7: the stored fold with its freshness, or ``None`` before any observation."""
    if row is None or row.build is None or row.build_observed_at is None:
        return None
    stored = row.build
    interval = stored.get("poll_interval_s")
    if isinstance(interval, bool) or not isinstance(interval, int | float) or interval <= 0:
        interval = DEFAULT_POLL_SECONDS
    try:
        return Build.model_validate(
            {
                "state": stored.get("state", row.build_state),
                "head_sha": stored.get("head_sha"),
                "url": stored.get("url"),
                "workflows": stored.get("workflows", []),
                "source": BUILD_SOURCE,
                "observed_at": row.build_observed_at,
                "freshness": freshness(
                    row.build_observed_at, now, float(interval), row.build_error_kind
                ),
                "error_kind": row.build_error_kind,
            }
        )
    except (ValidationError, TypeError):
        logger.warning("a stored build could not be read")
        return None


def _project(
    path: str,
    row: ProjectRow | None,
    placement: ProjectPlacement | None,
    sessions: Sequence[str],
    todos: list[Todo] | None = None,
    now: datetime | None = None,
) -> Project:
    repo_name = None if row is None else row.repo_name
    group = None if row is None else row.group_name
    if group is None and placement is not None:
        group = placement.group_name
    return Project(
        path=path,
        name=project_name(path, repo_name=repo_name, group_name=group) or path,
        repo_root=(None if row is None else row.repo_root)
        or (None if placement is None else placement.repo_root),
        repo_name=repo_name,
        remote_url=None if row is None else row.remote_url,
        group=group,
        workspace=None if placement is None else placement.workspace,
        sessions=list(sessions),
        branch=None if row is None else row.branch,
        dirty=None if row is None else row.dirty,
        ahead=None if row is None else row.ahead,
        behind=None if row is None else row.behind,
        pr=_pull_request(row),
        build=None if now is None else _build(row, now),
        last_commit_at=None if row is None else row.last_commit_at,
        todos=todos,
        updated_at=None if row is None else row.updated_at,
    )


def next_items(published: Published) -> list[NextItem]:
    """The ``/now`` items of a published list, top item first."""
    return [
        NextItem(
            suggestion_id=item.suggestion_id,
            kind=item.kind,
            target_ref=item.target_ref,
            score=item.score,
            reason=item.reason,
            rank=item.rank,
        )
        for item in published.items
    ]


def _focused_workspace(workspaces: Sequence[Workspace] | None) -> str | None:
    """The first focused workspace, in natural order."""
    return next((w.name for w in workspaces or () if w.focused), None)


def collector_info(state: CollectorState) -> CollectorInfo:
    health = state.health
    return CollectorInfo(
        name=state.name,
        origin="built-in" if state.origin is Origin.BUILT_IN else "external",
        state=health.state,
        since=health.since,
        last_ok_at=health.last_ok_at,
        restarts=health.restarts,
        last_error_kind=health.last_error_kind,
        detail=health.detail,
    )


def _capabilities(collectors: Iterable[CollectorState]) -> frozenset[Capability]:
    found: set[Capability] = set()
    for collector in collectors:
        if collector.available:
            found |= collector.capabilities
    return frozenset(found)


# --------------------------------------------------------------------------------------------
# The whole picture
# --------------------------------------------------------------------------------------------


def build_now(state: StateView) -> Now:
    """``fylgja.now/1`` for ``state`` (see the module docstring)."""
    live = state.live
    available = {c.name for c in state.collectors if c.available}
    capabilities = _capabilities(state.collectors)
    show_windows = Capability.WINDOWS in capabilities
    show_sessions = Capability.SESSIONS in capabilities
    show_projects = show_sessions or Capability.PROJECT_STATE in capabilities

    todos = _todo_index(live.todos.values())
    contexts, summaries = _context_index(state.contexts, available)
    windows = (
        sorted((w for w in live.windows.values() if w.source in available), key=_window_key)
        if show_windows
        else []
    )
    focus = live.focused
    if not show_windows or focus is None or focus.source not in available:
        focus = None
    session_rows = (
        sorted(
            (s for s in live.sessions.values() if s.source in available and s.ended_at is None),
            key=lambda s: s.session_key,
        )
        if show_sessions
        else []
    )
    stories = state.summaries if state.summaries is not None and state.summaries.enabled else None
    built: list[tuple[SessionRow, ActivityRow | None, Session]] = []
    for row in session_rows:
        key = row.session_key
        activity = live.activity.get(key)
        session = _session(
            row,
            activity,
            state.sessions.get(key),
            state.placements.get(key),
            _todos_of(todos, "session", key),
            state.generated_at,
        )
        built.append((row, activity, with_session_story(stories, session)))

    workspaces: list[Workspace] | None = None
    if show_windows:
        rows = {n: r for n, r in live.workspaces.items() if r.source in available}
        names = set(rows)
        names.update(w.workspace for w in windows if w.workspace)
        names.update(s.workspace for s in session_rows if s.workspace)
        by_key = {session.key: session for _, _, session in built}
        workspaces = [
            with_workspace_story(
                stories,
                _workspace(
                    name,
                    rows.get(name),
                    state.labels.get(name),
                    [w for w in windows if w.workspace == name],
                    [item for item in built if item[0].workspace == name],
                    focus,
                    _todos_of(todos, "workspace", name),
                    contexts,
                    summaries,
                ),
                by_key,
            )
            for name in sorted(names, key=natural_key)
        ]

    projects: list[Project] | None = None
    if show_projects:
        members: dict[str, list[str]] = {}
        for row in session_rows:
            if row.project_path:
                members.setdefault(row.project_path, []).append(row.session_key)
        projects = [
            _project(
                path,
                live.projects.get(path),
                state.projects.get(path),
                keys,
                _todos_of(todos, "project", path),
                state.generated_at,
            )
            for path, keys in sorted(members.items())
        ]

    waiting: list[Waiting] | None = None
    ranked: Published | None = None
    if show_sessions or show_windows or show_projects:
        entries = waiting_on_me(
            now=state.generated_at,
            sessions=session_rows,
            activity=live.activity,
            projects=live.projects.values() if show_projects else (),
            contexts=_available_contexts(state.contexts, available) if show_windows else None,
            touched=state.touched,
        )
        if state.next_lists is not None:
            ranked = state.next_lists.publish(
                rank_next(
                    now=state.generated_at,
                    waiting=entries,
                    todos=live.todos.values(),
                    sessions=session_rows,
                    activity=live.activity,
                    focused_workspace=_focused_workspace(workspaces),
                    focus_seconds=state.focus_today,
                ),
                state.generated_at,
            )
        waiting = [
            Waiting(
                kind=entry.kind,
                target=entry.target,
                target_kind=entry.target_kind,
                reason=entry.reason,
                since=entry.since,
            )
            for entry in entries
        ]

    collectors = [collector_info(c) for c in state.collectors]
    return Now(
        generated_at=state.generated_at,
        focused=None
        if focus is None
        else _focused(
            focus,
            {w.window_id: w for w in windows},
            session_rows,
            None if focus.window_id is None else contexts.get(focus.window_id),
        ),
        workspaces=workspaces,
        sessions=_nested([session for _, _, session in built]) if show_sessions else None,
        projects=projects,
        waiting=waiting,
        next=None if ranked is None else next_items(ranked),
        next_list_id=None if ranked is None else ranked.list_id,
        usage=build_usage(live.usage, state.usage, available, state.generated_at),
        collectors=collectors,
        degraded=sorted({c.name for c in state.collectors if c.degraded}),
    )
