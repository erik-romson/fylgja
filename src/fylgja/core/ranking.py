"""The "what next" ranking: one score per candidate, explained in words (WP6.2).

``PLAN-final-plan.md`` §5 WP6.2; ``PLAN.md`` §7. :func:`rank_next` is a pure function of the
waiting-on-me list (step 43), the todos, the live sessions and their activity, the focused
workspace and the focus seconds of today. It reads no clock and does no I/O; the caller passes
``now``. For one live view and one ``now`` the answer is always the same list.

**The six rules**, in the order of ``PLAN.md`` §7 (a higher score is more urgent):

1. an agent waiting for permission or input (``permission``, ``input``): 100;
2. an agent turn finished and not seen (``turn_done``), or a run finished and its project not
   focused since (``run_finished``): 90;
3. a todo marked ``next`` in the focused workspace (``todo``): 80. A todo scoped to a project or a
   session counts for the workspace its session sits in. The todo model has no due date, so
   "due" has no input yet and only the ``next`` mark counts;
4. a build failed or a review requested, on a project touched today (``build_failed``,
   ``review_requested``): 70. The waiting list already filtered by "touched today";
5. the workspace with the most focus today that has an open todo but no session activity in the
   last hour, and is not the one in focus: a way back (``context_switch``): 60. Only one workspace
   is offered;
6. an unread marker of Teams, Slack or another app (``unread``): 50, lowest, titles only (A15).

Rules 1, 2, 4 and 6 come from ``waiting[]`` as it is, so a target that qualifies twice is already
counted once there and keeps the reason it was given.

**Ties are broken by age**: among equal scores the oldest ``since`` comes first, a candidate
without one last, then the kind and the target so that the order is total. A reason never holds a
time or a duration that changes from one call to the next, so an unchanged situation is an
unchanged list.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from fylgja.core.waiting import (
    BUILD_FAILED,
    INPUT,
    PERMISSION,
    REVIEW_REQUESTED,
    RUN_FINISHED,
    TURN_DONE,
    UNREAD,
    WaitingEntry,
)
from fylgja.store.rows import ActivityRow, SessionRow, TodoRow

__all__ = [
    "CONTEXT_SWITCH",
    "IDLE_WINDOW",
    "MAX_ITEMS",
    "SCORES",
    "TODO",
    "Candidate",
    "rank_next",
]

TODO: Final = "todo"
CONTEXT_SWITCH: Final = "context_switch"

SCORES: Final[Mapping[str, float]] = {
    PERMISSION: 100.0,
    INPUT: 100.0,
    TURN_DONE: 90.0,
    RUN_FINISHED: 90.0,
    TODO: 80.0,
    BUILD_FAILED: 70.0,
    REVIEW_REQUESTED: 70.0,
    CONTEXT_SWITCH: 60.0,
    UNREAD: 50.0,
}
"""The score of each kind of candidate: the ``PLAN.md`` §7 rule it belongs to."""

IDLE_WINDOW: Final = timedelta(hours=1)
"""Rule 5: a workspace is a way back when none of its sessions did anything for this long."""
MAX_ITEMS: Final = 10
"""A list never holds more items than this."""

_KIND_ORDER: Final = {kind: index for index, kind in enumerate(SCORES)}
_MAX_TEXT: Final = 100
_NEXT: Final = "next"
_ACTIVE: Final = frozenset({"open", "next"})
_EPOCH: Final = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Candidate:
    """One thing to do next."""

    kind: str
    target_ref: str
    """A session key, a project path, a window id, a todo id or a workspace name."""
    score: float
    reason: str
    since: datetime | None = None
    """When it began waiting (the age that breaks a tie of score)."""


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


def _order(candidate: Candidate) -> tuple[float, bool, datetime, int, str]:
    since = candidate.since
    return (
        -candidate.score,
        since is None,
        _EPOCH if since is None else since,
        _KIND_ORDER.get(candidate.kind, len(_KIND_ORDER)),
        candidate.target_ref,
    )


def _workspaces_of(todo: TodoRow, sessions: Sequence[SessionRow]) -> set[str]:
    """The workspaces a todo belongs to: its own, or those of the sessions it is scoped to."""
    ref = todo.scope_ref
    if todo.scope_kind is None or ref is None:
        return set()
    if todo.scope_kind == "workspace":
        return {ref}
    if todo.scope_kind == "session":
        return {s.workspace for s in sessions if s.session_key == ref and s.workspace}
    if todo.scope_kind == "project":
        return {s.workspace for s in sessions if s.project_path == ref and s.workspace}
    return set()


def _todo_candidates(
    todos: Iterable[TodoRow], sessions: Sequence[SessionRow], focused: str | None
) -> list[Candidate]:
    """Rule 3: the todos marked ``next`` that belong to the focused workspace."""
    if focused is None:
        return []
    return [
        Candidate(
            kind=TODO,
            target_ref=todo.id,
            score=SCORES[TODO],
            reason=f"todo marked next in workspace {focused}: {_clip(todo.text)}",
            since=todo.created_at,
        )
        for todo in todos
        if todo.status == _NEXT and focused in _workspaces_of(todo, sessions)
    ]


def _active_workspaces(
    sessions: Sequence[SessionRow], activity: Mapping[str, ActivityRow], now: datetime
) -> set[str]:
    """The workspaces where a session changed state or activity within :data:`IDLE_WINDOW`."""
    since = now - IDLE_WINDOW
    found: set[str] = set()
    for row in sessions:
        if not row.workspace:
            continue
        current = activity.get(row.session_key)
        moments = (row.state_since, None if current is None else current.valid_from)
        if any(moment is not None and moment >= since for moment in moments):
            found.add(row.workspace)
    return found


def _switch_candidates(
    todos: Iterable[TodoRow],
    sessions: Sequence[SessionRow],
    activity: Mapping[str, ActivityRow],
    focused: str | None,
    focus_seconds: Mapping[str, float],
    now: datetime,
) -> list[Candidate]:
    """Rule 5: the way back to the workspace with the most focus today that has gone quiet."""
    open_todos: dict[str, list[TodoRow]] = {}
    for todo in todos:
        if todo.status not in _ACTIVE:
            continue
        for workspace in _workspaces_of(todo, sessions):
            open_todos.setdefault(workspace, []).append(todo)
    busy = _active_workspaces(sessions, activity, now)
    eligible = [
        (-seconds, name)
        for name, seconds in focus_seconds.items()
        if seconds > 0 and name in open_todos and name != focused and name not in busy
    ]
    if not eligible:
        return []
    name = min(eligible)[1]
    waiting = open_todos[name]
    count = len(waiting)
    noun = "an open todo" if count == 1 else f"{count} open todos"
    return [
        Candidate(
            kind=CONTEXT_SWITCH,
            target_ref=name,
            score=SCORES[CONTEXT_SWITCH],
            reason=(
                f"workspace {name} has had the most focus today, holds {noun} "
                "and has been quiet for an hour"
            ),
            since=min(todo.created_at for todo in waiting),
        )
    ]


def rank_next(
    *,
    now: datetime,
    waiting: Iterable[WaitingEntry] = (),
    todos: Iterable[TodoRow] = (),
    sessions: Iterable[SessionRow] = (),
    activity: Mapping[str, ActivityRow] | None = None,
    focused_workspace: str | None = None,
    focus_seconds: Mapping[str, float] | None = None,
    limit: int = MAX_ITEMS,
) -> list[Candidate]:
    """The candidates, best first (see the module docstring), at most ``limit`` of them.

    ``waiting`` is :func:`fylgja.core.waiting.waiting_on_me`'s answer; ``todos`` are the open and
    next todos; ``sessions`` the live ones the caller shows; ``focus_seconds`` the seconds of focus
    today per workspace.
    """
    live = [s for s in sessions if s.ended_at is None]
    todo_rows = list(todos)
    candidates = [
        Candidate(
            kind=entry.kind,
            target_ref=entry.target,
            score=SCORES[entry.kind],
            reason=entry.reason,
            since=entry.since,
        )
        for entry in waiting
    ]
    candidates += _todo_candidates(todo_rows, live, focused_workspace)
    candidates += _switch_candidates(
        todo_rows,
        live,
        {} if activity is None else activity,
        focused_workspace,
        {} if focus_seconds is None else focus_seconds,
        now,
    )
    return sorted(candidates, key=_order)[: max(limit, 0)]
