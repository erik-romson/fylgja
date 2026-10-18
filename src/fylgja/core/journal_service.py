"""The journal service (docs/decisions/0057). This module loads no framework.

It grows over the journal steps. For now it holds

* :class:`JournalTracker`, the in-memory state machine that turns live-view updates and focus
  span actions into dirty threads and boundaries (J1–J5);
* :func:`read_commits`, the commit subjects of a window (J8 section 3, D3).

The tracker does no I/O and reads only the rows it is given; :func:`~fylgja.core.journal.due`
says which thread gets an attempt and when to look again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Container, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Protocol, cast

from fylgja.collectors.base import Clock
from fylgja.collectors.seams import ProcessRunner
from fylgja.config import JournalConfig
from fylgja.core.journal import (
    COMMIT_HORIZON,
    CommitPart,
    Source,
    ThreadKey,
    ThreadState,
    Trigger,
    highest_trigger,
    in_window,
    log_argv,
    parse_log,
    session_source,
    thread_key,
    thread_name,
    thread_source,
)
from fylgja.core.live_view import Change, Collection, LiveUpdate
from fylgja.core.spans import Action, Closed, Opened
from fylgja.core.todo_import import SCOPE_PROJECT
from fylgja.store.rows import (
    ActivityRow,
    JournalThreadRow,
    ProjectRow,
    SessionRow,
    TodoRow,
)

__all__ = ["GIT_TIMEOUT", "JournalTracker", "LiveRows", "read_commits"]

log = logging.getLogger("fylgja.journal")

GIT_TIMEOUT = 5.0
"""Seconds ``git log`` may take."""


# --------------------------------------------------------------------------------------------
# The tracker (J1–J5)
# --------------------------------------------------------------------------------------------

_TURN_DONE: Final = "turn_done"
_IDLE: Final = "idle"
_BUSY: Final = frozenset({"busy", "working"})
"""States whose change to ``idle`` ends a turn of a session without hooks (J3)."""
_NOT_INPUT: Final = frozenset({"seen", "ended"})
"""States a session changes to without anything happening in the checkout (J2)."""
_ENDED: Final = "ended"
_RUN_OUTCOMES: Final = frozenset({"passed", "failed", "cancelled"})
"""The states a finished run ends in (decision 0056); ``stale`` is not one."""


class LiveRows(Protocol):
    """The live rows the tracker reads: a ``LiveView`` or a ``LiveSnapshot``."""

    @property
    def sessions(self) -> Mapping[str, SessionRow]: ...

    @property
    def activity(self) -> Mapping[str, ActivityRow]: ...

    @property
    def projects(self) -> Mapping[str, ProjectRow]: ...

    @property
    def todos(self) -> Mapping[str, TodoRow]: ...


@dataclass
class _Checkout:
    """The tracker's memo of one ``projects`` row (J1)."""

    row: ProjectRow
    branch: str | None
    """The latest ``projects.branch``; ``None`` while HEAD is detached."""
    last_branch: str | None
    """The last non-null branch seen: a different non-null branch is a switch."""
    last_commit_at: datetime | None

    @classmethod
    def of(cls, row: ProjectRow) -> _Checkout:
        return cls(row, row.branch, row.branch, row.last_commit_at)

    def thread_on(self, branch: str | None) -> ThreadKey | None:
        return thread_key(self.row, branch)

    @property
    def thread(self) -> ThreadKey | None:
        """The current thread: none for a path that is not a checkout or a detached HEAD."""
        return self.thread_on(self.branch)

    def source(self, key: ThreadKey) -> Source:
        return thread_source(key, thread_name(self.row))


@dataclass(frozen=True)
class _Session:
    """The tracker's memo of one session (J2, J3)."""

    project_path: str | None
    child: bool
    state: str | None
    source: Source
    ended: bool = False
    """Its ``session_end`` was taken (fired, dropped on a clean thread, or ignored while the
    checkout had no thread): it fires once per session key."""
    finished: frozenset[datetime | None] = frozenset()
    """The ``state_since`` of each outcome already taken: ``run_finished`` fires once per
    ``(session_key, state_since)``."""

    @classmethod
    def of(cls, row: SessionRow) -> _Session:
        return cls(
            project_path=row.project_path,
            child=row.parent_key is not None,
            state=row.state,
            source=session_source(row),
            ended=row.state == _ENDED,
            finished=frozenset({row.state_since}) if row.state in _RUN_OUTCOMES else frozenset(),
        )


