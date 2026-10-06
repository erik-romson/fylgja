"""Pure logic of the presence collector (``PLAN-final-plan.md`` WP5.1, A17).

Nothing here does I/O or reads a clock. The adapter reads the signals, the collector hands each
reading and the current time to a tracker, and a tracker answers with the transitions to emit.

**Events.** Idle and lock are ``presence`` records and a call is a ``context_item``:

========== ============================ ======================= =====================
event      record                        ``entity_id``           payload
========== ============================ ======================= =====================
idle.start ``PresenceEvent``             ``idle``                ``state="idle"``
idle.end   ``PresenceEvent``             ``idle``                ``state="active"``
lock.start ``PresenceEvent``             ``lock``                ``state="locked"``
lock.end   ``PresenceEvent``             ``lock``                ``state="active"``
call.start ``ContextItemEvent``          ``call``                ``context_kind="call"``
call.end   ``ContextItemEvent``          ``call``                ``context_kind="call"``
========== ============================ ======================= =====================

A call event's ``data`` holds ``event`` (``call.start`` or ``call.end``) and the two flags
``microphone`` and ``camera``.

**Idle times.** ``idle.start`` is stamped with the moment the threshold was crossed
(``now - (idle_seconds - threshold)``), so the idle period began ``threshold`` seconds before the
event's ``observed_at``. ``idle.end`` is stamped with the moment of the first input
(``now - idle_seconds``). Both are exact to the poll's resolution and never later than ``now``.

**Nothing is guessed.** A signal that could not be read does not call its tracker, so its state
stays as it was. A call is only judged ended when every signal that feeds it was read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from fylgja.models import (
    ContextItemEvent,
    ContextItemPayload,
    PresenceEvent,
    PresencePayload,
)

__all__ = [
    "CALL",
    "IDLE",
    "LOCK",
    "SOURCE",
    "CallTracker",
    "IdleParseError",
    "IdleTracker",
    "LockTracker",
    "Transition",
    "parse_hid_idle_seconds",
    "to_record",
]

SOURCE: Final = "presence"

IDLE: Final = "idle"
LOCK: Final = "lock"
CALL: Final = "call"

NANOSECONDS: Final = 1_000_000_000
_HID_IDLE: Final = re.compile(rb'^[\s|]*"HIDIdleTime"\s*=\s*(\d{1,20})\s*$', re.MULTILINE)


class IdleParseError(ValueError):
    """``ioreg`` output without a usable ``HIDIdleTime``. ``kind`` is a short code."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def parse_hid_idle_seconds(output: bytes) -> float:
    """Seconds since the last input, from ``ioreg -r -c IOHIDSystem`` output.

    ``HIDIdleTime`` is in nanoseconds. Raises :class:`IdleParseError` when the key is absent or
    its value is not a plain number.
    """
    match = _HID_IDLE.search(output)
    if match is None:
        kind = "idle_key_missing" if b'"HIDIdleTime"' not in output else "idle_value_invalid"
        raise IdleParseError(kind)
    return int(match.group(1)) / NANOSECONDS


@dataclass(frozen=True)
class Transition:
    """One event to emit."""

    event: str
    """One of ``idle``, ``lock`` or ``call`` followed by ``.start`` or ``.end``."""
    at: datetime
    microphone: bool = False
    camera: bool = False

    @property
    def signal(self) -> str:
        return self.event.partition(".")[0]

    @property
    def starting(self) -> bool:
        return self.event.endswith(".start")


class IdleTracker:
    """Idle from ``HIDIdleTime``: idle at or above the threshold, active below it."""

    def __init__(self, threshold_seconds: float) -> None:
        if threshold_seconds <= 0:
            raise ValueError("threshold_seconds must be positive")
        self._threshold = threshold_seconds
        self._idle = False

    @property
    def idle(self) -> bool:
        return self._idle

    def update(self, idle_seconds: float, now: datetime) -> Transition | None:
        idle_seconds = max(idle_seconds, 0.0)
        if not self._idle and idle_seconds >= self._threshold:
            self._idle = True
            crossed = now - timedelta(seconds=idle_seconds - self._threshold)
            return Transition("idle.start", crossed)
        if self._idle and idle_seconds < self._threshold:
            self._idle = False
            return Transition("idle.end", now - timedelta(seconds=idle_seconds))
        return None


class LockTracker:
    """Screen lock. The first reading is a baseline: only a locked screen is an event."""

    def __init__(self) -> None:
        self._locked = False

    @property
    def locked(self) -> bool:
        return self._locked

    def update(self, locked: bool, now: datetime) -> Transition | None:
        if locked == self._locked:
            return None
        self._locked = locked
        return Transition("lock.start" if locked else "lock.end", now)


class CallTracker:
    """A call is the microphone or the camera running somewhere."""

    def __init__(self) -> None:
        self._in_call = False

    @property
    def in_call(self) -> bool:
        return self._in_call

    def update(
        self, microphone: bool | None, camera: bool | None, now: datetime
    ) -> Transition | None:
        """``None`` for a signal that could not be read."""
        readings = (microphone, camera)
        if microphone is True or camera is True:
            running = True
        elif None in readings:
            return None  # one signal is unknown and none is running: the call may go on
        else:
            running = False
        if running == self._in_call:
            return None
        self._in_call = running
        if running:
            return Transition("call.start", now, bool(microphone), bool(camera))
        return Transition("call.end", now)


def to_record(transition: Transition) -> PresenceEvent | ContextItemEvent:
    """The record for ``transition``."""
    if transition.signal == CALL:
        return ContextItemEvent(
            source=SOURCE,
            entity_id=CALL,
            observed_at=transition.at,
            payload=ContextItemPayload(
                context_kind=CALL,
                data={
                    "event": transition.event,
                    "microphone": transition.microphone,
                    "camera": transition.camera,
                },
            ),
        )
    state: Literal["active", "idle", "locked"]
    if transition.signal == LOCK:
        state = "locked" if transition.starting else "active"
    elif transition.signal == IDLE:
        state = "idle" if transition.starting else "active"
    else:
        raise ValueError(f"unknown presence event {transition.event!r}")
    return PresenceEvent(
        source=SOURCE,
        entity_id=transition.signal,
        observed_at=transition.at,
        payload=PresencePayload(state=state),
    )
