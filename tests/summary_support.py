"""Shared set-up of the summary tests (WP8.1): a fake model runner, a daemon whose picture has a
session and a workspace to summarize, and an ``/events`` client over the raw ASGI interface.

Workspace ``3`` is focused. Window ``100`` (a terminal) sits in it and hosts the Claude session
``claude:abc-123``, whose activity carries a facts block; a todo is open in the workspace. Nothing
here calls a model: :class:`FakeRunner` answers in its place and records every call.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from api_support import (
    PORT,
    T0,
    Win,
    activity_snapshot,
    make_daemon,
    session,
    session_snapshot,
    sessions_collector,
    window_snapshot,
    windows_collector,
    workspace_snapshot,
)
from fastapi import FastAPI

from fylgja.core.daemon import Daemon
from fylgja.core.summaries import RunOutput
from fylgja.models import SessionActivity
from fylgja.store import db

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"
FOCUSED = "3"
NATIVE_ID = "abc-123"
SESSION_KEY = f"claude:{NATIVE_ID}"
OTHER_KEYS = tuple(f"claude:other-{n}" for n in range(1, 6))
TODO_TEXT = "write the release notes"
ARC = "The session fixes the login form of alpha."
NOW = "It is making the email field validate. The validator is changed."
REPLY = f"ARC: {ARC}\nNOW: {NOW}\n"
PARAGRAPH = "Workspace 3 is where alpha's login form is fixed. One Claude session edits it now."


def facts(*, last_at: datetime = T0 - timedelta(minutes=5), turns: int = 4) -> dict[str, Any]:
    """A facts block as the Claude collector reports it (``last_at``: the transcript's last
    line)."""
    return {
        "title": "Fix the login form",
        "branch": "fix/login",
        "turns": turns,
        "tools": {"Edit": 3, "Bash": 2, "Read": 5},
        "files": ["/work/alpha/src/login.py", "/work/alpha/src/form.py"],
        "tasks": [
            {"status": "in_progress", "content": "validate the email"},
            {"status": "completed", "content": "read the code"},
        ],
        "prompt": "make the form validate",
        "said": "I changed the validator.",
        "prompts": ["look at the login form", "make the form validate"],
        "last_at": int(last_at.timestamp()),
    }


def activity(state: str = "working", **fact_options: Any) -> SessionActivity:
    return SessionActivity(state=state, title="Fix the login form", facts=facts(**fact_options))


# --------------------------------------------------------------------------------------------
# The fake runner
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    prompt: str
    cwd: Path
    env: dict[str, str]
    timeout: float


Script = RunOutput | BaseException


class FakeRunner:
    """A :class:`~fylgja.core.summaries.SummaryRunner` that records calls and answers from a
    script (then with :attr:`default`). While :meth:`hold` is in effect every call waits for
    :meth:`release`; :attr:`in_flight` and :attr:`peak` count calls waiting or answering."""

    def __init__(self, script: Sequence[Script] = (), *, default: str = REPLY) -> None:
        self.calls: list[Call] = []
        self.script: deque[Script] = deque(script)
        self.default = default
        self.in_flight = 0
        self.peak = 0
        self.on_call: Callable[[Call], Awaitable[None]] | None = None
        self._open = asyncio.Event()
        self._open.set()

    def hold(self) -> None:
        self._open.clear()

    def release(self) -> None:
        self._open.set()

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,  # noqa: ASYNC109 - the protocol's signature
    ) -> RunOutput:
        call = Call(prompt, cwd, dict(env), timeout)
        self.calls.append(call)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self.on_call is not None:
                await self.on_call(call)
            await self._open.wait()
            item = self.script.popleft() if self.script else RunOutput(0, self.default)
            if isinstance(item, BaseException):
                raise item
            return item
        finally:
            self.in_flight -= 1


async def wait_until(condition: Callable[[], bool], timeout: float = 10.0) -> None:  # noqa: ASYNC109
    """Poll ``condition`` on the event loop until it holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------------------------
# The world
# --------------------------------------------------------------------------------------------