_Fields = tuple[str | None, str | None, str | None, str | None, dict[str, Any] | None]
"""The activity fields whose change is input (J2)."""
_NO_FIELDS: Final[_Fields] = (None, None, None, None, None)


def _fields(row: ActivityRow) -> _Fields:
    return (row.last_prompt, row.last_reply, row.one_liner, row.open_task, row.stage)


@dataclass
class _Memo:
    checkouts: dict[str, _Checkout] = field(default_factory=dict)
    sessions: dict[str, _Session] = field(default_factory=dict)
    activity: dict[str, _Fields] = field(default_factory=dict)
    todos: dict[str, str] = field(default_factory=dict)
    """Todo id to the path it is scoped ``project`` to."""
    visits: dict[str, float] = field(default_factory=dict)
    """Seconds of focus of the visit to each checkout path (J4); at most one is open."""


class JournalTracker:
    """Dirty threads and boundaries from the live view and the focus spans (J1–J5).

    The tracker keeps its own memo of sessions, activity, checkouts, todo scopes and focus
    visits, because a :class:`~fylgja.core.live_view.LiveUpdate` carries only the new rows. Its
    thread states (:attr:`states`) are what :func:`~fylgja.core.journal.due` reads. An attempt
    moves a thread through :meth:`reserve`, :meth:`begin` (the capture) and one of
    :meth:`succeeded`, :meth:`failed` or :meth:`cancelled`.

    It does no I/O and reads only the rows it is given; the clock gives ``last_input`` and
    ``last_attempt`` (monotonic) and ``first_input`` and ``floor`` (wall time). The changes of
    one update are taken in their order.
    """

    def __init__(self, settings: JournalConfig, clock: Clock) -> None:
        self._settings = settings
        self._clock = clock
        self._states: dict[ThreadKey, ThreadState] = {}
        self._memo = _Memo()

    @property
    def states(self) -> Mapping[ThreadKey, ThreadState]:
        """Every thread the tracker has seen in this process."""
        return MappingProxyType(self._states)

    # -- seeding (J6) -------------------------------------------------------------------------

    def seed(self, view: LiveRows, threads: Iterable[JournalThreadRow] = ()) -> None:
        """Take the live view as the memo: the session states and activity, the checkouts'
        branches and last commits, and the todo scopes. Nothing becomes dirty, so a run that is
        already finished is not a transition (J6).

        ``threads`` are the stored thread rows; the bootstrap over them (J6) comes with the
        service.
        """
        memo = _Memo()
        memo.checkouts = {path: _Checkout.of(row) for path, row in view.projects.items()}
        memo.sessions = {key: _Session.of(row) for key, row in view.sessions.items()}
        memo.activity = {key: _fields(row) for key, row in view.activity.items()}
        memo.todos = {
            todo_id: row.scope_ref
            for todo_id, row in view.todos.items()
            if row.scope_kind == SCOPE_PROJECT and row.scope_ref
        }
        self._memo = memo

    # -- observing (J1–J4) --------------------------------------------------------------------

    def observe(
        self, update: LiveUpdate, view: LiveRows, *, linked: Container[str] = frozenset()
    ) -> None:
        """Take one live-view update (J2, J3). ``view`` is the live view after it; ``linked``
        holds the ids of todos linked from journal items, which are never input."""
        for change in update.changes:
            if change.collection is Collection.SESSIONS:
                self._session(change, view)
            elif change.collection is Collection.ACTIVITY:
                self._activity(change, view)
            elif change.collection is Collection.PROJECTS:
                self._project(change)
            elif change.collection is Collection.TODOS:
                self._todo(change, view, linked)

    def on_span(self, action: Action) -> None:
        """Take one focus span action (J4): visits and ``focus_left``."""
        visits = self._memo.visits
        path = action.span.project_path
        if path is None:
            return  # a window with no project neither starts nor ends a visit
        if isinstance(action, Opened):
            for other in [visited for visited in visits if visited != path]:
                seconds = visits.pop(other)
                if seconds >= self._settings.min_focus_seconds:
                    self._thread_boundary(other, "focus_left")
            visits.setdefault(path, 0.0)
        elif isinstance(action, Closed) and path in visits and action.span.ended_at is not None:
            visits[path] += (action.span.ended_at - action.span.started_at).total_seconds()

    # -- attempts (J5) ------------------------------------------------------------------------

    def reserve(self, key: ThreadKey) -> None:
        """An attempt was picked for the thread: it is not due while the attempt runs or waits
        for a slot. Nothing else changes, so input meanwhile keeps it dirty."""
        self._state(key).running = True

    def begin(self, key: ThreadKey, trigger: Trigger) -> tuple[ThreadState, Trigger]:
        """The capture of J5: the attempt holds its slot and takes its inputs now.

        Returns a snapshot of the thread's state and the trigger, raised to the highest hard
        boundary recorded meanwhile (``manual`` is kept). The state is cleared (dirty, Stop,
        pending boundaries), which acknowledges every input observed so far, and
        ``last_attempt`` is now.
        """
        state = self._state(key)
        snapshot = state.copy()
        state.dirty = False
        state.pending = {}
        state.last_attempt = self._clock.monotonic()
        return snapshot, highest_trigger(trigger, snapshot.pending)

    def succeeded(self, key: ThreadKey) -> None:
        """The attempt wrote an entry or a skip: nothing is restored."""
        state = self._state(key)
        state.running = False
        state.failed_kind = None
        state.failed_at = None
        state.input_since_failure = False

    def failed(self, key: ThreadKey, kind: str, snapshot: ThreadState) -> None:
        """The attempt failed with ``kind`` (J14): the snapshot ``begin`` returned is merged back,
        with the input observed after the capture, and the thread waits (J5 rule 3).

        ``begin`` keeps ``first_input`` and ``last_input``, so only the cleared fields return."""
        state = self._state(key)
        observed_after = state.dirty
        state.running = False
        state.dirty = snapshot.dirty or state.dirty
        state.pending = {**snapshot.pending, **state.pending}
        state.failed_kind = kind
        state.failed_at = self._clock.monotonic()
        state.input_since_failure = observed_after

    def cancelled(self, key: ThreadKey) -> None:
        """The attempt stopped before its capture: only ``running`` is cleared."""
        self._state(key).running = False

    # -- changes ------------------------------------------------------------------------------

    def _session(self, change: Change, view: LiveRows) -> None:
        memo = self._memo.sessions
        before = memo.get(change.key)
        row = change.row
        if row is None:
            if before is None:
                return
            del memo[change.key]
            if not before.child and not before.ended:
                self._boundary(before.project_path, "session_end", before.source)
            return
        row = cast(SessionRow, row)
        now = replace(
            _Session.of(row),
            ended=False if before is None else before.ended,
            finished=frozenset() if before is None else before.finished,
        )
        state = row.state
        changed = before is None or state != before.state
        path = row.project_path
        if changed and state is not None and state not in _NOT_INPUT:
            self._input(path, view)
        if not now.child and changed:
            previous = None if before is None else before.state
            if state == _TURN_DONE or (state == _IDLE and previous in _BUSY):
                self._boundary(path, "turn_done", now.source)
        if not now.child and state == _ENDED and not now.ended:
            now = replace(now, ended=True)
            self._boundary(path, "session_end", now.source)
        if changed and state in _RUN_OUTCOMES and row.state_since not in now.finished:
            now = replace(now, finished=now.finished | {row.state_since})
            self._boundary(path, "run_finished", now.source)
        memo[change.key] = now

    def _activity(self, change: Change, view: LiveRows) -> None:
        memo = self._memo.activity
        row = change.row
        if row is None:
            memo.pop(change.key, None)
            return
        fields = _fields(cast(ActivityRow, row))
        before = memo.get(change.key, _NO_FIELDS)
        memo[change.key] = fields
        if fields == before:
            return
        session = self._memo.sessions.get(change.key)
        if session is not None:
            self._input(session.project_path, view)
        elif (live := view.sessions.get(change.key)) is not None:
            self._input(live.project_path, view)

    def _project(self, change: Change) -> None:
        memo = self._memo.checkouts
        row = change.row
        if row is None:
            memo.pop(change.key, None)
            return
        row = cast(ProjectRow, row)
        checkout = memo.get(change.key)
        if checkout is None:
            memo[change.key] = _Checkout.of(row)  # a first sighting is no input
            return
        checkout.row = row
        branch = row.branch
        switched = (
            branch is not None
            and checkout.last_branch is not None
            and branch != checkout.last_branch
        )
        if switched:
            old = checkout.thread_on(checkout.last_branch)
            if old is not None:
                self._record(old, "branch_changed", checkout.source(old))
            new = checkout.thread_on(branch)
            if new is not None:
                self._state(new).floor = self._clock.now()
        checkout.branch = branch
        if branch is not None:
            checkout.last_branch = branch
        commit = row.last_commit_at
        if commit is not None and (
            checkout.last_commit_at is None or commit > checkout.last_commit_at
        ):
            checkout.last_commit_at = commit
            if not switched:  # the new thread starts clean (J3 branch_changed)
                self._dirty(checkout.thread)

    def _todo(self, change: Change, view: LiveRows, linked: Container[str]) -> None:
        memo = self._memo.todos
        row = change.row
        if row is None:
            path = memo.pop(change.key, None)
            if path is not None and change.key not in linked:
                self._input(path, view)
            return
        row = cast(TodoRow, row)
        if row.scope_kind != SCOPE_PROJECT or not row.scope_ref:
            memo.pop(change.key, None)
            return
        memo[change.key] = row.scope_ref
        if change.key not in linked:
            self._input(row.scope_ref, view)

    # -- threads ------------------------------------------------------------------------------

    def _checkout(self, path: str | None, view: LiveRows | None = None) -> _Checkout | None:
        if path is None:
            return None
        checkout = self._memo.checkouts.get(path)
        if checkout is None and view is not None:
            row = view.projects.get(path)
            if row is not None:  # its PROJECTS change comes later in the same update
                checkout = self._memo.checkouts[path] = _Checkout.of(row)
        return checkout

    def _state(self, key: ThreadKey) -> ThreadState:
        state = self._states.get(key)
        if state is None:
            state = self._states[key] = ThreadState()
        return state

    def _input(self, path: str | None, view: LiveRows) -> None:
        """A dirtying change in the checkout at ``path`` (J2); ignored without a current thread."""
        checkout = self._checkout(path, view)
        self._dirty(None if checkout is None else checkout.thread)

    def _dirty(self, key: ThreadKey | None) -> None:
        if key is None:
            return
        state = self._state(key)
        state.dirty = True
        state.last_input = self._clock.monotonic()
        if state.first_input is None:
            state.first_input = self._clock.now()
        if state.failed_kind is not None:
            state.input_since_failure = True

    def _boundary(self, path: str | None, trigger: Trigger, source: Source) -> None:
        """A boundary a session triggered in the checkout at ``path``, for its current thread."""
        checkout = self._checkout(path)
        key = None if checkout is None else checkout.thread
        if key is not None:
            self._record(key, trigger, source)

    def _thread_boundary(self, path: str, trigger: Trigger) -> None:
        """A boundary of the checkout's current thread itself (``focus_left``)."""
        checkout = self._checkout(path)
        key = None if checkout is None else checkout.thread
        if checkout is not None and key is not None:
            self._record(key, trigger, checkout.source(key))

    def _record(self, key: ThreadKey, trigger: Trigger, source: Source) -> None:
        """Record a boundary as pending, only while the thread is dirty (J3)."""
        state = self._states.get(key)
        if state is not None and state.dirty:
            state.pending[trigger] = source


# --------------------------------------------------------------------------------------------
# Commit subjects (J8 section 3, D3)
# --------------------------------------------------------------------------------------------


async def read_commits(
    runner: ProcessRunner,
    git: str,
    root: str,
    branch: str,
    since: datetime,
    as_of: datetime,
    redact: Callable[[str], str],
) -> list[CommitPart]:
    """The commits of ``branch`` in ``(since, as_of]``, never from before ``as_of`` minus
    :data:`~fylgja.core.journal.COMMIT_HORIZON` (J8 section 3, D3).

    A repo with no commits, a missing or slow ``git`` is no commits, not a failure.
    """
    since = max(since, as_of - COMMIT_HORIZON)
    try:
        result = await runner.run(log_argv(git, root, branch, since), timeout=GIT_TIMEOUT)
    except (TimeoutError, OSError) as error:  # FileNotFoundError is an OSError
        log.debug("reading commit subjects failed: %s", type(error).__name__)
        return []
    if result.returncode != 0:
        log.debug("reading commit subjects failed: exit %d", result.returncode)
        return []
    stdout = result.stdout.decode("utf-8", errors="replace")
    return in_window(parse_log(stdout, redact), since, as_of)
