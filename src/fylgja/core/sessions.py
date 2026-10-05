"""The live session state machine (``PLAN-final-plan.md`` WP2.3, A11, R9).

Two layers, in one module:

**The reducer** (:func:`reduce`) is pure: ``(SessionState, event) → SessionState``. Three events
exist.

* :class:`HookSignal`, one normalized hook event. The table of WP2.3:

  =========================================================  ====================
  Signal                                                     State
  =========================================================  ====================
  ``UserPromptSubmit``                                       ``working``
  ``PreToolUse``                                             ``running_tool``
  ``PermissionRequest``, ``Notification.permission_prompt``  ``waiting_permission``
  ``agent_needs_input``, ``elicitation_dialog``              ``waiting_input``
  ``Stop``                                                   ``turn_done``
  ``idle_prompt``                                            ``idle``
  ``StopFailure``                                            ``error``
  ``SessionEnd``                                             ``ended``
  =========================================================  ====================

  ``PostToolUse``, ``PostToolUseFailure`` and ``PermissionDenied`` give ``working`` again, because
  the tool ran or was refused and the agent carries on; without them a granted permission would
  stay ``waiting_permission`` until the turn ends. Every other event (``SessionStart``, subagent and
  task events, compaction, ``StatusLine``) names no state and changes nothing. An ``idle_prompt``
  never overwrites a ``turn_done`` that nobody has looked at yet: the person still has not seen the
  result, and ``seen`` or a newer event is what ends it.
* :class:`RegistrySignal`, the registry's own ``waiting``, ``working`` or ``idle``. It is the
  fallback and applies **only while no hook has arrived** for the session.
* :class:`FocusSignal`, the session's window gaining focus. It turns ``turn_done`` into ``seen``
  when the focus came *after* the Stop event.

Hook events are ordered by ``received_at_ns``, then ``observed_at``. A hook event older than the
newest one already applied changes nothing, so a late event never moves the state backwards.

**The engine** (:class:`SessionStateEngine`) puts the reducer in the daemon. It sits between the
ingest gate and the writer (``Daemon`` wires it into the tap):

* a ``hook_event`` record is reduced, and a change of state is written as a derived
  ``session_activity`` event, merged over the last activity the registry reported (the title, task,
  facts and so on survive). The writer does the rest: ``state_since``, ``waiting_reason``, the
  live view and the one-liner (rule 1 of §4.6 fires for ``waiting_permission`` and
  ``waiting_input``);
* a ``session_activity`` record of any source passes through the engine. While the session has hook
  state, the engine overlays that state on it, so the registry's next poll cannot undo a hook state.
  Before the first hook the registry's state stands;
* a ``focus`` event looks up the window of every ``turn_done`` session (``window_of``, the
  resolver's join) and marks the session ``seen`` when the window is the one that gained focus;
* a session that hooks know and the registry does not still exists: the engine announces it with a
  ``session`` event (pid, cwd and the predecessor link). The registry stays the authority for
  liveness, so its next complete snapshot ends a session it does not list;
* the **A11 link**: a session id that first appears under the same ``CLAUDE_PID`` as a session that
  just ended with reason ``clear`` names that session as its predecessor. A registry row carries the
  same link (``SessionPayload.predecessor_key``, the step 11 continuity record) and wins.

Derived records never go back through the engine, and their ``observed_at`` is never older than the
last activity the engine has passed on for that session: the store ignores an observation older than
its open interval, and a hook event is stamped when the hook started, which can be earlier than the
registry poll that landed in between.

Only state values of ``fylgja.now/1`` are used. Nothing here logs content.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Final

from fylgja.core.writer import session_key, split_session_key
from fylgja.models import (
    FocusEvent,
    HookEvent,
    Record,
    SessionActivity,
    SessionActivityEvent,
    SessionActivitySnapshot,
    SessionEvent,
    SessionPayload,
    SessionSnapshot,
    compute_dedup_key,
)

__all__ = [
    "ENDED",
    "ERROR",
    "HOOK_ONLY_STATES",
    "IDLE",
    "RUNNING_TOOL",
    "SEEN",
    "TURN_DONE",
    "WAITING_INPUT",
    "WAITING_PERMISSION",
    "WORKING",
    "FocusSignal",
    "HookSignal",
    "RegistrySignal",
    "SessionState",
    "SessionStateEngine",
    "activity_of",
    "ns_of",
    "reduce",
    "signal_of",
]

WORKING: Final = "working"
RUNNING_TOOL: Final = "running_tool"
WAITING_PERMISSION: Final = "waiting_permission"
WAITING_INPUT: Final = "waiting_input"
TURN_DONE: Final = "turn_done"
SEEN: Final = "seen"
IDLE: Final = "idle"
ERROR: Final = "error"
ENDED: Final = "ended"

HOOK_ONLY_STATES: Final = frozenset(
    {RUNNING_TOOL, WAITING_PERMISSION, WAITING_INPUT, TURN_DONE, SEEN, ERROR, ENDED}
)
"""States only hooks (or the focus rule) produce. The registry never reports them, so a stored one
proves that hook state existed (the engine seeds itself from them after a restart)."""

_EVENT_STATES: Final[Mapping[str, str]] = {
    "UserPromptSubmit": WORKING,
    "PreToolUse": RUNNING_TOOL,
    "PostToolUse": WORKING,
    "PostToolUseFailure": WORKING,
    "PermissionRequest": WAITING_PERMISSION,
    "PermissionDenied": WORKING,
    "Stop": TURN_DONE,
    "StopFailure": ERROR,
    "SessionEnd": ENDED,
}

_NOTIFICATION_STATES: Final[Mapping[str, str]] = {
    "permission_prompt": WAITING_PERMISSION,
    "agent_needs_input": WAITING_INPUT,
    "elicitation_dialog": WAITING_INPUT,
    "idle_prompt": IDLE,
}

_INPUT_REASONS: Final[Mapping[str, str]] = {
    "agent_needs_input": "agent needs input",
    "elicitation_dialog": "question dialog",
}

_TOOL_STATES: Final = frozenset({RUNNING_TOOL, WAITING_PERMISSION})
"""States in which ``current_tool`` means something; a permission prompt keeps the tool of the call
it interrupts."""

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

MAX_SESSIONS: Final = 4096
"""How many sessions the engine remembers at most; the oldest is forgotten first."""
MAX_PIDS: Final = 256
"""How many ``/clear`` rotations in flight are remembered."""

Order = tuple[int, datetime]


# --------------------------------------------------------------------------------------------
# Events and state
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookSignal:
    """One normalized hook event, as the reducer needs it."""

    event: str
    received_at_ns: int
    observed_at: datetime
    tool_name: str | None = None
    notification_type: str | None = None
    target: str | None = None
    """What the tool touched, when the hook says so (the first file an editing tool wrote)."""

    @property
    def order(self) -> Order:
        """The sort key of hook events: ``received_at_ns``, then ``observed_at``."""
        return (self.received_at_ns, self.observed_at)


@dataclass(frozen=True, slots=True)
class RegistrySignal:
    """What the registry reports about a session (the fallback)."""

    state: str | None
    at: datetime
    waiting_reason: str | None = None
    current_tool: str | None = None
    current_target: str | None = None


@dataclass(frozen=True, slots=True)
class FocusSignal:
    """The session's window gained focus at ``at``."""

    at: datetime


