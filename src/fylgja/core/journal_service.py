"""The journal service (docs/decisions/0057). This module loads no framework.

It holds

* :class:`JournalTracker`, the in-memory state machine that turns live-view updates and focus
  span actions into dirty threads and boundaries (J1–J6);
* :func:`read_commits`, the commit subjects of a window (J8 section 3, D3);
* :class:`JournalService`, which runs the journal in the daemon (J5, J7, J13–J15, J22): it wakes
  on live-view updates, span actions, the daemon's tick and its own deadline, reserves one attempt
  per due thread, captures the thread's inputs once the attempt holds a model slot of the shared
  :class:`~fylgja.core.summaries.ModelGate`, and writes the entry, the ledger merge and the thread
  row in one writer job. Its listeners are told the thread id after every write.

"How do I do this?" (:meth:`JournalService.request_how`, :meth:`~JournalService.how`; J20) turns an
item into a checklist: one model job per item, cached by the digest of the item, its source and the
excerpt the service read from it. The excerpt is read only below the thread's resolved checkout and
never from a directory the person keeps private.

The person's actions (:meth:`JournalService.patch_item`, :meth:`~JournalService.add_item`,
:meth:`~JournalService.make_todo`, :meth:`~JournalService.refresh`; J18, J19) write through the
writer and tell the listeners, and never dirty a thread. A todo made from an item is registered as
linked before it is written, so its changes are never journal input (J2).

The tracker does no I/O and reads only the rows it is given; :func:`~fylgja.core.journal.due`
says which thread gets an attempt and when to look again. No log line carries a prompt, a reply,
a headline, an item text, a label, a name, a branch or a path (J14).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Container, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, NamedTuple, Protocol, cast

from fylgja.collectors.base import Clock
from fylgja.collectors.seams import FileSystem, PathResolver, ProcessRunner
from fylgja.config import JournalConfig
from fylgja.core import spans
from fylgja.core.journal import (
    BAD_OUTPUT,
    COMMIT_HORIZON,
    EXCERPT_MAX_BYTES,
    FILE_MAX_BYTES,
    FIRST_LOOKBACK,
    NEVER_READ,
    CommitPart,
    JournalInputs,
    MergeResult,
    OpenItem,
    Reply,
    RunSection,
    SessionPart,
    Source,
    ThreadKey,
    ThreadState,
    TodoPart,
    Trigger,
    check_reply,
    cut_bytes,
    due,
    highest_trigger,
    how_prompt,
    in_window,
    inside,
    item_seq_id,
    journal_prompt,
    log_argv,
    mark_shown,
    merge,
    order_open,
    parse_log,
    parse_steps,
    refused_name,
    section_excerpt,
    session_source,
    show_argv,
    thread_id,
    thread_key,
    thread_name,
    thread_source,
)
from fylgja.core.labels import project_name
from fylgja.core.live_view import Change, Collection, LiveUpdate, LiveView
from fylgja.core.one_liner import WAITING_STATES
from fylgja.core.span_window import SpanWindow
from fylgja.core.spans import Action, Closed, Opened
from fylgja.core.summaries import MODEL, ModelGate, input_digest, normalize
from fylgja.core.todo_import import SCOPE_PROJECT
from fylgja.core.todos import TodoNotFound, TodoService, new_todo_id
from fylgja.core.writer import Tx, Writer, WriterClosed
from fylgja.privacy import PrivacyFilter
from fylgja.store import db, queries
from fylgja.store.rows import (
    ActivityIntervalRow,
    ActivityRow,
    JournalEntryRow,
    JournalItemRow,
    JournalThreadRow,
    ProjectRow,
    RecapRow,
    SessionRow,
    TodoRow,
)

if TYPE_CHECKING:
    from fylgja.core.daemon import StateView

__all__ = [
    "GIT_TIMEOUT",
    "HowAnswer",
    "ITEM_TEXT_MAX",
    "InvalidItemText",
    "ItemNotFound",
    "ItemResult",
    "JournalError",
    "JournalService",
    "CLOSED_BY_FYLGJA",
    "JournalTracker",
    "LiveRows",
    "Resumed",
    "ThreadBrief",
    "ThreadIdentity",
    "ThreadNotFound",
    "ThreadRead",
    "item_values",
    "read_commits",
]

log = logging.getLogger("fylgja.journal")

GIT_TIMEOUT = 5.0
"""Seconds ``git log`` may take."""

ITEM_TEXT_MAX: Final = 200
"""The longest text the person may give an item (J18)."""

_COMMIT_REF: Final = re.compile(r"[0-9a-f]{7,40}")
"""A commit source's ref must look like this before it reaches ``git`` (J20)."""


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
CLOSED_BY_FYLGJA: Final = "fylgja"
"""The ``closed_by`` of a blocked item the daemon closed itself (decision 0063)."""


def _went_on(state: str | None) -> bool:
    """A session that waited for the person and is now in ``state`` went on (decision 0063)."""
    return state is not None and state not in WAITING_STATES and state != _ENDED


class Resumed(NamedTuple):
    """A session that went on after waiting for the person (decision 0063)."""

    session_key: str
    waiting_since: datetime | None
    """When it began to wait: a blocked item added before then is about something else."""


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
    since: datetime | None = None
    """The session's ``state_since``."""
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
            since=row.state_since,
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
        branches and last commits, and the todo scopes. A run that is already finished is not a
        transition (J6).

        Nothing becomes dirty, except the bootstrap of J6 over the stored ``threads``: a thread
        row that is its checkout's current thread, while a session in the checkout has an
        activity interval that opened after the row's watermark, is dirty with a seen Stop and
        its last input now, so it gets one entry ``quiet_seconds`` from now unless input keeps
        coming.
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
        for thread in threads:
            self._bootstrap(thread, view)

    def _bootstrap(self, thread: JournalThreadRow, view: LiveRows) -> None:
        key = ThreadKey(thread.repo_root, thread.branch)
        checkout = self._memo.checkouts.get(thread.repo_root)
        if checkout is None or checkout.thread != key:
            return
        newer: list[tuple[datetime, str]] = []
        for session_key, session in view.sessions.items():
            activity = view.activity.get(session_key)
            if session.project_path != thread.repo_root or activity is None:
                continue
            if thread.watermark is None or activity.valid_from > thread.watermark:
                newer.append((activity.valid_from, session_key))
        if not newer:
            return
        _, newest = max(newer)
        state = self._state(key)
        state.dirty = True
        state.pending["turn_done"] = session_source(view.sessions[newest])
        state.last_input = self._clock.monotonic()
        if state.first_input is None:
            state.first_input = self._clock.now()

    def thread_of(self, path: str) -> ThreadKey | None:
        """The current thread of the checkout at ``path`` (J1), or ``None``: no checkout there,
        a detached HEAD or no branch seen yet."""
        checkout = self._memo.checkouts.get(path)
        return None if checkout is None else checkout.thread

    # -- observing (J1–J4) --------------------------------------------------------------------

    def observe(
        self, update: LiveUpdate, view: LiveRows, *, linked: Container[str] = frozenset()
    ) -> tuple[Resumed, ...]:
        """Take one live-view update (J2, J3). ``view`` is the live view after it; ``linked``
        holds the ids of todos linked from journal items, which are never input. Returns the
        sessions that went on after waiting for the person, children too (decision 0063)."""
        resumed: list[Resumed] = []
        for change in update.changes:
            if change.collection is Collection.SESSIONS:
                self._session(change, view, resumed)
            elif change.collection is Collection.ACTIVITY:
                self._activity(change, view)
            elif change.collection is Collection.PROJECTS:
                self._project(change)
            elif change.collection is Collection.TODOS:
                self._todo(change, view, linked)
        return tuple(resumed)

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

    def nothing_new(self, key: ThreadKey) -> None:
        """The attempt found nothing new in its window (J7): what ``begin`` cleared stays cleared,
        and a failure before it still holds. Only ``running`` is cleared."""
        self._state(key).running = False

    # -- changes ------------------------------------------------------------------------------

    def _session(self, change: Change, view: LiveRows, resumed: list[Resumed]) -> None:
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
        if changed and before is not None and before.state in WAITING_STATES and _went_on(state):
            resumed.append(Resumed(change.key, before.since))
            if not now.child:
                self._boundary(path, "resumed", now.source)
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


