"""``GET /events`` (WP2.5): delivery, heartbeat, the client cap and cleanup.

httpx's ASGI transport returns a response only when the app has finished, so a stream cannot be
read through it. :class:`Stream` drives the ASGI app directly instead: it sends the request, hands
the body chunks to a queue and ends with an ``http.disconnect``, as a server does.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from api_support import (
    BASE_URL,
    PORT,
    T0,
    Win,
    activity_snapshot,
    focus_event,
    make_daemon,
    session,
    session_snapshot,
    sessions_collector,
    window_snapshot,
    windows_collector,
)
from fastapi import FastAPI

from fylgja.api.app import create_app
from fylgja.api.sse import (
    HEARTBEAT_FRAME,
    HEARTBEAT_SECONDS,
    MAX_CLIENTS,
    QUEUE_SIZE,
    EventHub,
    format_event,
)
from fylgja.core.daemon import Daemon
from fylgja.core.live_view import LiveView
from fylgja.models import SessionActivity
from fylgja.testing.fakes import FakeClock

pytestmark = pytest.mark.skipif(
    not os.path.isdir("/dev/fd") and not os.path.isdir("/proc/self/fd"),
    reason="needs /dev/fd or /proc/self/fd",
)


class Stream:
    """One client of ``/events`` over the raw ASGI interface."""

    def __init__(self, app: FastAPI, *, host: str = f"127.0.0.1:{PORT}") -> None:
        self._app = app
        self._host = host
        self._incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._started: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._task: asyncio.Task[None] | None = None
        self.status = 0
        self.headers: dict[str, str] = {}

    async def _receive(self) -> dict[str, Any]:
        return await self._incoming.get()

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode(): v.decode() for k, v in message["headers"]}
            self._started.set_result(message)
        elif message["type"] == "http.response.body" and message.get("body"):
            self._chunks.put_nowait(message["body"])

    async def open(self, timeout: float = 5) -> Stream:  # noqa: ASYNC109
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/events",
            "raw_path": b"/events",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", self._host.encode())],
            "server": ("127.0.0.1", PORT),
            "client": ("127.0.0.1", 50000),
        }
        self._incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        self._task = asyncio.create_task(self._app(scope, self._receive, self._send))
        await asyncio.wait_for(self._started, timeout)
        return self

    async def chunk(self, timeout: float = 1.0) -> bytes:  # noqa: ASYNC109
        return await asyncio.wait_for(self._chunks.get(), timeout)

    async def event(self, timeout: float = 1.0) -> tuple[str, dict[str, Any]]:  # noqa: ASYNC109
        """The next data frame (comments are skipped)."""
        while True:
            frame = (await self.chunk(timeout)).decode()
            if frame.startswith(":"):
                continue
            lines = dict(line.split(": ", 1) for line in frame.strip().split("\n"))
            return lines["event"], json.loads(lines["data"])

    async def comments(self, wait: float = 0.05) -> list[bytes]:
        """The comment frames that arrive within ``wait`` seconds."""
        found: list[bytes] = []
        try:
            while True:
                frame = await self.chunk(wait)
                if frame.startswith(b":"):
                    found.append(frame)
        except TimeoutError:
            return found

    def pending(self) -> list[bytes]:
        frames: list[bytes] = []
        while not self._chunks.empty():
            frames.append(self._chunks.get_nowait())
        return frames

    async def close(self) -> None:
        self._incoming.put_nowait({"type": "http.disconnect"})
        if self._task is not None:
            await asyncio.wait_for(self._task, 5)

    @property
    def done(self) -> bool:
        return self._task is not None and self._task.done()


@pytest.fixture
async def daemon() -> AsyncIterator[Daemon]:
    instance = make_daemon(windows_collector(), sessions_collector())
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


def _fd_count() -> int:
    return len(os.listdir("/dev/fd" if sys.platform == "darwin" else "/proc/self/fd"))


async def _commit(daemon: Daemon, title: str = "Secret title", *, workspace: str = "1") -> None:
    daemon.sink("wm").emit(window_snapshot("wm", [Win("1", workspace, "App", 10, title)], T0))
    await daemon.settle()


async def test_a_committed_change_arrives_as_now_changed_within_a_second(daemon: Daemon) -> None:
    stream = await Stream(create_app(daemon)).open()
    try:
        assert stream.status == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert stream.headers["cache-control"] == "no-store"
        before = daemon.view.version
        await _commit(daemon)
        name, data = await stream.event(1.0)
        assert (name, data) == ("now.changed", {"version": before + 1})
        assert daemon.view.version >= before + 1
    finally:
        await stream.close()


async def test_events_carry_ids_and_versions_never_titles(daemon: Daemon) -> None:
    app = create_app(daemon)
    stream = await Stream(app).open()
    try:
        await _commit(daemon, "Secret title")
        daemon.sink("wm").emit(focus_event("wm", "1", "1", T0))
        await daemon.settle()
        await asyncio.sleep(0.05)
        raw = b"".join(stream.pending())
        assert b"event: now.changed" in raw
        assert b"event: focus.changed" in raw
        assert b"Secret" not in raw
    finally:
        await stream.close()


async def test_focus_and_session_state_events(daemon: Daemon) -> None:
    stream = await Stream(create_app(daemon)).open()
    try:
        agents = daemon.sink("agents")
        agents.emit(session_snapshot("agents", [session("claude", "s-1", pid=900)], T0))
        agents.emit(
            activity_snapshot(
                "agents", {"claude:s-1": SessionActivity(state="working", title="Hidden")}, T0
            )
        )
        await daemon.settle()
        seen: list[tuple[str, dict[str, Any]]] = []
        while True:
            try:
                seen.append(await stream.event(0.1))
            except TimeoutError:
                break
        states = [d for n, d in seen if n == "session.state"]
        assert states
        assert states[-1]["session_key"] == "claude:s-1"
        assert states[-1]["state"] == "working"
        assert all(set(d) == {"session_key", "state", "version"} for d in states)
        assert not any("Hidden" in json.dumps(d) for _, d in seen)

        # the same state again is not a state change
        agents.emit(
            activity_snapshot(
                "agents",
                {"claude:s-1": SessionActivity(state="working", title="Hidden 2")},
                T0 + timedelta(seconds=1),
            )
        )
        await daemon.settle()
        assert [n for n, _ in _drain(stream)].count("session.state") == 0

        daemon.sink("wm").emit(focus_event("wm", "7", "3", T0 + timedelta(seconds=2)))
        await daemon.settle()
        name, data = await stream.event()
        assert name == "focus.changed"
        assert data == {"window_id": "7", "workspace": "3", "version": daemon.view.version}

        # the session ends: its state goes to null
        agents.emit(session_snapshot("agents", [], T0 + timedelta(seconds=3)))
        await daemon.settle()
        ended = [d for n, d in _drain(stream) if n == "session.state"]
        assert ended[-1]["session_key"] == "claude:s-1"
        assert ended[-1]["state"] is None
    finally:
        await stream.close()


def _drain(stream: Stream) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in stream.pending():
        text = frame.decode()
        if text.startswith(":"):
            continue
        lines = dict(line.split(": ", 1) for line in text.strip().split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


async def test_a_heartbeat_arrives_every_15_seconds_on_the_injected_clock() -> None:
    clock = FakeClock(T0)
    instance = make_daemon(windows_collector(), clock=clock)
    await instance.start()
    stream = await Stream(create_app(instance)).open()
    try:
        assert HEARTBEAT_SECONDS == 15
        assert await stream.comments() == [b": connected\n\n"]
        await clock.advance(14)
        assert await stream.comments() == []
        await clock.advance(1)
        assert await stream.comments() == [HEARTBEAT_FRAME]
        await clock.advance(15)
        assert await stream.comments() == [HEARTBEAT_FRAME]
        await clock.advance(45)
        assert await stream.comments() == [HEARTBEAT_FRAME] * 3
    finally:
        await stream.close()
        await instance.stop()


async def test_the_17th_client_gets_503_and_a_freed_slot_is_reusable(daemon: Daemon) -> None:
    app = create_app(daemon)
    streams = [await Stream(app).open() for _ in range(MAX_CLIENTS)]
    try:
        assert all(s.status == 200 for s in streams)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as http:
            refused = await http.get("/events")
            assert refused.status_code == 503
            assert refused.headers["retry-after"] == "5"
        await streams.pop().close()
        again = await Stream(app).open()
        assert again.status == 200
        streams.append(again)
    finally:
        for stream in streams:
            await stream.close()
    assert app.state.events.clients == 0


async def test_a_foreign_host_is_rejected(daemon: Daemon) -> None:
    app = create_app(daemon)
    stream = await Stream(app, host=f"evil.example:{PORT}").open()
    assert stream.status == 403
    await stream.close()
    assert app.state.events.clients == 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http:
        assert (await http.get("/events", headers={"Host": "evil.example"})).status_code == 403
        assert (
            await http.get("/events", headers={"Host": f"evil.example:{PORT}"})
        ).status_code == 403


async def test_100_connect_disconnect_cycles_return_tasks_and_fds_to_the_baseline(
    daemon: Daemon,
) -> None:
    app = create_app(daemon)
    hub: EventHub = app.state.events

    async def cycle() -> None:
        stream = await Stream(app).open()
        await stream.chunk()
        await stream.close()

    await cycle()  # warm up whatever is created lazily
    await asyncio.sleep(0.05)
    tasks = len(asyncio.all_tasks())
    fds = _fd_count()
    for _ in range(100):
        await cycle()
    await asyncio.sleep(0.05)
    assert len(asyncio.all_tasks()) == tasks
    assert _fd_count() == fds
    assert hub.clients == 0
    assert not hub.attached


async def test_a_client_that_leaves_before_the_first_frame_frees_its_slot(daemon: Daemon) -> None:
    app = create_app(daemon)
    hub: EventHub = app.state.events
    stream = Stream(app)
    stream._incoming.put_nowait({"type": "http.disconnect"})
    await stream.open()
    await asyncio.sleep(0.05)
    assert stream.done
    assert hub.clients == 0


async def test_a_dropped_client_stops_the_listening_to_the_live_view(daemon: Daemon) -> None:
    app = create_app(daemon)
    hub: EventHub = app.state.events
    assert not hub.attached
    first = await Stream(app).open()
    second = await Stream(app).open()
    assert hub.attached
    await first.close()
    assert hub.attached
    await second.close()
    assert not hub.attached


async def test_published_events_reach_every_client_and_close_ends_the_streams(
    daemon: Daemon,
) -> None:
    app = create_app(daemon)
    hub: EventHub = app.state.events
    one = await Stream(app).open()
    two = await Stream(app).open()
    hub.publish("summary.ready", {"scope": "workspace:1", "version": 4}, event_id=4)
    for stream in (one, two):
        assert await stream.event() == ("summary.ready", {"scope": "workspace:1", "version": 4})
    hub.close()
    await asyncio.sleep(0.05)
    assert one.done
    assert two.done
    assert hub.clients == 0


async def test_published_event_frame() -> None:
    assert format_event("summary.ready", {"b": 1, "a": 2}, 7) == (
        b'event: summary.ready\nid: 7\ndata: {"a":2,"b":1}\n\n'
    )
    assert format_event("x", {}) == b"event: x\ndata: {}\n\n"


async def test_a_slow_client_loses_its_oldest_frames_not_the_stream() -> None:
    view = LiveView()
    hub = EventHub(view)
    subscription = hub.subscribe()
    assert subscription is not None
    for index in range(QUEUE_SIZE + 5):
        hub.publish("now.changed", {"version": index})
    assert subscription.queue.qsize() == QUEUE_SIZE
    first = subscription.queue.get_nowait()
    assert first is not None
    assert b'"version":5' in first
    subscription.close()
    subscription.close()
    assert hub.clients == 0


async def test_the_hub_refuses_past_its_limit() -> None:
    hub = EventHub(LiveView(), max_clients=2)
    a, b = hub.subscribe(), hub.subscribe()
    assert a is not None
    assert b is not None
    assert hub.subscribe() is None
    a.close()
    assert hub.subscribe() is not None