Event = HookSignal | RegistrySignal | FocusSignal


@dataclass(frozen=True, slots=True)
class SessionState:
    """The state of one session. ``since`` is when ``state`` began."""

    state: str | None = None
    since: datetime | None = None
    waiting_reason: str | None = None
    current_tool: str | None = None
    current_target: str | None = None
    hooked: bool = False
    """A hook event has set the state; the registry fallback no longer applies."""
    order: Order | None = None
    """The sort key of the newest hook event applied."""


# --------------------------------------------------------------------------------------------
# The reducer
# --------------------------------------------------------------------------------------------


def _state_of(event: HookSignal) -> str | None:
    if event.event == "Notification":
        return _NOTIFICATION_STATES.get(event.notification_type or "")
    return _EVENT_STATES.get(event.event)


def _waiting_reason(state: str, event: HookSignal, tool: str | None) -> str | None:
    if state == WAITING_PERMISSION:
        return f"approve {tool}" if tool else "approve"
    if state == WAITING_INPUT:
        return _INPUT_REASONS.get(event.notification_type or "", "input needed")
    return None


def _on_hook(current: SessionState, event: HookSignal) -> SessionState:
    state = _state_of(event)
    if state is None:
        return current
    if current.order is not None and event.order < current.order:
        return current  # late: a newer event has been applied
    if state == IDLE and current.state == TURN_DONE:
        # Nobody has looked at the finished turn yet; idleness is already implied by turn_done.
        return replace(current, hooked=True, order=event.order)
    carried = current.state in _TOOL_STATES
    tool: str | None = None
    target: str | None = None
    if state in _TOOL_STATES:
        tool = event.tool_name or (current.current_tool if carried else None)
        same = tool is not None and tool == current.current_tool and carried
        target = event.target or (current.current_target if same else None)
    return SessionState(
        state=state,
        since=event.observed_at
        if state != current.state or current.since is None
        else current.since,
        waiting_reason=_waiting_reason(state, event, tool),
        current_tool=tool,
        current_target=target,
        hooked=True,
        order=event.order,
    )