# --------------------------------------------------------------------------------------------
# The service (J5, J7, J13–J15, J22)
# --------------------------------------------------------------------------------------------

Listener = Callable[[str], object]
"""Called with the thread id after every journal write, a failure included (J13, J22)."""

_OPEN: Final = ("open",)
_DAY: Final = timedelta(days=1)


@dataclass(frozen=True)
class ThreadBrief:
    """A thread as the service's cache holds it (kept current after every write)."""

    row: JournalThreadRow
    open_items: tuple[JournalItemRow, ...]
    """Every open item of the thread, in J16 order."""
    pending: bool
    """An attempt for the thread is reserved or running."""


@dataclass(frozen=True)
class ThreadIdentity:
    """A thread the reads can name: its key, its id and the name it goes by (J1 display)."""

    key: ThreadKey
    id: str
    name: str


@dataclass(frozen=True)
class ThreadRead:
    """What a read of one thread takes from the store (J16, J17)."""

    entries: tuple[JournalEntryRow, ...]
    """The newest entries asked for, newest first."""
    recent: tuple[JournalEntryRow, ...]
    """Every entry of the last days "Done lately" covers, newest first."""
    done: tuple[JournalItemRow, ...]
    """The items closed as done in those days."""


DONE_READ_DAYS: Final = 8
"""Days a thread read looks back for "Done lately": the seven local days of J16 and a day of margin
for the zone."""


class JournalError(Exception):
    """A person's action the journal refuses; the routes map each subclass to a status."""


class ItemNotFound(JournalError):
    """No journal item has this id."""


class ThreadNotFound(JournalError):
    """No journal thread has this id (it has no row and is not a known checkout's current one)."""


class InvalidItemText(JournalError):
    """An item text that is empty or longer than :data:`ITEM_TEXT_MAX` after cleaning."""


@dataclass(frozen=True)
class ItemResult:
    """An item after a person's action (J18)."""

    item: JournalItemRow
    shown: bool
    """Among the first three open items of its kind."""
    changed: bool
    """The action wrote something (and told the listeners)."""


@dataclass(frozen=True)
class HowAnswer:
    """The "How do I do this?" state of an item (J17, J20)."""

    item: str
    state: str
    """``fresh``, ``stale``, ``pending``, ``failed`` or ``missing``."""
    steps: tuple[str, ...] | None = None
    """The stored steps, whatever the state, when there are any."""
    generated_at: datetime | None = None
    error_kind: str | None = None
    """Set for ``failed``."""


@dataclass(frozen=True)
class _HowInput:
    """What a checklist is made from: the item's source and the excerpt read from it."""

    source: Source
    excerpt: str | None
    digest: str


@dataclass(frozen=True)
class _Acted:
    """What an action's writer job returns."""

    item: JournalItemRow
    open_items: tuple[JournalItemRow, ...]
    changed: bool


@dataclass(frozen=True)
class _Capture:
    """What one attempt took at its capture (J5): the instant, the trigger raised to the highest
    boundary recorded meanwhile, that trigger's source, the state ``begin`` cleared and the picture
    of that moment."""

    key: ThreadKey
    id: str
    as_of: datetime
    trigger: Trigger
    source: Source
    snapshot: ThreadState
    view: StateView
    row: JournalThreadRow | None
    """The thread's cached row; ``None`` before its first write."""
    start: datetime
    """The start of the window ``(start, as_of]`` (J7)."""
    focus_minutes: int


@dataclass(frozen=True)
class _Window:
    """What an attempt read from the store for its window."""

    intervals: tuple[ActivityIntervalRow, ...]
    todos: tuple[TodoRow, ...]
    open_items: tuple[JournalItemRow, ...]
    waiting_at_start: frozenset[str] = frozenset()
    """The checkout's sessions that waited for the person at the window's start."""


@dataclass(frozen=True)
class _Written:
    thread: JournalThreadRow
    open_items: tuple[JournalItemRow, ...]
    merged: MergeResult


