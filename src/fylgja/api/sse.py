"""``GET /events``: the change stream (WP2.5; ``PLAN-final-plan.md`` §4.2, §3.1).

A Server-Sent Events stream, so the navigator page, ``fylgja story`` and Lidskjalv update without
polling. It is a read endpoint: it sits behind the A6 Host guard like every other route and needs
no authentication.

* ``now.changed`` (``{"version": n}``) after every effective change of the live view, with the live
  view's version counter as the SSE ``id``. Clients fetch ``/now`` to see what changed.
* ``session.state`` (``{"session_key", "state", "version"}``) when a live session's state changed
  or the session ended (``state`` is ``null``), and ``focus.changed`` (``{"window_id",
  "workspace", "version"}``). Both come before the ``now.changed`` of the same update.
* ``summary.ready`` and later events are published by their owners through
  :meth:`EventHub.publish`.
* A ``: hb`` comment every :data:`HEARTBEAT_SECONDS`, timed by the daemon's injected clock.
* At most :data:`MAX_CLIENTS` clients; the next one gets ``503``.

Payloads carry ids, states and versions, never prompt text or titles. The hub listens to the live
view only while a client is connected. A client's slot, queue and tasks are released when its
response ends, however it ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from fylgja.api.deps import daemon_of
from fylgja.collectors.base import Clock
from fylgja.core.live_view import Collection, LiveUpdate, LiveView

__all__ = [
    "HEARTBEAT_SECONDS",
    "MAX_CLIENTS",
    "EventHub",
    "Subscription",
    "format_event",
    "router",
]

logger = logging.getLogger("fylgja.api.sse")

HEARTBEAT_SECONDS: Final = 15.0
MAX_CLIENTS: Final = 16
QUEUE_SIZE: Final = 256
"""Frames a slow client may fall behind before its oldest ones are dropped. Every event is a hint
to fetch ``/now``, so a dropped one costs nothing a later one does not repair."""

HEARTBEAT_FRAME: Final = b": hb\n\n"
_CONNECTED_FRAME: Final = b": connected\n\n"
_HEADERS: Final = {"cache-control": "no-store", "x-accel-buffering": "no"}

router = APIRouter()


def format_event(event: str, data: Mapping[str, Any], event_id: int | None = None) -> bytes:
    """One SSE frame."""
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append("data: " + json.dumps(data, separators=(",", ":"), sort_keys=True))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


class Subscription:
    """One client's queue and slot. :meth:`close` is idempotent."""

    def __init__(self, hub: EventHub) -> None:
        self._hub = hub
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(QUEUE_SIZE)
        self.closed = False

    def push(self, frame: bytes | None) -> None:
        while True:
            try:
                self.queue.put_nowait(frame)
                return
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self.queue.get_nowait()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._hub.release(self)


class EventHub:
    """Fans published events out to the connected clients and turns live view updates into them."""

    def __init__(self, view: LiveView, *, max_clients: int = MAX_CLIENTS) -> None:
        self._view = view
        self._max = max_clients
        self._subscriptions: list[Subscription] = []
        self._unsubscribe: Callable[[], None] | None = None
        self._states: dict[str, str | None] = {}

    @property
    def clients(self) -> int:
        return len(self._subscriptions)

    @property
    def attached(self) -> bool:
        """The hub is listening to the live view (it is while a client is connected)."""
        return self._unsubscribe is not None

    def subscribe(self) -> Subscription | None:
        """A new client's subscription, or ``None`` when :data:`MAX_CLIENTS` are connected."""
        if len(self._subscriptions) >= self._max:
            return None
        subscription = Subscription(self)
        self._subscriptions.append(subscription)
        if self._unsubscribe is None:
            self._states = {key: row.state for key, row in self._view.sessions.items()}
            self._unsubscribe = self._view.subscribe(self._on_update)
        return subscription

    def release(self, subscription: Subscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)
        if not self._subscriptions and self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
            self._states = {}

    def publish(self, event: str, data: Mapping[str, Any], *, event_id: int | None = None) -> None:
        """Send ``event`` to every connected client (``summary.ready`` and the like)."""
        frame = format_event(event, data, event_id)
        for subscription in tuple(self._subscriptions):
            subscription.push(frame)

    def close(self) -> None:
        """End every stream (the app is shutting down)."""
        for subscription in tuple(self._subscriptions):
            subscription.push(None)

    def _on_update(self, update: LiveUpdate) -> None:
        version = update.version
        for change in update.changes:
            if change.collection is Collection.SESSIONS:
                if change.row is None:
                    if change.key not in self._states:
                        continue
                    del self._states[change.key]
                    state = None
                else:
                    state = getattr(change.row, "state", None)
                    if change.key in self._states and self._states[change.key] == state:
                        continue
                    self._states[change.key] = state
                self.publish(
                    "session.state",
                    {"session_key": change.key, "state": state, "version": version},
                )
            elif change.collection is Collection.FOCUS:
                row = change.row
                self.publish(
                    "focus.changed",
                    {
                        "window_id": getattr(row, "window_id", None),
                        "workspace": getattr(row, "workspace", None),
                        "version": version,
                    },
                )
        self.publish("now.changed", {"version": version}, event_id=version)


class _EventStream(StreamingResponse):
    """A streaming response that gives its subscription back however the response ends, also
    when the client leaves before the first frame."""

    def __init__(self, subscription: Subscription, clock: Clock) -> None:
        super().__init__(
            _frames(subscription, clock), media_type="text/event-stream", headers=_HEADERS
        )
        self._subscription = subscription

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._subscription.close()


async def _frames(subscription: Subscription, clock: Clock) -> AsyncIterator[bytes]:
    getter: asyncio.Task[bytes | None] = asyncio.create_task(subscription.queue.get())
    ticker: asyncio.Task[None] = asyncio.create_task(clock.sleep(HEARTBEAT_SECONDS))
    try:
        yield _CONNECTED_FRAME
        while True:
            await asyncio.wait({getter, ticker}, return_when=asyncio.FIRST_COMPLETED)
            if getter.done():
                frame = getter.result()
                if frame is None:
                    return
                yield frame
                getter = asyncio.create_task(subscription.queue.get())
            elif ticker.done():
                ticker.result()
                ticker = asyncio.create_task(clock.sleep(HEARTBEAT_SECONDS))
                yield HEARTBEAT_FRAME
    finally:
        getter.cancel()
        ticker.cancel()
        subscription.close()


def hub_of(request: Request) -> EventHub:
    hub = request.app.state.events
    if not isinstance(hub, EventHub):  # pragma: no cover - create_app always sets it
        raise RuntimeError("the app has no event hub")
    return hub


@router.get("/events")
async def get_events(request: Request) -> Response:
    subscription = hub_of(request).subscribe()
    if subscription is None:
        return JSONResponse(
            {"detail": f"at most {MAX_CLIENTS} event streams"},
            status_code=503,
            headers={"retry-after": "5"},
        )
    return _EventStream(subscription, daemon_of(request).clock)
