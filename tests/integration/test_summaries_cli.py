"""``fylgja story session <key>`` and ``fylgja story ws <n>`` (WP8.1, A8) against a running
in-process daemon.

The daemon runs behind a real Uvicorn server on a free loopback port, with a fake model runner.
The CLI runs in a worker thread (it is a blocking client): it opens ``/events``, posts the request,
waits for ``summary.ready`` and then fetches and prints the summary. The model call runs in the
daemon only.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import uvicorn
from summary_support import (
    ARC,
    FOCUSED,
    NATIVE_ID,
    NOW,
    PARAGRAPH,
    SECRET,
    SESSION_KEY,
    FakeRunner,
    story_world,
    wait_until,
)
from typer.testing import CliRunner

from fylgja import paths
from fylgja.api import sse
from fylgja.api.app import create_app, server_config
from fylgja.cli import client
from fylgja.cli.app import STORY_DISABLED
from fylgja.cli.app import app as cli
from fylgja.core.daemon import Daemon
from fylgja.core.summaries import RunOutput


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Served:
    def __init__(self, daemon: Daemon, runner: FakeRunner, port: int) -> None:
        self.daemon = daemon
        self.runner = runner
        self.port = port
        self.cli = CliRunner()

    async def fylgja(self, *args: str, code: int = 0) -> str:
        result = await asyncio.to_thread(self.cli.invoke, cli, list(args))
        assert result.exit_code == code, (args, result.exit_code, result.output)
        return str(result.output)

    async def get(self, path: str) -> Any:
        return await asyncio.to_thread(client.get_json, path)


@asynccontextmanager
async def serving(runner: FakeRunner, *, enabled: bool = True) -> AsyncIterator[Served]:
    port = free_port()
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(f"[api]\nport = {port}\n", encoding="utf-8")
    paths.api_secret_file().write_text(SECRET + "\n", encoding="utf-8")
    async with story_world(runner, enabled=enabled, user_config={"api": {"port": port}}) as daemon:
        server = uvicorn.Server(server_config(create_app(daemon, secret=SECRET), daemon.config))
        task = asyncio.create_task(server.serve())
        while not server.started:  # noqa: ASYNC110 - Uvicorn offers no readiness event
            await asyncio.sleep(0.01)
        try:
            yield Served(daemon, runner, port)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 20)


@pytest.fixture
async def served() -> AsyncIterator[Served]:
    async with serving(FakeRunner()) as found:
        yield found


async def _released_after_the_call(served: Served, *args: str, code: int = 0) -> str:
    """Run the CLI while the model holds its answer; release it once the daemon called it, so the
    CLI can only print the summary by waiting for ``summary.ready``."""
    served.runner.hold()
    calls = len(served.runner.calls)
    command = asyncio.create_task(served.fylgja(*args, code=code))
    await wait_until(lambda: len(served.runner.calls) > calls or command.done())
    await asyncio.sleep(0.05)
    assert not command.done()  # it is waiting on the event stream
    served.runner.release()
    return await command


async def test_story_session_waits_for_summary_ready_and_prints_arc_and_now(
    served: Served,
) -> None:
    output = await _released_after_the_call(served, "story", "session", SESSION_KEY)
    assert f"ARC: {ARC}" in output
    assert f"NOW: {NOW}" in output
    assert "[fresh, as of " in output
    assert len(served.runner.calls) == 1

    # the second time it is answered from the cache at once, with no model call
    again = await served.fylgja("story", "session", SESSION_KEY)
    assert f"ARC: {ARC}" in again
    assert len(served.runner.calls) == 1


async def test_story_session_takes_a_native_id_prefix_and_prints_json(served: Served) -> None:
    output = await _released_after_the_call(served, "story", "session", NATIVE_ID[:3], "--json")
    answer = json.loads(output)
    assert answer["scope"] == "session"
    assert answer["ref"] == SESSION_KEY
    assert (answer["state"], answer["arc"], answer["now"]) == ("fresh", ARC, NOW)


async def test_story_ws_prints_the_workspace_paragraph(served: Served) -> None:
    served.runner.default = PARAGRAPH
    output = await _released_after_the_call(served, "story", "ws", FOCUSED)
    assert PARAGRAPH in output.replace("\n", " ")
    assert "ARC:" not in output
    assert (await served.get(f"/summaries/workspace/{FOCUSED}"))["state"] == "fresh"


async def test_story_exits_1_with_a_clear_message_when_summaries_are_disabled() -> None:
    runner = FakeRunner()
    async with serving(runner, enabled=False) as served:
        output = await served.fylgja("story", "session", SESSION_KEY, code=1)
        assert STORY_DISABLED in output
        assert "[summaries] enabled = true" in output
        output = await served.fylgja("story", "ws", FOCUSED, code=1)
        assert STORY_DISABLED in output
    assert runner.calls == []


async def test_story_exits_1_when_no_summary_arrives_in_time(served: Served) -> None:
    served.runner.hold()
    output = await served.fylgja("story", "session", SESSION_KEY, "--wait", "0.3", code=1)
    assert "no summary after 0.3 s" in output
    assert served.daemon.summaries.pending("session", SESSION_KEY)
    served.runner.release()
    await served.daemon.summaries.drain()
    assert (await served.get(f"/summaries/session/{SESSION_KEY}"))["state"] == "fresh"


async def test_story_exits_1_and_says_why_when_the_summary_failed(served: Served) -> None:
    served.runner.script.append(RunOutput(1, ""))
    output = await _released_after_the_call(served, "story", "session", SESSION_KEY, code=1)
    assert "[failed" in output
    assert "nonzero_exit" in output


async def test_story_of_an_unknown_session_or_workspace_fails_clearly(served: Served) -> None:
    output = await served.fylgja("story", "session", "claude:nobody", code=1)
    assert "404" in output
    assert "no live session has that key" in output
    output = await served.fylgja("story", "ws", "99", code=1)
    assert "no workspace has that name" in output
    output = await served.fylgja("story", "session", "zzz", code=1)
    assert "no live session matches" in output
    assert served.runner.calls == []


async def test_story_exits_3_when_the_daemon_is_down() -> None:
    port = free_port()
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(f"[api]\nport = {port}\n", encoding="utf-8")
    paths.api_secret_file().write_text(SECRET + "\n", encoding="utf-8")
    result = await asyncio.to_thread(CliRunner().invoke, cli, ["story", "ws", FOCUSED])
    assert result.exit_code == client.EXIT_DAEMON_DOWN
    assert "daemon not running" in result.output


def test_the_story_commands_are_listed_in_help() -> None:
    result = CliRunner().invoke(cli, ["story", "--help"])
    assert result.exit_code == 0
    assert "session" in result.output
    assert "ws" in result.output
    assert "--wait" in CliRunner().invoke(cli, ["story", "session", "--help"]).output


# --------------------------------------------------------------------------------------------
# The change-stream client
# --------------------------------------------------------------------------------------------


async def test_the_event_stream_reads_frames_and_stops_waiting_at_the_deadline(
    served: Served,
) -> None:
    served.runner.default = PARAGRAPH
    stream = await asyncio.to_thread(client.open_events)
    seen: list[str] = []

    def match(name: str, data: Any) -> bool:
        seen.append(name)
        return name == "summary.ready"

    waiter = asyncio.create_task(asyncio.to_thread(stream.wait_for, match, 10.0))
    served.daemon.view.touch()  # a now.changed first, then the summary
    await asyncio.to_thread(client.send_json, "POST", f"/summaries/workspace/{FOCUSED}")
    data = await waiter
    assert data == {"scope": "workspace", "ref": FOCUSED, "state": "fresh"}
    assert seen[-1] == "summary.ready"
    assert "now.changed" in seen
    stream.close()  # closing again is harmless

    quiet = await asyncio.to_thread(client.open_events)
    assert await asyncio.to_thread(quiet.wait_for, match, 0.2) is None


async def test_the_event_stream_when_nothing_answers_or_the_daemon_refuses(
    served: Served,
) -> None:
    with pytest.raises(client.DaemonDown):
        await asyncio.to_thread(client.open_events, url=f"http://127.0.0.1:{free_port()}")
    streams = [await asyncio.to_thread(client.open_events) for _ in range(sse.MAX_CLIENTS)]
    try:
        with pytest.raises(client.ApiError, match="503"):
            await asyncio.to_thread(client.open_events)
    finally:
        for stream in streams:
            await asyncio.to_thread(stream.close)
    assert served.daemon.running
