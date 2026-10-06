"""The presence collector (``PLAN-final-plan.md`` WP5.1, A17; ``PLAN.md`` §4.2 and §4.7).

Two loops run under one task group:

* the **signal loop** reads the screen lock, the microphone and the camera every
  ``poll_interval_seconds`` (5 s, never more than 10 s);
* the **idle loop** reads ``HIDIdleTime`` every ``idle_poll_interval_seconds`` (15 s), **only while
  a focus span is open**, that is, while the live view has a focused window. With no focused window
  it reads nothing and starts no process.

Each reading goes to a tracker (:mod:`~fylgja.collectors.presence.parse`) and a transition becomes
one record. A signal that fails is logged once, named in :meth:`PresenceCollector.doctor`, and
leaves its tracker alone: no event is guessed. The other signals keep reporting, and the failed one
is tried again on its next poll.

Nothing is logged except signal names, short failure codes and event names.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field

from fylgja.collectors.base import CollectorContext
from fylgja.collectors.presence import parse
from fylgja.collectors.presence.adapter import SIGNALS, PresenceSignals, SignalError
from fylgja.models import HealthReport, HealthState

__all__ = ["PresenceCollector", "PresenceSettings"]


class PresenceSettings(BaseModel):
    """``[collectors.presence]``."""

    model_config = ConfigDict(extra="forbid")

    idle_threshold_seconds: float = Field(default=300.0, gt=0)
    idle_poll_interval_seconds: float = Field(default=15.0, gt=0)
    poll_interval_seconds: float = Field(default=5.0, ge=5.0, le=10.0)
    """Lock, microphone and camera."""
    database: str | None = None
    """The store the focus gate reads (read-only); default the daemon's own."""


class PresenceCollector:
    def __init__(
        self,
        settings: PresenceSettings,
        context: CollectorContext,
        *,
        signals: PresenceSignals,
    ) -> None:
        self._settings = settings
        self._context = context
        self._signals = signals
        self._idle = parse.IdleTracker(settings.idle_threshold_seconds)
        self._lock = parse.LockTracker()
        self._call = parse.CallTracker()
        self._failures: dict[str, str] = {}
        """Signal name to the code of its last failure; cleared by the next good reading."""
        self._loop_failures: dict[str, str] = {}
        """Loop name to the type of the unexpected error that stopped its last poll."""
        self._polled = False
        self.idle_polls = 0
        """Idle readings attempted (a poll skipped for lack of a focused window is not one)."""
        self.signal_polls = 0
        self.events = 0
        """Events that reached the sink."""

    # -- failures ---------------------------------------------------------------------------

    def _failed(self, error: SignalError) -> None:
        if self._failures.get(error.signal) != error.kind:
            self._context.logger.warning("presence signal %s failed: %s", error.signal, error.kind)
        self._failures[error.signal] = error.kind

    def _recovered(self, signal: str) -> None:
        if self._failures.pop(signal, None) is not None:
            self._context.logger.info("presence signal %s recovered", signal)

    def _emit(self, transition: parse.Transition | None) -> None:
        if transition is None:
            return
        self._context.sink.emit(parse.to_record(transition))
        self.events += 1
        self._context.logger.debug("presence %s", transition.event)

    # -- one poll each ----------------------------------------------------------------------

    async def poll_idle(self) -> bool:
        """Read the idle time if a focus span is open. Returns whether it was read."""
        try:
            focused = self._signals.focus_open()
        except SignalError as error:
            self._failed(error)
            return False
        self._recovered("focus")
        if not focused:
            return False
        self.idle_polls += 1
        try:
            seconds = await self._signals.idle_seconds()
        except SignalError as error:
            self._failed(error)
            return False
        self._recovered("idle")
        self._emit(self._idle.update(seconds, self._context.clock.now()))
        return True

    def poll_signals(self) -> None:
        """Read the screen lock, the microphone and the camera once."""
        self.signal_polls += 1
        now = self._context.clock.now()
        try:
            locked = self._signals.screen_locked()
        except SignalError as error:
            self._failed(error)
        else:
            self._recovered("lock")
            self._emit(self._lock.update(locked, now))

        microphone: bool | None = None
        camera: bool | None = None
        try:
            microphone = self._signals.microphone_in_use()
        except SignalError as error:
            self._failed(error)
        else:
            self._recovered("microphone")
        try:
            camera = self._signals.camera_in_use()
        except SignalError as error:
            self._failed(error)
        else:
            self._recovered("camera")
        self._emit(self._call.update(microphone, camera, now))
        self._polled = True

    # -- loops ------------------------------------------------------------------------------

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._idle_loop(), name="presence:idle")
            await self._signal_loop()

    async def _signal_loop(self) -> None:
        while True:
            try:
                self.poll_signals()
            except Exception as exc:
                self._unexpected("signals", exc)
            else:
                self._loop_failures.pop("signals", None)
            await self._context.clock.sleep(self._settings.poll_interval_seconds)

    async def _idle_loop(self) -> None:
        while True:
            try:
                await self.poll_idle()
            except Exception as exc:
                self._unexpected("idle", exc)
            else:
                self._loop_failures.pop("idle", None)
            await self._context.clock.sleep(self._settings.idle_poll_interval_seconds)

    def _unexpected(self, loop: str, exc: Exception) -> None:
        kind = type(exc).__name__
        if self._loop_failures.get(loop) != kind:
            self._context.logger.error("presence %s poll failed: %s", loop, kind)
        self._loop_failures[loop] = kind

    # -- health -----------------------------------------------------------------------------

    async def doctor(self) -> HealthReport:
        """Name the failing signals. No side effects."""
        now = self._context.clock.now()
        if self._failures:
            names = [name for name in SIGNALS if name in self._failures]
            detail = "; ".join(f"{name}: {self._failures[name]}" for name in names)
            return HealthReport(
                state=HealthState.DEGRADED,
                detail=f"failing signals: {detail}",
                error_kind=f"{names[0]}_{self._failures[names[0]]}",
                checked_at=now,
            )
        if self._loop_failures:
            loops = ", ".join(
                f"{loop} ({kind})" for loop, kind in sorted(self._loop_failures.items())
            )
            return HealthReport(
                state=HealthState.DEGRADED,
                detail=f"a poll failed: {loops}",
                error_kind="poll_failed",
                checked_at=now,
            )
        detail = (
            "lock, microphone and camera read; idle read while a window is focused"
            if self._polled
            else "not polled yet"
        )
        return HealthReport(state=HealthState.OK, detail=detail, checked_at=now)