def _on_registry(current: SessionState, event: RegistrySignal) -> SessionState:
    if current.hooked or event.state is None:
        return current
    changed = event.state != current.state or current.since is None
    return replace(
        current,
        state=event.state,
        since=event.at if changed else current.since,
        waiting_reason=event.waiting_reason,
        current_tool=event.current_tool,
        current_target=event.current_target,
    )


def _on_focus(current: SessionState, event: FocusSignal) -> SessionState:
    if current.state != TURN_DONE or current.since is None or event.at <= current.since:
        return current
    return replace(current, state=SEEN, since=event.at, waiting_reason=None)


def reduce(state: SessionState | None, event: Event) -> SessionState:
    """The state after ``event``. ``None`` is a session nothing is known about.

    An event that changes nothing returns ``state`` itself (the same object).
    """
    current = SessionState() if state is None else state
    if isinstance(event, HookSignal):
        return _on_hook(current, event)
    if isinstance(event, RegistrySignal):
        return _on_registry(current, event)
    return _on_focus(current, event)


def ns_of(moment: datetime) -> int:
    """Nanoseconds since the epoch of ``moment`` (integer arithmetic, microsecond resolution)."""
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def signal_of(record: HookEvent) -> HookSignal:
    """The reducer's view of a normalized hook event record."""
    payload = record.payload
    target = None
    if payload.edited_paths:
        target = PurePosixPath(payload.edited_paths[0]).name or None
    return HookSignal(
        event=payload.event,
        received_at_ns=ns_of(record.observed_at),
        observed_at=record.observed_at,
        tool_name=payload.tool_name,
        notification_type=payload.notification_type,
        target=target,
    )


def activity_of(state: SessionState, base: SessionActivity | None = None) -> SessionActivity:
    """``base`` with the state fields of ``state`` laid over it.

    A target the base knows (the registry's pending tool call carries the command) is kept while
    the tool is the same, because a hook says less about what a tool is doing.
    """
    activity = SessionActivity() if base is None else base
    tool = state.current_tool
    target = state.current_target
    if target is None and tool is not None and activity.current_tool == tool:
        target = activity.current_target
    return activity.model_copy(
        update={
            "state": state.state,
            "waiting_reason": state.waiting_reason,
            "current_tool": tool,
            "current_target": target,
        }
    )