def summaries_config(
    *, enabled: bool = True, ttl: int = 300, max_concurrent: int = 3
) -> dict[str, Any]:
    return {"summaries": {"enabled": enabled, "ttl_seconds": ttl, "max_concurrent": max_concurrent}}


async def set_activity(daemon: Daemon, items: Mapping[str, SessionActivity]) -> None:
    """Replace the activity of the listed sessions (a complete snapshot, stamped now)."""
    daemon.sink("agents").emit(activity_snapshot("agents", items, daemon.clock.now()))
    await daemon.settle()


@asynccontextmanager
async def story_world(
    runner: FakeRunner | None = None,
    *,
    enabled: bool = True,
    ttl: int = 300,
    max_concurrent: int = 3,
    others: int = 0,
    user_config: Mapping[str, Any] | None = None,
) -> AsyncIterator[Daemon]:
    """A started daemon with the picture described in the module docstring; ``others`` more
    Claude sessions run in the same window."""
    config = summaries_config(enabled=enabled, ttl=ttl, max_concurrent=max_concurrent)
    config.update(user_config or {})
    daemon = make_daemon(
        windows_collector(),
        sessions_collector(),
        user_config=config,
        summary_runner=FakeRunner() if runner is None else runner,
    )
    await daemon.start()
    try:
        wm, agents = daemon.sink("wm"), daemon.sink("agents")
        wm.emit(workspace_snapshot("wm", ["1", "2", FOCUSED], T0, focused=FOCUSED))
        wm.emit(window_snapshot("wm", [Win("100", FOCUSED, "Term", 500, "zsh — alpha")], T0))
        keys = (SESSION_KEY, *OTHER_KEYS[:others])
        pids = [501, *(511 + 10 * n for n in range(others))]
        agents.emit(
            session_snapshot(
                "agents",
                [
                    session(
                        "claude", key.partition(":")[2], pid=pid, cwd="/work/alpha", host_pid=500
                    )
                    for key, pid in zip(keys, pids, strict=True)
                ],
                T0,
            )
        )
        agents.emit(activity_snapshot("agents", {key: activity() for key in keys}, T0))
        await daemon.settle()
        await daemon.todos.add(TODO_TEXT, scope_kind="workspace", scope_ref=FOCUSED)
        await daemon.settle()
        yield daemon
    finally:
        await daemon.stop()


def stored(daemon: Daemon) -> list[dict[str, Any]]:
    """The ``summaries`` rows as stored (read-only connection)."""
    connection = db.connect_reader(daemon.writer.database)
    try:
        rows: list[sqlite3.Row] = connection.execute(
            "SELECT scope_kind, scope_ref, kind, text, input_digest, model, generated_at, "
            "error_kind FROM summaries ORDER BY scope_kind, scope_ref, kind"
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------------------------
# /events over raw ASGI (httpx's ASGI transport cannot stream)
# --------------------------------------------------------------------------------------------


class Stream:
    """One client of ``/events``: sends the request, queues the body chunks, and disconnects on
    :meth:`close`."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task: asyncio.Task[None] | None = None

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start" and not self._started.done():
            self._started.set_result(None)
        elif message["type"] == "http.response.body" and message.get("body"):
            self._chunks.put_nowait(message["body"])

    async def open(self) -> Stream:
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
            "headers": [(b"host", f"127.0.0.1:{PORT}".encode())],
            "server": ("127.0.0.1", PORT),
            "client": ("127.0.0.1", 50000),
        }
        self._incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        self._task = asyncio.create_task(self._app(scope, self._incoming.get, self._send))
        await asyncio.wait_for(self._started, 5)
        return self

    async def event(self, timeout: float = 5.0) -> tuple[str, dict[str, Any]]:  # noqa: ASYNC109
        """The next data frame (comments are skipped)."""
        while True:
            frame = (await asyncio.wait_for(self._chunks.get(), timeout)).decode()
            if frame.startswith(":"):
                continue
            lines = dict(line.split(": ", 1) for line in frame.strip().split("\n"))
            return lines["event"], json.loads(lines["data"])

    async def close(self) -> None:
        self._incoming.put_nowait({"type": "http.disconnect"})
        if self._task is not None:
            await asyncio.wait_for(self._task, 5)
