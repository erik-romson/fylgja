"""Waiting-on-me: everything that waits for the person, merged and ordered (WP6.1).

``PLAN-final-plan.md`` §5 WP6.1, §4.7, A15; ``PLAN.md`` §7. :func:`waiting_on_me` is a pure function
of the live view's rows, the window context items and the projects the person touched today. It
reads no clock and does no I/O; ``/now`` calls it on every request
(:func:`fylgja.api.now.build_now`) and the live view's change notification is what tells
clients to fetch again.

**Sources**, in rank order (the ``PLAN.md`` §7 ranking, items 1, 2, 4 and 6):

0. a session in ``waiting_permission`` (``permission``) or ``waiting_input`` (``input``): Claude
   through its hooks, norn runs through their waiting state;
1. a finished turn nobody has looked at (``turn_done``; ``seen`` ends it), or a finished run
   (norn) whose project has had no focus since it ended (``run_finished``, decision 0056);
2. a failed build (``build_failed``, ``build.state == "fail"``) or a PR whose review is required
   (``review_requested``), on a project touched today;
3. an explicit unread marker of an app (``unread``, A15): titles only, lowest rank. Nothing is
   inferred where the app set no marker.

**One target counts once.** A session, a project and a window are the targets; when one qualifies
twice the higher-ranked entry stays (a waiting session that also shows an unseen turn is listed
as waiting).

**Order.** Rank first, then age: the oldest ``since`` first, an entry without one last, the target
as the final tie-break so the order is stable.

**Touched today** means a focus span of the project or session activity (a state change or a new
activity interval) since local midnight. The daemon supplies the last focus time per project
(``touched``).

An unread entry's ``since`` is when its marker was last observed: the context memory keeps only
the current item of a window.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any, Final

from fylgja.core.context_items import ContextEntry
from fylgja.core.labels import project_name
from fylgja.store.rows import ActivityRow, ProjectRow, SessionRow

__all__ = [
    "KINDS",
    "RANKS",
    "WaitingEntry",
    "start_of_day",
    "waiting_on_me",
]

PERMISSION: Final = "permission"
INPUT: Final = "input"
TURN_DONE: Final = "turn_done"
RUN_FINISHED: Final = "run_finished"
BUILD_FAILED: Final = "build_failed"
REVIEW_REQUESTED: Final = "review_requested"
UNREAD: Final = "unread"

KINDS: Final = (PERMISSION, INPUT, TURN_DONE, RUN_FINISHED, BUILD_FAILED, REVIEW_REQUESTED, UNREAD)
"""Every ``kind`` an entry can carry."""

RANKS: Final[Mapping[str, int]] = {
    PERMISSION: 0,
    INPUT: 0,
    TURN_DONE: 1,
    RUN_FINISHED: 1,
    BUILD_FAILED: 2,
    REVIEW_REQUESTED: 2,
    UNREAD: 3,
}
"""The ``PLAN.md`` §7 rank of each kind; a lower number is more urgent."""

_KIND_ORDER: Final = {kind: index for index, kind in enumerate(KINDS)}
"""Within one target the first kind in :data:`KINDS` wins a tie of rank."""

_SESSION_STATES: Final[Mapping[str, str]] = {
    "waiting_permission": PERMISSION,
    "waiting_input": INPUT,
    "turn_done": TURN_DONE,
}
_RUN_OUTCOMES: Final = frozenset({"passed", "failed", "cancelled"})
"""The terminal states of a run (the ``/now`` vocabulary of norn)."""
_CLOSED_PR: Final = frozenset({"CLOSED", "MERGED"})
_REVIEW_REQUIRED: Final = "REVIEW_REQUIRED"
_MAX_REASON: Final = 120


@dataclass(frozen=True)
class WaitingEntry:
    """One thing that waits on the person."""

    kind: str
    target: str
    """A session key, a project path or a window id."""
    target_kind: str
    """``session``, ``project`` or ``window``."""
    reason: str
    since: datetime | None

    @property
    def rank(self) -> int:
        return RANKS[self.kind]


def start_of_day(now: datetime, tz: tzinfo | None = None) -> datetime:
    """The start of ``now``'s calendar day in ``tz`` (the machine's local zone by default)."""
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_REASON else text[: _MAX_REASON - 1] + "…"


def _moment(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


# --------------------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------------------


def _session_reason(kind: str, waiting_reason: str | None) -> str:
    if kind == TURN_DONE:
        return "finished a turn nobody has looked at"
    base = "waiting for permission" if kind == PERMISSION else "waiting for input"
    return f"{base}: {_clip(waiting_reason)}" if waiting_reason else base


def _session_entries(
    sessions: Iterable[SessionRow], activity: Mapping[str, ActivityRow]
) -> list[WaitingEntry]:
    found: list[WaitingEntry] = []
    for row in sessions:
        if row.ended_at is not None:
            continue
        current = activity.get(row.session_key)
        # The session row carries the state engine's verdict (a ``seen`` turn is ``seen`` here);
        # the activity interval is only the fallback of a row that has no state yet.
        state: str | None
        since: datetime | None
        if row.state is None and current is not None:
            state, since, reason = current.state, current.valid_from, current.waiting_reason
        else:
            state, since, reason = row.state, row.state_since, row.waiting_reason
        kind = _SESSION_STATES.get(state or "")
        if kind is None:
            continue
        found.append(
            WaitingEntry(
                kind=kind,
                target=row.session_key,
                target_kind="session",
                reason=_session_reason(kind, reason),
                since=since or row.first_seen,
            )
        )
    return found


def _run_entries(
    sessions: Iterable[SessionRow], touched: Mapping[str, datetime]
) -> list[WaitingEntry]:
    """A finished run that is still listed and whose project had no focus since it ended."""
    found: list[WaitingEntry] = []
    for row in sessions:
        if row.ended_at is not None or row.state not in _RUN_OUTCOMES:
            continue
        since = row.state_since or row.first_seen
        focused = None if row.project_path is None else touched.get(row.project_path)
        if focused is not None and focused >= since:
            continue
        found.append(
            WaitingEntry(
                kind=RUN_FINISHED,
                target=row.session_key,
                target_kind="session",
                reason=f"{row.name or row.kind} {row.state}",
                since=since,
            )
        )
    return found


def _touched_today(
    path: str,
    sessions: Sequence[SessionRow],
    activity: Mapping[str, ActivityRow],
    touched: Mapping[str, datetime],
    today: datetime,
) -> bool:
    focused = touched.get(path)
    if focused is not None and focused >= today:
        return True
    for row in sessions:
        if row.project_path != path or row.ended_at is not None:
            continue
        current = activity.get(row.session_key)
        moments = (row.state_since, None if current is None else current.valid_from)
        if any(moment is not None and moment >= today for moment in moments):
            return True
    return False


def _build_state(row: ProjectRow) -> str | None:
    stored: Mapping[str, Any] | None = row.build
    if stored is not None and isinstance(stored.get("state"), str):
        return str(stored["state"])
    return row.build_state


def _failed_since(row: ProjectRow) -> datetime | None:
    stored: Mapping[str, Any] = row.build or {}
    workflows = stored.get("workflows")
    starts: list[datetime] = []
    if isinstance(workflows, list):
        for workflow in workflows:
            if isinstance(workflow, dict) and workflow.get("state") == "fail":
                moment = _moment(workflow.get("run_started_at"))
                if moment is not None:
                    starts.append(moment)
    return min(starts) if starts else (row.build_observed_at or row.updated_at)


def _project_entries(
    projects: Iterable[ProjectRow],
    sessions: Sequence[SessionRow],
    activity: Mapping[str, ActivityRow],
    touched: Mapping[str, datetime],
    today: datetime,
) -> list[WaitingEntry]:
    found: list[WaitingEntry] = []
    for row in projects:
        failed = _build_state(row) == "fail"
        review = (
            bool(row.pr_number)
            and (row.review_decision or "").upper() == _REVIEW_REQUIRED
            and (row.pr_state or "").upper() not in _CLOSED_PR
        )
        if not (failed or review) or not _touched_today(
            row.path, sessions, activity, touched, today
        ):
            continue
        name = (
            project_name(row.path, repo_name=row.repo_name, group_name=row.group_name) or row.path
        )
        if failed:
            found.append(
                WaitingEntry(
                    kind=BUILD_FAILED,
                    target=row.path,
                    target_kind="project",
                    reason=f"build failed on {name}",
                    since=_failed_since(row),
                )
            )
        if review:
            found.append(
                WaitingEntry(
                    kind=REVIEW_REQUESTED,
                    target=row.path,
                    target_kind="project",
                    reason=f"review required on the pull request of {name}",
                    since=row.updated_at,
                )
            )
    return found


def _has_marker(entry: ContextEntry) -> bool:
    marker = entry.unread
    return marker is True or (
        isinstance(marker, int) and not isinstance(marker, bool) and marker > 0
    )


def _unread_entries(contexts: Mapping[str, Sequence[ContextEntry]]) -> list[WaitingEntry]:
    """One entry per window with an explicit unread marker (A15), titles only."""
    found: list[WaitingEntry] = []
    for window_id, entries in contexts.items():
        marked = [entry for entry in entries if _has_marker(entry)]
        if not marked:
            continue
        first = min(marked, key=lambda e: e.observed_at)
        count = sum(
            e.unread for e in marked if isinstance(e.unread, int) and not isinstance(e.unread, bool)
        )
        title = next((e.title for e in marked if e.title), None)
        where = f" in {_clip(title)}" if title else ""
        reason = f"{count} unread{where}" if count else f"unread{where}"
        found.append(
            WaitingEntry(
                kind=UNREAD,
                target=window_id,
                target_kind="window",
                reason=reason,
                since=first.observed_at,
            )
        )
    return found


# --------------------------------------------------------------------------------------------
# The merge
# --------------------------------------------------------------------------------------------


def _order(entry: WaitingEntry) -> tuple[int, bool, datetime, str, int]:
    since = entry.since
    return (
        entry.rank,
        since is None,
        since if since is not None else datetime.min.replace(tzinfo=UTC),
        entry.target,
        _KIND_ORDER[entry.kind],
    )


def waiting_on_me(
    *,
    now: datetime,
    sessions: Iterable[SessionRow] = (),
    activity: Mapping[str, ActivityRow] | None = None,
    projects: Iterable[ProjectRow] = (),
    contexts: Mapping[str, Sequence[ContextEntry]] | None = None,
    touched: Mapping[str, datetime] | None = None,
    tz: tzinfo | None = None,
) -> list[WaitingEntry]:
    """Everything waiting on the person, most urgent first (see the module docstring).

    ``sessions``, ``projects`` and ``contexts`` are what the caller shows: the rows of available
    collectors only. ``touched`` is the last focus time per project path.
    """
    live = list(sessions)
    current = {} if activity is None else activity
    focused = {} if touched is None else touched
    today = start_of_day(now, tz)
    candidates = [
        *_session_entries(live, current),
        *_run_entries(live, focused),
        *_project_entries(projects, live, current, focused, today),
        *_unread_entries({} if contexts is None else contexts),
    ]
    best: dict[tuple[str, str], WaitingEntry] = {}
    for entry in sorted(candidates, key=lambda e: (e.rank, _KIND_ORDER[e.kind])):
        best.setdefault((entry.target_kind, entry.target), entry)
    return sorted(best.values(), key=_order)
