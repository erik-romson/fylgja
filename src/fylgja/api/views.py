"""Builds the page-shaped views of the navigator (``docs/api/views.md``, ``docs/decisions/0047``).

**The daemon computes; the page renders.** Every rule the Overview tab once ran in JavaScript runs
here instead, unchanged, so every client gets the same answer and the rules are unit-tested:

* **Caps.** A card shows at most :data:`MAX_SESSIONS` sessions, :data:`MAX_CONTEXT` context items
  and :data:`MAX_TODOS` todos; ``sessions_more``, ``context_more`` and ``todos_more`` count the
  rest.
* **Session order.** A card's waiting sessions (:data:`WAITING_STATES`) come first, otherwise the
  ``/now`` order holds.
* **Cards.** Only a workspace with a window, a session or a todo gets a card. Its click target is
  its focused window, else its first window.
* **Next targets.** A Next item's ``target_ref`` focuses: a session key, that session; a project
  path with a workspace, that workspace's window; a window id, that window; a workspace name, its
  window; the id of an open todo of workspace scope, that workspace's window.
* **Todos.** The focused workspace's todos come first, then every other todo under one heading per
  scope, in first-seen order, titled with the session, project or workspace it belongs to.

:func:`build_overview` is pure: it reads only its arguments, imports no web framework and does no
I/O. Titles arrive redacted, and nothing here logs.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from typing import Final

from fylgja.api.now_models import ContextItem, NextItem, Now, Project, Session, Todo, Workspace
from fylgja.api.view_models import (
    Card,
    CardContext,
    CardProject,
    CardSession,
    CardTodo,
    FocusTarget,
    NextEntry,
    NextList,
    Overview,
    TodoGroup,
    TodoRow,
)
from fylgja.core.todo_import import NATIVE_SOURCE, SCOPE_PROJECT, SCOPE_SESSION, SCOPE_WORKSPACE

__all__ = [
    "MAX_CONTEXT",
    "MAX_SESSIONS",
    "MAX_TODOS",
    "WAITING_STATES",
    "build_overview",
    "scope_title",
]

MAX_SESSIONS: Final = 6
"""Sessions shown on one card."""
MAX_CONTEXT: Final = 3
"""Context items shown on one card."""
MAX_TODOS: Final = 4
"""Todos shown on one card."""

WAITING_STATES: Final = frozenset({"waiting", "waiting_permission", "waiting_input", "turn_done"})
"""The session states that put a session first on its card and mark it as waiting."""

_NEXT_STATUS: Final = "next"
_BUILD_FAILED: Final = "fail"
"""The ``projects[].build.state`` of a failed build."""

_TAIL_CHARS: Final = 40
_ELLIPSIS: Final = "…"

_Sessions = Mapping[str, Session]
_Projects = Mapping[str, Project]
_Workspaces = Mapping[str, Workspace]


# --------------------------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------------------------


def _walk(sessions: Iterable[Session]) -> Iterator[Session]:
    """Every session, each followed by its children (depth first)."""
    for session in sessions:
        yield session
        yield from _walk(session.children or ())


def _sessions_by_key(now: Now) -> dict[str, Session]:
    return {session.key: session for session in _walk(now.sessions or ())}


def _window_target(workspace: Workspace | None) -> FocusTarget | None:
    """The focused window of ``workspace``, else its first; ``None`` without a window."""
    if workspace is None or not workspace.windows:
        return None
    here = next((w for w in workspace.windows if w.focused), workspace.windows[0])
    return FocusTarget(kind="window", ref=here.id)


def _focused_workspace(now: Now) -> str | None:
    """The workspace ``focused`` names, else the first one flagged as focused."""
    if now.focused is not None and now.focused.workspace:
        return now.focused.workspace
    return next((w.name for w in now.workspaces or () if w.focused), None)


def _label(workspace: Workspace) -> str | None:
    return workspace.label or workspace.inferred_label or None


# --------------------------------------------------------------------------------------------
# Next
# --------------------------------------------------------------------------------------------


def _next_target(
    target_ref: str,
    now: Now,
    workspaces: _Workspaces,
    todos: Sequence[Todo],
) -> FocusTarget | None:
    """What a click on a Next item focuses (the rules of the module docstring, in order)."""
    if any(s.key == target_ref for s in now.sessions or ()):
        return FocusTarget(kind="session", ref=target_ref)
    project = next((p for p in now.projects or () if p.path == target_ref), None)
    if project is not None and project.workspace:
        return _window_target(workspaces.get(project.workspace))
    for workspace in now.workspaces or ():
        if any(w.id == target_ref for w in workspace.windows):
            return FocusTarget(kind="window", ref=target_ref)
        if workspace.name == target_ref:
            return _window_target(workspace)
    todo = next((t for t in todos if t.id == target_ref), None)
    if todo is not None and todo.scope_kind == SCOPE_WORKSPACE and todo.scope_ref:
        return _window_target(workspaces.get(todo.scope_ref))
    return None


def _next_entries(
    items: Sequence[NextItem], now: Now, workspaces: _Workspaces, todos: Sequence[Todo]
) -> list[NextEntry]:
    return [
        NextEntry(
            rank=item.rank if item.rank is not None else position,
            kind=item.kind,
            reason=item.reason,
            go=_next_target(item.target_ref, now, workspaces, todos),
        )
        for position, item in enumerate(items, start=1)
    ]


# --------------------------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------------------------


def _is_busy(workspace: Workspace) -> bool:
    return bool(workspace.windows or workspace.sessions or workspace.todos)


def _card_project(project: Project) -> CardProject:
    build_state = None if project.build is None else project.build.state
    return CardProject(
        name=project.name or project.path,
        branch=project.branch or None,
        dirty=bool(project.dirty),
        pr_number=project.pr.number if project.pr is not None and project.pr.number else None,
        pr_checks=None if project.pr is None else project.pr.checks or None,
        build_state=build_state or None,
        build_failed=build_state == _BUILD_FAILED,
    )


def _is_waiting(session: Session) -> bool:
    return session.state is not None and session.state.value in WAITING_STATES


def _card_session(session: Session) -> CardSession:
    return CardSession(
        key=session.key,
        state=session.state.value if session.state is not None else "unknown",
        waiting=_is_waiting(session),
        text=session.one_liner or session.title or session.name or session.kind,
        go=FocusTarget(kind="session", ref=session.key),
    )


def _card_context(item: ContextItem) -> CardContext:
    return CardContext(text=item.title or item.kind, unread=bool(item.unread))


def _more(total: int, cap: int) -> int:
    return max(total - cap, 0)


def _card(workspace: Workspace, sessions: _Sessions, projects: _Projects) -> Card:
    mine = [sessions[key] for key in workspace.sessions if key in sessions]
    # waiting first; the sort is stable, so the /now order holds otherwise
    mine.sort(key=lambda s: not _is_waiting(s))
    where = [projects[path] for path in workspace.projects if path in projects]
    context = [item for window in workspace.windows for item in window.context or ()]
    todos = workspace.todos or []
    return Card(
        name=workspace.name,
        label=_label(workspace),
        focused=workspace.focused,
        windows=len(workspace.windows),
        one_liner=workspace.one_liner or None,
        go=_window_target(workspace),
        projects=[_card_project(p) for p in where],
        sessions=[_card_session(s) for s in mine[:MAX_SESSIONS]],
        sessions_more=_more(len(mine), MAX_SESSIONS),
        context=[_card_context(c) for c in context[:MAX_CONTEXT]],
        context_more=_more(len(context), MAX_CONTEXT),
        todos=[CardTodo(text=t.text, next=t.status == _NEXT_STATUS) for t in todos[:MAX_TODOS]],
        todos_more=_more(len(todos), MAX_TODOS),
    )


def _ordered(cards: list[Card], workspace_order: Sequence[str] | None) -> list[Card]:
    """``cards`` in ``workspace_order``; a card it does not name keeps its place after those."""
    if workspace_order is None:
        return cards
    by_name = {card.name: card for card in cards}
    first = [by_name.pop(name) for name in dict.fromkeys(workspace_order) if name in by_name]
    return first + [card for card in cards if card.name in by_name]


# --------------------------------------------------------------------------------------------
# Todos
# --------------------------------------------------------------------------------------------


def _base_name(path: str | None) -> str:
    parts = [part for part in (path or "").split("/") if part]
    return parts[-1] if parts else ""


def _tail(text: str, size: int) -> str:
    """``text``, or ``…`` and its end when it is longer than ``size`` (``size`` characters)."""
    return _ELLIPSIS + text[1 - size :] if len(text) > size else text


def _project_name(path: str | None, projects: _Projects) -> str:
    project = None if path is None else projects.get(path)
    return (project.name if project is not None else "") or _base_name(path)


def scope_title(
    kind: str | None,
    ref: str | None,
    sessions: _Sessions,
    projects: _Projects,
    workspaces: _Workspaces,
) -> str:
    """The heading of a group of todos: the session, project or workspace they belong to."""
    if kind == SCOPE_SESSION:
        session = None if ref is None else sessions.get(ref)
        if session is None:
            return "Session " + _tail(ref or "", _TAIL_CHARS)
        parts = (
            session.title or session.one_liner or session.name,
            _project_name(session.project_path, projects),
            session.kind,
        )
        return " · ".join(part for part in parts if part)
    if kind == SCOPE_PROJECT:
        return "Project " + _project_name(ref, projects)
    if kind == SCOPE_WORKSPACE:
        workspace = None if ref is None else workspaces.get(ref)
        label = None if workspace is None else _label(workspace)
        return "Workspace " + (ref or "") + (" " + label if label else "")
    return "No scope"


def _todo_row(todo: Todo) -> TodoRow:
    return TodoRow(
        id=todo.id,
        text=todo.text,
        next=todo.status == _NEXT_STATUS,
        source=todo.source or None,
        can_finish=not todo.source or todo.source == NATIVE_SOURCE,
    )


def _todo_groups(
    todos: Sequence[Todo],
    focused: str | None,
    sessions: _Sessions,
    projects: _Projects,
    workspaces: _Workspaces,
) -> list[TodoGroup]:
    groups: list[TodoGroup] = []
    here: list[Todo] = []
    rest: list[Todo] = []
    for todo in todos:
        mine = bool(focused) and todo.scope_kind == SCOPE_WORKSPACE and todo.scope_ref == focused
        (here if mine else rest).append(todo)
    if focused:
        groups.append(
            TodoGroup(
                title="Focused workspace " + focused,
                kind="focused",
                empty=None if here else "Nothing open here",
                todos=[_todo_row(t) for t in here],
            )
        )
    groups.append(
        TodoGroup(
            title="All workspaces",
            kind="all",
            empty=None if rest else "Nothing else open",
            todos=[],
        )
    )
    scopes: dict[tuple[str, str], list[Todo]] = {}
    for todo in rest:
        scopes.setdefault((todo.scope_kind or "", todo.scope_ref or ""), []).append(todo)
    for (kind, ref), members in scopes.items():
        groups.append(
            TodoGroup(
                title=scope_title(kind or None, ref or None, sessions, projects, workspaces),
                kind="scope",
                todos=[_todo_row(t) for t in members],
            )
        )
    return groups


# --------------------------------------------------------------------------------------------
# The Overview
# --------------------------------------------------------------------------------------------


def build_overview(
    now: Now,
    todos: Sequence[Todo],
    next_answer: NextList,
    *,
    generated_at: datetime,
    workspace_order: Sequence[str] | None = None,
) -> Overview:
    """``fylgja.view.overview/1`` from ``/now``, the open and next todos (oldest first, as
    ``GET /todos?status=open,next`` lists them) and the answer of ``GET /next``.

    ``workspace_order`` puts the named workspaces' cards first, in that order; the others follow in
    their ``/now`` order.
    """
    sessions = _sessions_by_key(now)
    projects = {project.path: project for project in now.projects or ()}
    workspaces = {workspace.name: workspace for workspace in now.workspaces or ()}
    focused = _focused_workspace(now)
    cards = [_card(w, sessions, projects) for w in now.workspaces or () if _is_busy(w)]
    return Overview(
        generated_at=generated_at,
        focused_workspace=focused,
        next=_next_entries(next_answer.items, now, workspaces, todos),
        next_list_id=next_answer.list_id,
        cards=_ordered(cards, workspace_order),
        todo_groups=_todo_groups(todos, focused, sessions, projects, workspaces),
    )