# --------------------------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------------------------

Emit = Callable[[Record], None]
WindowOf = Callable[[str], str | None]


class SessionStateEngine:
    """Runs the reducer for every session the daemon sees. See the module docstring.

    ``emit`` takes the derived records (the daemon passes the writer's ``submit``) and ``window_of``
    returns the resolver's window id of a session key. :meth:`apply` is the ingest tap's filter: it
    returns the record to store, which is the same record unless a hook state is overlaid on it.
    Call it on one thread (the event loop).
    """

    def __init__(
        self, emit: Emit, window_of: WindowOf, *, max_sessions: int = MAX_SESSIONS
    ) -> None:
        self._emit = emit
        self._window_of = window_of
        self._max = max_sessions
        self._states: dict[str, SessionState] = {}
        self._base: dict[str, SessionActivity] = {}
        self._sources: dict[str, str] = {}
        self._floor: dict[str, datetime] = {}
        self._predecessor: dict[str, str] = {}
        self._cleared: OrderedDict[int, str] = OrderedDict()

    # -- reading ----------------------------------------------------------------------------

    def state_of(self, key: str) -> SessionState | None:
        return self._states.get(key)

    def predecessor(self, key: str) -> str | None:
        """The session this one replaced in the same process (A11), when known."""
        return self._predecessor.get(key)

    def keys(self) -> set[str]:
        return set(self._states)

    # -- lifecycle --------------------------------------------------------------------------

    def seed(
        self, rows: Iterable[tuple[str, str, str | None, datetime | None, str | None]]
    ) -> None:
        """Resume after a restart from stored sessions ``(key, source, state, since, reason)``.

        Only the hook-only states are taken (:data:`HOOK_ONLY_STATES`): the registry would report
        the others again by itself.
        """
        for key, source, state, since, reason in rows:
            if state in HOOK_ONLY_STATES and key not in self._states:
                self._states[key] = SessionState(
                    state=state, since=since, waiting_reason=reason, hooked=True
                )
                self._sources[key] = source
                if since is not None:
                    self._floor[key] = since

    def forget(self, key: str) -> None:
        """Drop a session that left the live view."""
        for table in (self._states, self._base, self._sources, self._floor, self._predecessor):
            table.pop(key, None)

    def _remember(self, key: str, state: SessionState, source: str) -> None:
        self._states[key] = state
        self._sources[key] = source
        while len(self._states) > self._max:
            self.forget(next(iter(self._states)))

    # -- the tap ----------------------------------------------------------------------------

    def apply(self, record: Record) -> Record:
        """Observe ``record`` and return what is to be stored in its place."""
        if isinstance(record, HookEvent):
            self._hook(record)
        elif isinstance(record, FocusEvent):
            self._focus(record)
        elif isinstance(record, SessionSnapshot):
            for item in record.items:
                self._predecessor_of(item.payload)
        elif isinstance(record, SessionEvent):
            self._predecessor_of(record.payload)
        elif isinstance(record, SessionActivitySnapshot):
            return self._activity_snapshot(record)
        elif isinstance(record, SessionActivityEvent):
            return self._activity_event(record)
        return record

    # -- hook events ------------------------------------------------------------------------

    def _hook(self, record: HookEvent) -> None:
        payload = record.payload
        if payload.event == "StatusLine":
            return  # a statusline reading is not a hook event
        key = _key_of(record.entity_id, record.source)
        signal = signal_of(record)
        previous = self._states.get(key)
        if previous is None:
            self._announce(key, record)
        if payload.event == "SessionEnd" and payload.reason == "clear" and payload.pid is not None:
            self._cleared[payload.pid] = key
            self._cleared.move_to_end(payload.pid)
            while len(self._cleared) > MAX_PIDS:
                self._cleared.popitem(last=False)
        current = reduce(previous, signal)
        self._remember(key, current, record.source)
        if current.state is not None and _visible(current) != _visible(previous):
            self._write(key, current, signal.observed_at)

    def _announce(self, key: str, record: HookEvent) -> None:
        """First sight of a session: make sure it exists, with what the hook tells about it."""
        payload = record.payload
        if payload.pid is not None:
            replaced = self._cleared.get(payload.pid)
            if replaced is not None and replaced != key:
                self._predecessor.setdefault(key, replaced)
                del self._cleared[payload.pid]
        kind, native_id = split_session_key(key, default_kind=record.source)
        at = max(record.observed_at, self._floor.get(key, record.observed_at))
        self._floor[key] = at
        self._emit(
            SessionEvent(
                source=record.source,
                entity_id=key,
                observed_at=at,
                payload=SessionPayload(
                    kind=kind,
                    native_id=native_id,
                    pid=payload.pid,
                    cwd=payload.cwd,
                    predecessor_key=self._predecessor.get(key),
                ),
            )
        )

    def _predecessor_of(self, payload: SessionPayload) -> None:
        if payload.predecessor_key is not None:
            self._predecessor[session_key(payload.kind, payload.native_id)] = (
                payload.predecessor_key
            )

    def _write(self, key: str, state: SessionState, at: datetime) -> None:
        """Emit the activity of ``key`` with ``state`` laid over the last one the registry gave."""
        at = max(at, self._floor.get(key, at))
        self._floor[key] = at
        self._emit(
            SessionActivityEvent(
                source=self._sources[key],
                entity_id=key,
                observed_at=at,
                payload=activity_of(state, self._base.get(key)),
            )
        )

    # -- focus ------------------------------------------------------------------------------

    def _focus(self, record: FocusEvent) -> None:
        window = record.payload.window_id
        if window is None:
            return
        for key in sorted(self._states):
            current = self._states[key]
            if current.state != TURN_DONE or self._window_of(key) != window:
                continue
            seen = reduce(current, FocusSignal(record.observed_at))
            if seen is not current:
                self._states[key] = seen
                self._write(key, seen, record.observed_at)

    # -- activity of other sources ----------------------------------------------------------

    def _observe_activity(
        self, key: str, source: str, activity: SessionActivity, at: datetime
    ) -> SessionActivity:
        self._base[key] = activity
        self._floor[key] = max(at, self._floor.get(key, at))
        signal = RegistrySignal(
            state=activity.state,
            at=at,
            waiting_reason=activity.waiting_reason,
            current_tool=activity.current_tool,
            current_target=activity.current_target,
        )
        current = reduce(self._states.get(key), signal)
        self._remember(key, current, source)
        return activity_of(current, activity) if current.hooked else activity

    def _activity_snapshot(self, record: SessionActivitySnapshot) -> Record:
        items = []
        changed = False
        for item in record.items:
            key = _key_of(item.entity_id, record.source)
            merged = self._observe_activity(key, record.source, item.payload, record.observed_at)
            if merged != item.payload:
                item = item.model_copy(update={"payload": merged})
                changed = True
            items.append(item)
        return record.model_copy(update={"items": tuple(items)}) if changed else record

    def _activity_event(self, record: SessionActivityEvent) -> Record:
        key = _key_of(record.entity_id, record.source)
        merged = self._observe_activity(key, record.source, record.payload, record.observed_at)
        if merged == record.payload:
            return record
        return record.model_copy(
            update={
                "payload": merged,
                "dedup_key": compute_dedup_key(
                    record.source,
                    record.entity_kind,
                    record.entity_id,
                    record.observed_at,
                    merged.model_dump(mode="json"),
                ),
            }
        )


def _visible(state: SessionState | None) -> tuple[str | None, str | None, str | None, str | None]:
    """The fields of a state that reach the activity record."""
    if state is None:
        return (None, None, None, None)
    return (state.state, state.waiting_reason, state.current_tool, state.current_target)


def _key_of(entity_id: str, source: str) -> str:
    return session_key(*split_session_key(entity_id, default_kind=source))