class JournalService:
    """Runs the journal in the daemon (see the module docstring). Built only while
    ``[journal] enabled = true`` (J23). Call every method on the event loop thread.

    :meth:`observe` and :meth:`on_span` only update the tracker and :meth:`wake` the loop: the
    evaluation runs on the service's own task (:meth:`run`), never inside the writer's callback.
    """

    def __init__(
        self,
        writer: Writer,
        settings: JournalConfig,
        *,
        gate: ModelGate,
        todos: TodoService,
        clock: Clock,
        view: LiveView,
        state: Callable[[], StateView],
        span_window: SpanWindow,
        runner: ProcessRunner,
        git: str,
        privacy: PrivacyFilter,
        debounce: float,
        fs: FileSystem,
    ) -> None:
        self._writer = writer
        self._settings = settings
        self._gate = gate
        self._todos = todos
        self._clock = clock
        self._view = view
        self._state = state
        self._span_window = span_window
        self._runner = runner
        self._git = git
        self._privacy = privacy
        self._debounce = debounce
        self._fs = fs
        self.tracker = JournalTracker(settings, clock)
        """Dirty threads and boundaries (J1–J6)."""
        self._threads: dict[str, JournalThreadRow] = {}
        self._open: dict[str, tuple[JournalItemRow, ...]] = {}
        self._linked: set[str] = set()
        """The todos linked from journal items: their changes are never input (J2)."""
        self._recaps: dict[tuple[str, str | None], RecapRow] = {}
        """The newest recap per checkout and the recap's branch (docs/decisions/0064)."""
        self._attempts: dict[str, asyncio.Task[None]] = {}
        self._decisions: dict[str, asyncio.Future[str]] = {}
        """Per attempt started by a refresh: resolved once the attempt knows whether its window
        holds something new (J18)."""
        self._todo_lock = asyncio.Lock()
        self._how_lock = asyncio.Lock()
        """Serializes the decision of :meth:`request_how`, so two requests make one job."""
        self._how_jobs: dict[str, asyncio.Task[None]] = {}
        """The "How do I do this?" job of each item that has one running (J20)."""
        self._background: set[asyncio.Task[None]] = set()
        self._listeners: list[Listener] = []
        self._wake = asyncio.Event()
        self._loop: asyncio.Task[Any] | None = None
        self._closed = False
        self._logged: set[type[BaseException]] = set()

    # -- reads from the cache -----------------------------------------------------------------

    def pending(self, thread_id: str) -> bool:
        """An attempt for the thread is reserved (also while it waits for a slot) or running."""
        return thread_id in self._attempts

    def thread_for_path(self, path: str) -> ThreadKey | None:
        """The current thread of the checkout at ``path`` (J1), whether it has a row or not."""
        return self.tracker.thread_of(path)

    def brief(self, thread_id: str) -> ThreadBrief | None:
        """The thread's cached row and open items; ``None`` for a thread with no row."""
        row = self._threads.get(thread_id)
        if row is None:
            return None
        items = tuple(order_open(self._open.get(thread_id, ())))
        return ThreadBrief(row, items, self.pending(thread_id))

    def recent(self, since: datetime) -> list[ThreadBrief]:
        """The cached threads whose latest entry is at or after ``since``, newest first (J16)."""
        rows = [row for row in self._threads.values() if row.last_at and row.last_at >= since]
        rows.sort(key=lambda row: (-cast(datetime, row.last_at).timestamp(), row.thread_id))
        return [cast(ThreadBrief, self.brief(row.thread_id)) for row in rows]

    def recap(self, key: ThreadKey) -> RecapRow | None:
        """The newest recap of a session of the checkout, written on the thread's branch or on no
        known branch (docs/decisions/0064). From the cache: no I/O."""
        found = [
            row
            for row in (
                self._recaps.get((key.repo_root, key.branch)),
                self._recaps.get((key.repo_root, None)),
            )
            if row is not None
        ]
        return max(found, key=lambda row: row.at, default=None)

    def identity_of_path(self, path: str) -> ThreadIdentity | None:
        """The current thread of the checkout at ``path`` with its name, whether it has a row or
        not; ``None`` when the checkout has no thread (J1)."""
        key = self.tracker.thread_of(path)
        if key is None:
            return None
        tid = thread_id(key)
        row = self._threads.get(tid)
        if row is not None:
            return ThreadIdentity(key, tid, row.name)
        project = self._view.projects.get(path)
        name = project_name(path) if project is None else thread_name(project)
        return ThreadIdentity(key, tid, name)

    def identity(self, tid: str) -> ThreadIdentity | None:
        """The thread with this id: one with a row, or the current thread of a known checkout
        that has no row yet (J17); ``None`` for any other id."""
        row = self._threads.get(tid)
        if row is not None:
            return ThreadIdentity(ThreadKey(row.repo_root, row.branch), tid, row.name)
        for path in tuple(self._view.projects):
            found = self.identity_of_path(path)
            if found is not None and found.id == tid:
                return found
        return None

    async def read_thread(self, tid: str, entries: int) -> ThreadRead:
        """The newest ``entries`` entries of the thread, and what "Done lately" needs, from the
        store (a worker thread)."""
        now = self._clock.now()
        since = now - timedelta(days=DONE_READ_DAYS)
        return await asyncio.to_thread(self._read_thread, tid, entries, since, now + _DAY)

    def _read_thread(self, tid: str, entries: int, since: datetime, until: datetime) -> ThreadRead:
        connection = db.connect_reader(self._writer.database)
        try:
            newest = queries.list_journal_entries(connection, tid, limit=entries)
            recent = queries.entries_between(
                connection, tid, db.to_db_time(since), db.to_db_time(until)
            )
            done = [
                item
                for item in queries.list_journal_items(connection, tid, statuses=("done",))
                if item.closed_at is not None and item.closed_at >= since
            ]
            return ThreadRead(tuple(newest), tuple(recent), tuple(done))
        finally:
            connection.close()

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener`` with the thread id after every write; returns the function that
        stops it."""
        self._listeners.append(listener)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return remove

    # -- start and stop -----------------------------------------------------------------------

    async def load(self) -> None:
        """Read the thread rows, the open items and the linked todos, and seed the tracker from
        the live view with the bootstrap of J6. Run at start, after the live view is rebuilt."""
        await self._writer.execute(self._reconcile_all)  # J19 at start
        threads, open_items, linked, recaps = await asyncio.to_thread(self._read_index)
        self._threads = {row.thread_id: row for row in threads}
        self._recaps = {(path, row.branch): row for path, row in recaps}
        for session_key in self._view.recaps:
            self._note_recap(session_key)
        grouped: dict[str, list[JournalItemRow]] = {}
        for item in open_items:
            grouped.setdefault(item.thread_id, []).append(item)
        self._open = {key: tuple(items) for key, items in grouped.items()}
        self._linked = {item.todo_id for item in linked if item.todo_id is not None}
        self.tracker.seed(self._view, threads)

    def _read_index(
        self,
    ) -> tuple[
        list[JournalThreadRow],
        list[JournalItemRow],
        list[JournalItemRow],
        list[tuple[str, RecapRow]],
    ]:
        connection = db.connect_reader(self._writer.database)
        try:
            return (
                queries.load_journal_threads(connection),
                queries.load_open_journal_items(connection),
                queries.linked_journal_items(connection),
                queries.latest_recaps(connection),
            )
        finally:
            connection.close()

    async def stop(self) -> None:
        """Cancel the loop and every attempt; a running model call is killed by the runner and
        nothing of it is written."""
        self._closed = True
        current = asyncio.current_task()
        tasks = [
            task
            for task in (
                self._loop,
                *self._attempts.values(),
                *self._how_jobs.values(),
                *self._background,
            )
            if task is not None and task is not current
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._attempts.clear()
        self._how_jobs.clear()

    # -- wake-ups -----------------------------------------------------------------------------

    def wake(self) -> None:
        """Ask for an evaluation soon. Cheap and safe inside any callback."""
        self._wake.set()

    def observe(self, update: LiveUpdate) -> None:
        """Take one live-view update (the writer's callback): the tracker, a job per linked todo
        that left and per session that went on after waiting, then a wake."""
        resumed = self.tracker.observe(update, self._view, linked=self._linked)
        for change in update.changes:
            if change.collection in (Collection.RECAPS, Collection.SESSIONS) and change.row:
                self._note_recap(change.key)
        if not self._closed:
            for change in update.changes:
                if (
                    change.collection is Collection.TODOS
                    and change.row is None
                    and change.key in self._linked
                ):
                    self._spawn(self._todo_gone(change.key))
            for session in resumed:
                self._spawn(self._session_resumed(session))
        self.wake()

    def _note_recap(self, session_key: str) -> None:
        """Keep the live session's newest recap when it is the newest of its checkout and branch.
        A session placed in a checkout after its recap arrived is seen by its own change."""
        recap = self._view.recaps.get(session_key)
        session = self._view.sessions.get(session_key)
        if recap is None or session is None or session.project_path is None:
            return
        key = (session.project_path, recap.branch)
        known = self._recaps.get(key)
        if known is None or recap.at >= known.at:
            self._recaps[key] = recap

    def on_span(self, action: Action) -> None:
        """Take one focus span action (J4): the tracker only, then a wake."""
        self.tracker.on_span(action)
        self.wake()

    # -- the loop -----------------------------------------------------------------------------

    async def run(self) -> None:
        """Evaluate now, then after every wake (debounced) and at the next deadline, until
        cancelled. Each due thread is reserved and gets one attempt task."""
        self._loop = asyncio.current_task()
        deadline = self._evaluate()
        while True:
            if await self._wait(deadline) and self._debounce > 0:
                await self._clock.sleep(self._debounce)
            self._wake.clear()
            deadline = self._evaluate()

    async def _wait(self, deadline: float | None) -> bool:
        """Return on a wake (``True``) or once ``deadline`` (monotonic) has passed (``False``)."""
        if self._wake.is_set():
            return True
        if deadline is None:
            await self._wake.wait()
            return True
        delay = deadline - self._clock.monotonic()
        if delay <= 0:
            return False
        woken = asyncio.ensure_future(self._wake.wait())
        timer = asyncio.ensure_future(self._clock.sleep(delay))
        try:
            await asyncio.wait({woken, timer}, return_when=asyncio.FIRST_COMPLETED)
            return woken.done()
        finally:
            for task in (woken, timer):
                if not task.done():
                    task.cancel()
            await asyncio.gather(woken, timer, return_exceptions=True)

    def _evaluate(self) -> float | None:
        """Start an attempt for every due thread; the next deadline, or ``None`` when only a
        wake can make a thread due (or the evaluation failed: the next wake tries again)."""
        if self._closed:
            return None
        try:
            found = due(self._clock.monotonic(), self.tracker.states, self._settings)
            for thread in found.threads:
                self._start(thread.key, thread.trigger, thread.source)
        except Exception as error:
            self._failed_once("evaluating the journal", error)
            return None
        return found.next_check

    def _start(
        self,
        key: ThreadKey,
        trigger: Trigger,
        source: Source,
        decided: asyncio.Future[str] | None = None,
    ) -> None:
        """Reserve the thread (J5) and start its attempt task."""
        self.tracker.reserve(key)
        tid = thread_id(key)
        if decided is not None:
            self._decisions[tid] = decided
        self._attempts[tid] = asyncio.create_task(
            self._attempt(key, trigger, source), name="fylgja-journal-attempt"
        )

    # -- one attempt --------------------------------------------------------------------------

    async def _attempt(self, key: ThreadKey, trigger: Trigger, source: Source) -> None:
        tid = thread_id(key)
        try:
            wrote = await self._try(key, trigger, source)
        finally:
            if self._attempts.get(tid) is asyncio.current_task():
                del self._attempts[tid]
            self._decide(tid, "pending")  # an attempt that ended before it could say
        if wrote:
            self._notify(tid)
        self.wake()  # input during the attempt may make the thread due again

    async def _try(self, key: ThreadKey, trigger: Trigger, source: Source) -> bool:
        """One attempt (J5, J7, J13, J14); ``True`` when it wrote."""
        capture: _Capture | None = None
        try:
            async with self._gate.slot():
                capture = self._capture(key, trigger, source)
                inputs = await self._inputs(capture)
                if not inputs.has_news:  # J7: no call, no write, the thread stays cleared
                    self.tracker.nothing_new(key)
                    self._log(capture, "nothing_new")
                    self._decide(capture.id, "nothing_new")
                    return False
                self._decide(capture.id, "pending")
                outcome = await self._gate.run(journal_prompt(inputs))
            reply: Reply | None = None
            kind = outcome.error_kind
            rule: str | None = None
            if outcome.stdout is not None:
                checked = check_reply(outcome.stdout, inputs, self._privacy.redact)
                if isinstance(checked, Reply):
                    reply, kind = checked, None
                else:
                    rule, kind = checked, BAD_OUTPUT
            if reply is None:
                failure = kind or "runner_error"
                await self._write_failure(capture, failure)
                self.tracker.failed(key, failure, capture.snapshot)
                self._log(capture, failure)
                log.warning(
                    "journal attempt for thread %s failed: %s%s",
                    capture.id,
                    failure,
                    "" if rule is None else f" ({rule})",
                )
                return True
            merged = await self._write_reply(capture, inputs, reply)
            self.tracker.succeeded(key)
            self._log(capture, "skipped" if reply.skip else "written", merged)
            return True
        except asyncio.CancelledError:  # a daemon stop: nothing is written
            self.tracker.cancelled(key)
            raise
        except WriterClosed:
            self.tracker.cancelled(key)
            log.debug("the writer stopped before a journal attempt was stored")
            return False
        except Exception as error:
            if capture is None:
                self.tracker.cancelled(key)
            else:  # keep the captured input for a later attempt
                self.tracker.failed(key, "runner_error", capture.snapshot)
            self._failed_once("a journal attempt", error)
            return False

    def _capture(self, key: ThreadKey, trigger: Trigger, source: Source) -> _Capture:
        """J5 capture, in one synchronous step: ``as_of``, the picture of that moment and
        ``begin``, which comes last, so a failure before it leaves the thread's input as it was.
        ``begin`` keeps ``first_input`` and ``floor``, so the window can be taken before it."""
        as_of = self._clock.now()
        view = self._state()
        tid = thread_id(key)
        row = self._threads.get(tid)
        start = _window_start(row, self.tracker.states[key], as_of)
        focus_minutes = self._focus_minutes(key.repo_root, start, as_of)
        snapshot, trigger = self.tracker.begin(key, trigger)
        return _Capture(
            key=key,
            id=tid,
            as_of=as_of,
            trigger=trigger,
            source=snapshot.pending.get(trigger, source),
            snapshot=snapshot,
            view=view,
            row=row,
            start=start,
            focus_minutes=focus_minutes,
        )

    def _focus_minutes(self, root: str, start: datetime, as_of: datetime) -> int:
        """Whole minutes of focus on the checkout in the window (J8 section 5)."""
        summary = spans.summarize(self._span_window.view(as_of), start, as_of, as_of)
        seconds = next((total.seconds for total in summary.projects if total.key == root), 0.0)
        return int(seconds // 60)

    async def _inputs(self, capture: _Capture) -> JournalInputs:
        """The attempt's inputs (J8) for the window ``(start, as_of]``."""
        key, view, row = capture.key, capture.view, capture.row
        window = await asyncio.to_thread(
            self._read_window, capture.id, key.repo_root, capture.start, capture.as_of
        )
        commits = await read_commits(
            self._runner,
            self._git,
            key.repo_root,
            key.branch,
            capture.start,
            capture.as_of,
            self._privacy.redact,
        )
        project = view.live.projects.get(key.repo_root)
        return JournalInputs.create(
            thread_id=capture.id,
            name=self._name(capture),
            branch=key.branch,
            start=capture.start,
            as_of=capture.as_of,
            trigger=capture.trigger,
            trigger_source=capture.source,
            sessions=_session_parts(window.intervals, view, window.waiting_at_start),
            run_outcome=_run_outcome(capture),
            run_sections=_run_sections(capture),
            commits=commits,
            todos=_todo_parts(window.todos, capture.start, capture.as_of),
            focus_minutes=capture.focus_minutes,
            pr_number=None if project is None else project.pr_number,
            pr_state=None if project is None else project.pr_state,
            build_state=None if project is None else project.build_state,
            previous_where=None if row is None else row.where_text,
            open_items=[OpenItem.from_row(item) for item in window.open_items],
        )

    def _read_window(self, tid: str, root: str, start: datetime, as_of: datetime) -> _Window:
        connection = db.connect_reader(self._writer.database)
        try:
            since, until = db.to_db_time(start), db.to_db_time(as_of)
            return _Window(
                intervals=tuple(
                    queries.load_activity_between(
                        connection, project_path=root, since=since, until=until
                    )
                ),
                todos=tuple(
                    queries.todos_between(connection, scope_ref=root, since=since, until=until)
                ),
                open_items=tuple(queries.list_journal_items(connection, tid, statuses=_OPEN)),
                waiting_at_start=frozenset(
                    key
                    for key, state in queries.activity_states_at(
                        connection, project_path=root, at=since
                    ).items()
                    if state in WAITING_STATES
                ),
            )
        finally:
            connection.close()

    @staticmethod
    def _name(capture: _Capture) -> str:
        """The stored name, or for a new thread its checkout's project name (J1)."""
        if capture.row is not None:
            return capture.row.name
        project = capture.view.live.projects.get(capture.key.repo_root)
        return project_name(capture.key.repo_root) if project is None else thread_name(project)

    def _decide(self, tid: str, state: str) -> None:
        """Tell the refresh that started this attempt what it found (the first word counts)."""
        decided = self._decisions.pop(tid, None)
        if decided is not None and not decided.done():
            decided.set_result(state)

    # -- the person's actions (J18, J19) --------------------------------------------------------

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work, name="fylgja-journal-todo")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _result(self, acted: _Acted) -> ItemResult:
        tid = acted.item.thread_id
        if acted.changed:
            self._open[tid] = acted.open_items
            self._notify(tid)
        return ItemResult(acted.item, self.shown(acted.item), acted.changed)

    def shown(self, item: JournalItemRow) -> bool:
        """Whether ``item`` is among the first three open items of its kind (J16)."""
        if item.status != "open":
            return False
        for row, shown in mark_shown(self._open.get(item.thread_id, ())):
            if row.id == item.id:
                return shown
        return False

    def clean_text(self, text: str) -> str:
        """An item text as the person gave it: whitespace collapsed, redacted, 1–200 characters."""
        cleaned = self._privacy.redact(" ".join(text.split()))
        if not 1 <= len(cleaned) <= ITEM_TEXT_MAX:
            raise InvalidItemText
        return cleaned

    async def patch_item(
        self,
        item_id: str,
        *,
        status: str | None = None,
        pinned: bool | None = None,
        text: str | None = None,
    ) -> ItemResult:
        """Change an item's status, pin or text (J18). A change that changes nothing writes
        nothing and tells nobody. Nothing here dirties the thread (J2)."""
        cleaned = None if text is None else self.clean_text(text)
        now = self._clock.now()

        def job(tx: Tx) -> _Acted:
            connection = tx.connection
            current = queries.get_journal_item(connection, item_id)
            if current is None:
                raise ItemNotFound(item_id)
            row = current
            if status is not None and status != row.status:
                if status == "open":
                    row = replace(
                        row,
                        status="open",
                        edited=True,
                        closed_at=None,
                        closed_by=None,
                        closed_entry=None,
                    )
                else:
                    row = replace(
                        row, status=status, closed_at=now, closed_by="user", closed_entry=None
                    )
            if pinned is not None and pinned != row.pinned:
                row = replace(row, pinned=pinned)
            if cleaned is not None and cleaned != row.text:
                row = replace(row, text=cleaned, edited=True)
            return self._store_item(connection, current, row)

        return self._result(await self._writer.execute(job))

    async def add_item(self, thread: str, kind: str, text: str) -> ItemResult:
        """The person adds an open item to a thread that has a row (J18)."""
        cleaned = self.clean_text(text)
        if thread not in self._threads:
            raise ThreadNotFound(thread)
        now = self._clock.now()
        source = Source("person", thread, "you").to_json()

        def job(tx: Tx) -> _Acted:
            connection = tx.connection
            seq = max(
                (row.seq for row in queries.list_journal_items(connection, thread)), default=0
            )
            row = JournalItemRow(
                id=item_seq_id(thread, seq + 1),
                thread_id=thread,
                seq=seq + 1,
                kind=kind,
                text=cleaned,
                status="open",
                pinned=False,
                edited=True,
                source=dict(source),
                created_at=now,
                created_entry=None,
                closed_at=None,
                closed_by=None,
                closed_entry=None,
                todo_id=None,
                how_text=None,
                how_digest=None,
                how_at=None,
                how_error_kind=None,
            )
            return self._store_item(connection, None, row)

        return self._result(await self._writer.execute(job))

    @staticmethod
    def _store_item(
        connection: sqlite3.Connection, current: JournalItemRow | None, row: JournalItemRow
    ) -> _Acted:
        """Write ``row`` when it differs from ``current`` and read the thread's open items back."""
        if current is not None and row == current:
            return _Acted(current, (), False)
        stored = queries.put_journal_item(connection, item_values(row))
        open_items = queries.list_journal_items(connection, row.thread_id, statuses=_OPEN)
        return _Acted(stored, tuple(open_items), True)

    async def make_todo(self, item_id: str) -> tuple[TodoRow, bool]:
        """The Fylgja todo of an item, made now if the item has none (J19); the flag says it was
        made. The todo is scoped ``project`` to the thread's checkout."""
        async with self._todo_lock:
            item = await asyncio.to_thread(self._read_item, item_id)
            if item is None:
                raise ItemNotFound(item_id)
            if item.todo_id is not None:
                try:
                    return await self._todos.get(item.todo_id), False
                except TodoNotFound:
                    pass  # a deleted todo is unlinked when its removal is seen; make a new one
            row = self._threads.get(item.thread_id)
            if row is None:  # pragma: no cover - an item exists only with its thread row
                raise ItemNotFound(item_id)
            todo_id = new_todo_id()
            self._linked.add(todo_id)  # before the write, so its change is never input (J2)
            try:
                todo = await self._todos.add(
                    item.text, scope_kind=SCOPE_PROJECT, scope_ref=row.repo_root, todo_id=todo_id
                )
            except BaseException:
                self._linked.discard(todo_id)
                raise

            def job(tx: Tx) -> _Acted:
                current = queries.get_journal_item(tx.connection, item_id)
                if current is None:  # pragma: no cover - items are never deleted
                    raise ItemNotFound(item_id)
                return self._store_item(tx.connection, current, replace(current, todo_id=todo_id))

            self._result(await self._writer.execute(job))
            return todo, True

    def _read_item(self, item_id: str) -> JournalItemRow | None:
        connection = db.connect_reader(self._writer.database)
        try:
            return queries.get_journal_item(connection, item_id)
        finally:
            connection.close()

    async def _todo_gone(self, todo_id: str) -> None:
        """A linked todo left the live view: it is done or deleted (fact 14). The writer job reads
        the todo itself, so the two cannot be told apart wrongly (J19)."""
        try:
            changed = await self._writer.execute(lambda tx: self._reconcile(tx, todo_id))
        except WriterClosed:
            return
        except Exception as error:
            self._failed_once("reconciling a linked todo", error)
            return
        for acted in changed:
            if acted.item.todo_id is None:
                self._linked.discard(todo_id)
            self._result(acted)

    async def _session_resumed(self, resumed: Resumed) -> None:
        """A session went on after waiting for the person (decision 0063): the open blocked items
        it is the source of, added since it began to wait, are done with ``closed_by = fylgja``.
        An item the person kept or edited stays open, as in the merge (J12)."""
        now = self._clock.now()
        try:
            changed = await self._writer.execute(lambda tx: self._close_blocked(tx, resumed, now))
        except WriterClosed:
            return
        except Exception as error:
            self._failed_once("closing the blocked items of a resumed session", error)
            return
        for acted in changed:
            self._result(acted)

    def _close_blocked(self, tx: Tx, resumed: Resumed, now: datetime) -> list[_Acted]:
        connection = tx.connection
        found: list[_Acted] = []
        for item in queries.load_open_journal_items(connection):
            source = item.source
            if (
                item.kind != "blocked"
                or item.pinned
                or item.edited
                or source.get("kind") != "session"
                or source.get("ref") != resumed.session_key
                or (resumed.waiting_since is not None and item.created_at < resumed.waiting_since)
            ):
                continue
            row = replace(
                item, status="done", closed_by=CLOSED_BY_FYLGJA, closed_at=now, closed_entry=None
            )
            found.append(self._store_item(connection, item, row))
        return found

    def _reconcile_all(self, tx: Tx) -> None:
        self._reconcile(tx, None)

    def _reconcile(self, tx: Tx, only: str | None) -> list[_Acted]:
        """J19 for the items linked to ``only``, or to any todo: a done todo closes an open item
        (``closed_by = user`` at the todo's ``done_at``), a deleted one unlinks its item."""
        connection = tx.connection
        found: list[_Acted] = []
        for item in queries.linked_journal_items(connection):
            if item.todo_id is None or (only is not None and item.todo_id != only):
                continue
            todo = queries.get_todo(connection, item.todo_id)
            row = item
            if todo is None:
                row = replace(item, todo_id=None)
            elif todo.status == "done" and item.status == "open":
                row = replace(
                    item,
                    status="done",
                    closed_by="user",
                    closed_at=todo.done_at or self._clock.now(),
                    closed_entry=None,
                )
            if row != item:
                found.append(self._store_item(connection, item, row))
        return found

    async def refresh(self, tid: str) -> str:
        """Start a manual attempt for the thread, or join the one that runs (J18).

        Returns ``"pending"`` when an attempt runs (joined, or started and found something new)
        and ``"nothing_new"`` when the window holds nothing (J7). A manual attempt ignores the
        minimum interval and the failure wait. Raises :class:`ThreadNotFound` for an unknown id."""
        if tid in self._attempts:
            return "pending"
        found = self.identity(tid)
        if found is None:
            raise ThreadNotFound(tid)
        decided: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._start(found.key, "manual", thread_source(found.key, found.name), decided)
        return await decided

    # -- "How do I do this?" (J20) ------------------------------------------------------------

    async def request_how(self, item_id: str) -> HowAnswer:
        """The checklist of an item, started if need be (J20).

        ``fresh`` when the stored steps match the item as it is now (no job, no event);
        ``pending`` while a job runs for the item, joined when one already does, and otherwise
        started now. Raises :class:`ItemNotFound` for an unknown item."""
        async with self._how_lock:
            item = await asyncio.to_thread(self._read_item, item_id)
            if item is None:
                raise ItemNotFound(item_id)
            if item_id in self._how_jobs:
                return self._how_answer(item, None)
            material = await self._how_input(item)
            answer = self._how_answer(item, material.digest)
            if answer.state == "fresh":
                return answer
            task = asyncio.create_task(self._how_job(item, material), name="fylgja-journal-how")
            self._how_jobs[item_id] = task
            return self._how_answer(item, material.digest)

    async def how(self, item_id: str) -> HowAnswer:
        """The checklist state of an item. Never starts a job (J20). Raises
        :class:`ItemNotFound` for an unknown item."""
        item = await asyncio.to_thread(self._read_item, item_id)
        if item is None:
            raise ItemNotFound(item_id)
        if item_id in self._how_jobs or item.how_text is None:
            return self._how_answer(item, None)
        return self._how_answer(item, (await self._how_input(item)).digest)

    def _how_answer(self, item: JournalItemRow, digest: str | None) -> HowAnswer:
        """The state of ``item``; ``digest`` is that of its current material, when known."""
        steps = None if item.how_text is None else tuple(item.how_text.split("\n"))
        if item.id in self._how_jobs:
            state = "pending"
        elif steps is not None and digest is not None and item.how_digest == digest:
            state = "fresh"
        elif item.how_error_kind is not None:
            state = "failed"
        else:
            state = "missing" if steps is None else "stale"
        return HowAnswer(
            item=item.id,
            state=state,
            steps=steps,
            generated_at=item.how_at,
            error_kind=item.how_error_kind if state == "failed" else None,
        )

    async def _how_input(self, item: JournalItemRow) -> _HowInput:
        thread = self._threads.get(item.thread_id)
        found = item.source
        source = Source(
            kind=found.get("kind", "person"),
            ref=str(found.get("ref", "")),
            label=str(found.get("label", "")),
            path=found.get("path"),
            section=found.get("section"),
        )
        excerpt = None if thread is None else await self._excerpt(thread.repo_root, source)
        digest = input_digest(
            "journal-how", normalize({"text": item.text, "source": found, "excerpt": excerpt})
        )
        return _HowInput(source, excerpt, digest)

    async def _how_job(self, item: JournalItemRow, material: _HowInput) -> None:
        """One model call for an item, then one write and one event, a failure included."""
        try:
            async with self._gate.slot():
                outcome = await self._gate.run(
                    how_prompt(item.text, material.source, material.excerpt)
                )
            steps: list[str] | None = None
            kind = outcome.error_kind
            if outcome.stdout is not None:
                steps = parse_steps(outcome.stdout, self._privacy.redact)
                kind = BAD_OUTPUT if steps is None else None
            kind = None if steps is not None else kind or "runner_error"
            acted = await self._write_how(item.id, steps, material.digest, kind)
            self._how_jobs.pop(item.id, None)
            self._result(acted)
            if kind is None:
                log.info("journal checklist for item %s: written", item.id)
            else:
                log.warning("journal checklist for item %s failed: %s", item.id, kind)
        except asyncio.CancelledError:  # a daemon stop: nothing is written
            raise
        except WriterClosed:
            log.debug("the writer stopped before a journal checklist was stored")
        except Exception as error:
            self._failed_once("a journal checklist", error)
        finally:
            if self._how_jobs.get(item.id) is asyncio.current_task():
                del self._how_jobs[item.id]

    async def _write_how(
        self, item_id: str, steps: list[str] | None, digest: str, kind: str | None
    ) -> _Acted:
        """Steps replace the stored answer; a failure keeps it and records the kind."""
        now = self._clock.now()

        def job(tx: Tx) -> _Acted:
            connection = tx.connection
            current = queries.get_journal_item(connection, item_id)
            if current is None:  # pragma: no cover - items are never deleted
                raise ItemNotFound(item_id)
            if steps is not None:
                row = replace(
                    current,
                    how_text="\n".join(steps),
                    how_digest=digest,
                    how_at=now,
                    how_error_kind=None,
                )
            else:
                row = replace(current, how_error_kind=kind)
            stored = queries.put_journal_item(connection, item_values(row))
            open_items = queries.list_journal_items(connection, row.thread_id, statuses=_OPEN)
            return _Acted(stored, tuple(open_items), True)

        return await self._writer.execute(job)

    async def _excerpt(self, root: str, source: Source) -> str | None:
        """The text of the item's source that goes into the prompt, redacted and cut to
        :data:`~fylgja.core.journal.EXCERPT_MAX_BYTES`; ``None`` when the source gives none or
        cannot be read safely (J20)."""
        redact = self._privacy.redact
        text: str | None = None
        match source.kind:
            case "file":
                text = await asyncio.to_thread(self._file_text, root, source)
            case "session":
                text = await self._session_text(source.ref)
            case "commit":
                text = await self._commit_text(root, source.ref)
            case "todo":
                text = await asyncio.to_thread(self._todo_text, source.ref)
            case _:
                return None
        if not text:
            return None
        # Redact before the cut, so a secret the cut would split is not left half there.
        return cut_bytes(redact(text), EXCERPT_MAX_BYTES) or None

    def _file_text(self, root: str, source: Source) -> str | None:
        """The section of a file source below the thread's resolved checkout (J20)."""
        fs = self._fs
        if not isinstance(fs, PathResolver) or not source.path:
            return None
        candidate = source.path if source.path.startswith("/") else f"{root}/{source.path}"
        try:
            real = fs.realpath(candidate)
            if not inside(real, fs.realpath(root)):
                return None
            home = Path.home()
            if any(inside(real, fs.realpath(str(home / name))) for name in NEVER_READ):
                return None
            if refused_name(PurePosixPath(candidate).name) or refused_name(
                PurePosixPath(real).name
            ):
                return None
            info = fs.stat(real)
            if info.is_dir or info.size > FILE_MAX_BYTES:
                return None
            data = fs.read_bytes(real)
            if b"\x00" in data:
                return None
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        return section_excerpt(text, source.section, 1 << 30)

    async def _session_text(self, ref: str) -> str | None:
        """What a session source shows: the live session's facts and latest prompt and reply,
        otherwise its newest stored interval."""
        view = self._state()
        extras = view.sessions.get(ref)
        files, open_tasks, done_tasks = _facts(None if extras is None else extras.facts)
        live = view.live.activity.get(ref)
        found = live if live is not None else await asyncio.to_thread(self._latest_activity, ref)
        lines: list[str] = []
        if found is not None:
            for label, value in (
                ("Asked", found.last_prompt),
                ("Said", found.last_reply),
                ("Summary", found.one_liner),
            ):
                if value:
                    lines.append(f"{label}: {value}")
        if files:
            names = [PurePosixPath(name).name for name in files[:10]]
            lines.append("Files: " + ", ".join(names))
        lines += [f"Open task: {task}" for task in open_tasks[:4]]
        lines += [f"Done task: {task}" for task in done_tasks[:4]]
        return "\n".join(lines) or None

    def _latest_activity(self, ref: str) -> ActivityIntervalRow | None:
        connection = db.connect_reader(self._writer.database)
        try:
            return queries.latest_activity(connection, ref)
        finally:
            connection.close()

    async def _commit_text(self, root: str, ref: str) -> str | None:
        if not _COMMIT_REF.fullmatch(ref):
            return None
        try:
            result = await self._runner.run(show_argv(self._git, root, ref), timeout=GIT_TIMEOUT)
        except (TimeoutError, OSError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", errors="replace")

    def _todo_text(self, ref: str) -> str | None:
        connection = db.connect_reader(self._writer.database)
        try:
            todo = queries.get_todo(connection, ref)
        finally:
            connection.close()
        return None if todo is None else todo.text

    # -- the write (J13) ----------------------------------------------------------------------

    async def _write_reply(
        self, capture: _Capture, inputs: JournalInputs, reply: Reply
    ) -> MergeResult:
        """An entry or a skip: thread row, entry and merge in one writer job. The ledger is read
        again inside the job, so a change the person made during the call wins (J12)."""
        as_of = db.to_db_time(capture.as_of)
        name = self._name(capture)

        def job(tx: Tx) -> _Written:
            connection = tx.connection
            values = _thread_values(
                queries.get_journal_thread(connection, capture.id), capture, name
            )
            values.update(watermark=as_of, last_attempt_at=as_of, error_kind=None, error_at=None)
            if not reply.skip:
                values.update(last_at=as_of, where_text=reply.where, where_at=as_of)
            thread = queries.put_journal_thread(connection, values)
            merged = MergeResult((), ())
            if not reply.skip:
                entry = queries.insert_journal_entry(
                    connection,
                    {
                        "thread_id": capture.id,
                        "started_at": db.to_db_time(capture.start),
                        "ended_at": as_of,
                        "trigger": capture.trigger,
                        "headline": reply.headline,
                        "did": json.dumps(list(reply.did), ensure_ascii=False),
                        "sources": json.dumps(reply.sources_json(), ensure_ascii=False),
                        "input_digest": inputs.digest,
                        "model": MODEL,
                        "generated_at": as_of,
                    },
                )
                ledger = queries.list_journal_items(connection, capture.id)
                merged = merge(
                    ledger, reply, thread_id=capture.id, entry_id=entry, now=capture.as_of
                )
                for item in (*merged.added, *merged.closed):
                    queries.put_journal_item(connection, item_values(item))
            open_items = queries.list_journal_items(connection, capture.id, statuses=_OPEN)
            return _Written(thread, tuple(open_items), merged)

        written = await self._writer.execute(job)
        self._cache(written)
        return written.merged

    async def _write_failure(self, capture: _Capture, kind: str) -> None:
        """A failed attempt: only the thread row, with the error; no entry, no item, and the
        watermark stays (J13)."""
        as_of = db.to_db_time(capture.as_of)
        failed_at = db.to_db_time(self._clock.now())
        name = self._name(capture)

        def job(tx: Tx) -> _Written:
            connection = tx.connection
            values = _thread_values(
                queries.get_journal_thread(connection, capture.id), capture, name
            )
            values.update(last_attempt_at=as_of, error_kind=kind, error_at=failed_at)
            thread = queries.put_journal_thread(connection, values)
            open_items = queries.list_journal_items(connection, capture.id, statuses=_OPEN)
            return _Written(thread, tuple(open_items), MergeResult((), ()))

        self._cache(await self._writer.execute(job))

    def _cache(self, written: _Written) -> None:
        self._threads[written.thread.thread_id] = written.thread
        self._open[written.thread.thread_id] = written.open_items

    # -- telling ------------------------------------------------------------------------------

    def _notify(self, tid: str) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(tid)
            except Exception:
                log.exception("a journal listener failed")

    @staticmethod
    def _log(capture: _Capture, outcome: str, merged: MergeResult | None = None) -> None:
        """The one INFO line of an attempt (J14): ids, the trigger, the outcome and counts."""
        log.info(
            "journal attempt for thread %s (%s): %s, %d added, %d closed",
            capture.id,
            capture.trigger,
            outcome,
            0 if merged is None else len(merged.added),
            0 if merged is None else len(merged.closed),
        )

    def _failed_once(self, what: str, error: BaseException) -> None:
        """Log a failure once per exception type, by type only: its text could carry a path."""
        kind = type(error)
        if kind in self._logged:
            return
        self._logged.add(kind)
        log.error(
            "%s failed with %s; later failures of this type are not logged", what, kind.__name__
        )


# --------------------------------------------------------------------------------------------
# Building the inputs and the rows
# --------------------------------------------------------------------------------------------


def _window_start(row: JournalThreadRow | None, snapshot: ThreadState, as_of: datetime) -> datetime:
    """J7: the row's watermark; for a thread without one, the first input less
    :data:`~fylgja.core.journal.FIRST_LOOKBACK`, but not before the branch switch (``floor``)."""
    if row is not None and row.watermark is not None:
        return row.watermark
    start = (snapshot.first_input or as_of) - FIRST_LOOKBACK
    if snapshot.floor is not None and snapshot.floor > start:
        start = snapshot.floor
    return start


def _newest(values: Iterable[str | None]) -> str | None:
    found = [value for value in values if value]
    return found[-1] if found else None


def _stage_name(stage: Mapping[str, Any] | None) -> str | None:
    name = None if stage is None else stage.get("name")
    return name if isinstance(name, str) and name else None


def _facts(facts: Mapping[str, Any] | None) -> tuple[list[str], list[str], list[str]]:
    """``files``, open tasks and done tasks of a live session's facts block (J8 section 1)."""
    if not facts:
        return [], [], []
    files = facts.get("files")
    names = [name for name in files if isinstance(name, str)] if isinstance(files, list) else []
    open_tasks: list[str] = []
    done_tasks: list[str] = []
    tasks = facts.get("tasks")
    for task in tasks if isinstance(tasks, list) else ():
        if not isinstance(task, Mapping) or not isinstance(task.get("content"), str):
            continue
        (done_tasks if task.get("status") == "completed" else open_tasks).append(task["content"])
    return names, open_tasks, done_tasks


def _session_parts(
    intervals: Sequence[ActivityIntervalRow],
    view: StateView,
    waiting_at_start: Container[str] = frozenset(),
) -> list[SessionPart]:
    """J8 section 1, from the intervals of the window (oldest first) and the live facts. A session
    that waited at the window's start or in it, and whose newest state goes on, stopped waiting
    (decision 0063)."""
    by_session: dict[str, list[ActivityIntervalRow]] = {}
    for interval in intervals:
        by_session.setdefault(interval.session_key, []).append(interval)
    parts: list[SessionPart] = []
    for key, rows in by_session.items():
        newest = rows[-1]
        extras = view.sessions.get(key)
        files, open_tasks, done_tasks = _facts(None if extras is None else extras.facts)
        waited = key in waiting_at_start or any(row.state in WAITING_STATES for row in rows)
        parts.append(
            SessionPart(
                key=key,
                kind=newest.kind,
                last_at=newest.valid_from,
                title=newest.title,
                state=newest.state,
                child=newest.parent_key is not None,
                asked=tuple(row.last_prompt for row in rows if row.last_prompt),
                said=_newest(row.last_reply for row in rows),
                one_liner=_newest(row.one_liner for row in rows),
                stages=tuple(name for row in rows if (name := _stage_name(row.stage))),
                files=tuple(files),
                open_tasks=tuple(open_tasks),
                done_tasks=tuple(done_tasks),
                stopped_waiting=waited and _went_on(newest.state),
            )
        )
    return parts


def _run_outcome(capture: _Capture) -> str | None:
    """J8 section 2: the outcome state of the run whose finish triggered the attempt."""
    if capture.trigger != "run_finished" or capture.source.kind != "session":
        return None
    row = capture.view.live.sessions.get(capture.source.ref)
    return row.state if row is not None and row.state in _RUN_OUTCOMES else None


def _run_sections(capture: _Capture) -> list[RunSection]:
    """J8 section 2: the report sections of the finished run that triggered the attempt (J21)."""
    if capture.trigger != "run_finished" or capture.source.kind != "session":
        return []
    extras = capture.view.sessions.get(capture.source.ref)
    if extras is None or not extras.report:
        return []
    return [RunSection(path=s.path, heading=s.heading, text=s.text) for s in extras.report]


def _todo_parts(rows: Iterable[TodoRow], start: datetime, as_of: datetime) -> list[TodoPart]:
    """J8 section 4: a todo finished in the window is ``done`` at that instant, any other one
    was opened in it."""
    parts: list[TodoPart] = []
    for row in rows:
        done_at = row.done_at
        if done_at is not None and start < done_at <= as_of:
            parts.append(TodoPart(row.id, row.text, True, done_at))
        else:
            parts.append(TodoPart(row.id, row.text, False, row.created_at))
    return parts


def _time(value: datetime | None) -> str | None:
    return None if value is None else db.to_db_time(value)


def _thread_values(
    existing: JournalThreadRow | None, capture: _Capture, name: str
) -> dict[str, Any]:
    """The thread row as stored, or a new one (J13: created with ``first_at = start``)."""
    if existing is None:
        return {
            "thread_id": capture.id,
            "repo_root": capture.key.repo_root,
            "branch": capture.key.branch,
            "name": name,
            "first_at": db.to_db_time(capture.start),
            "last_at": None,
            "watermark": None,
            "where_text": None,
            "where_at": None,
            "last_attempt_at": None,
            "error_kind": None,
            "error_at": None,
        }
    return {
        "thread_id": existing.thread_id,
        "repo_root": existing.repo_root,
        "branch": existing.branch,
        "name": existing.name,
        "first_at": db.to_db_time(existing.first_at),
        "last_at": _time(existing.last_at),
        "watermark": _time(existing.watermark),
        "where_text": existing.where_text,
        "where_at": _time(existing.where_at),
        "last_attempt_at": _time(existing.last_attempt_at),
        "error_kind": existing.error_kind,
        "error_at": _time(existing.error_at),
    }


def item_values(row: JournalItemRow) -> dict[str, Any]:
    """The stored form of an item (``queries.put_journal_item``)."""
    return {
        "id": row.id,
        "thread_id": row.thread_id,
        "seq": row.seq,
        "kind": row.kind,
        "text": row.text,
        "status": row.status,
        "pinned": int(row.pinned),
        "edited": int(row.edited),
        "source": json.dumps(row.source, ensure_ascii=False),
        "created_at": db.to_db_time(row.created_at),
        "created_entry": row.created_entry,
        "closed_at": _time(row.closed_at),
        "closed_by": row.closed_by,
        "closed_entry": row.closed_entry,
        "todo_id": row.todo_id,
        "how_text": row.how_text,
        "how_digest": row.how_digest,
        "how_at": _time(row.how_at),
        "how_error_kind": row.how_error_kind,
    }
