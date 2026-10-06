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
* **Projects** are the projects of the live sessions, ordered by path.
* **Todos** (WP4.1): the open and next items of the live view fill ``workspaces[].todos``,
  ``sessions[].todos`` and ``projects[].todos`` by scope; ``next`` items come first. An entry
  with no todos has no ``todos`` field.

Titles, names and prompt text arrive redacted (the ingest gate ran before the store); nothing here
logs them.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import ValidationError

from fylgja.api.now_models import (
    CollectorInfo,
    Facts,
    Focused,
    Host,
    LabelSource,
    Now,
    OneLinerRule,
    Project,
    Session,
    SessionState,
    Stage,
    Todo,
    Window,
    Workspace,
)
from fylgja.collectors.base import Capability
from fylgja.collectors.registry import Origin
from fylgja.core.daemon import CollectorState, SessionExtras, StateView
from fylgja.core.labels import EMPTY_LABEL, effective_label, project_name
from fylgja.core.one_liner import WorkspaceSession, session_one_liner, workspace_one_liner
from fylgja.core.resolver import Placement, ProjectPlacement
from fylgja.models import SessionActivity
from fylgja.models import Stage as ActivityStage
from fylgja.store.rows import (
    ActivityRow,
    FocusRow,
    ProjectRow,
    SessionRow,
    TodoRow,
    WindowRow,
    WorkspaceRow,
)

__all__ = ["build_now", "collector_info", "natural_key", "todo_model"]

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


def natural_key(name: str) -> tuple[int, int, str, str]:
    """Sort key of a workspace name: numbers numerically first, then the rest by name."""
    if name.isdigit():
        return (0, int(name), "", name)
    return (1, 0, name.casefold(), name)


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
    focus: FocusRow, windows: Mapping[str, WindowRow], sessions: Sequence[SessionRow]
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
    )


def _workspace(
    name: str,
    row: WorkspaceRow | None,
    inferred_fallback: str | None,
    windows: Sequence[WindowRow],
    sessions: Sequence[tuple[SessionRow, ActivityRow | None, Session]],
    focus: FocusRow | None,
    todos: list[Todo] | None = None,
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
    return Workspace(
        name=name,
        label=label,
        label_source=LabelSource(source),
        inferred_label=inferred,
        one_liner=workspace_one_liner(label, members),
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
            )
            for w in windows
        ],
        projects=sorted({s.project_path for s, _, _ in sessions if s.project_path}),
        sessions=[built.key for _, _, built in sessions],
        todos=todos,
    )


def _project(
    path: str,
    row: ProjectRow | None,
    placement: ProjectPlacement | None,
    sessions: Sequence[str],
    todos: list[Todo] | None = None,
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
        last_commit_at=None if row is None else row.last_commit_at,
        todos=todos,
        updated_at=None if row is None else row.updated_at,
    )


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
        )
        built.append((row, activity, session))

    workspaces: list[Workspace] | None = None
    if show_windows:
        rows = {n: r for n, r in live.workspaces.items() if r.source in available}
        names = set(rows)
        names.update(w.workspace for w in windows if w.workspace)
        names.update(s.workspace for s in session_rows if s.workspace)
        workspaces = [
            _workspace(
                name,
                rows.get(name),
                state.labels.get(name),
                [w for w in windows if w.workspace == name],
                [item for item in built if item[0].workspace == name],
                focus,
                _todos_of(todos, "workspace", name),
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
            )
            for path, keys in sorted(members.items())
        ]

    collectors = [collector_info(c) for c in state.collectors]
    return Now(
        generated_at=state.generated_at,
        focused=None
        if focus is None
        else _focused(focus, {w.window_id: w for w in windows}, session_rows),
        workspaces=workspaces,
        sessions=_nested([session for _, _, session in built]) if show_sessions else None,
        projects=projects,
        collectors=collectors,
        degraded=sorted({c.name for c in state.collectors if c.degraded}),
    )
