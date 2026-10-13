"""The recursion guard (WP8.1, ``PLAN-final-plan.md`` §4.4): a summarizer run never makes a session
appear in ``/now``, whether it summarizes a session or a folder (``docs/decisions/0053``).

A real ``claude -p`` would do two things Fylgja can see: register itself in the Claude session
registry (with its cwd, the summarizer cwd) and run the hooks the user installed. The fake runner
here does both, minus the model: it writes a registry row with the cwd it was given and runs the
real ``fylgja hook`` command, as a subprocess, with the environment the service gave it. Then the
real Claude registry poller and the real inbox drainer run over what was left, their records are
fed to the daemon, and ``/now`` is read back:

* the hook exits at once (``FYLGJA_SUMMARY=1``), so the inbox stays empty; a control run without
  the guard shows that the hook would otherwise have written;
* the registry row and the control envelope carry the summarizer cwd, which is always on the
  Claude collector's ``ignore_cwd`` list, so neither becomes a session; a poller without that
  entry shows that the row would otherwise have been one.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from api_support import BASE_URL, T0, Synthetic, make_daemon
from claude_support import World, row_data, write_row
from pydantic import BaseModel, ConfigDict
from summary_support import SECRET, Call, FakeRunner, summaries_config

from fylgja import paths
from fylgja.api.app import create_app
from fylgja.collectors.base import Capability, CollectorPlugin
from fylgja.collectors.claude.collector import ClaudeSettings, ignore_list
from fylgja.collectors.claude.inbox import InboxDrainer
from fylgja.core.daemon import Daemon
from fylgja.core.summaries import GUARD_ENV
from fylgja.models import Record
from fylgja.testing.fakes import FakeClock, RecordingSink

USER_SESSION = "00000000-0000-4000-8000-000000004101"
USER_KEY = f"claude:{USER_SESSION}"
SUMMARIZER_SESSION = "00000000-0000-4000-8000-00000000f001"
SUMMARIZER_KEY = f"claude:{SUMMARIZER_SESSION}"
SUMMARIZER_PID = 4999
LOGGER = logging.LoggerAdapter(logging.getLogger("test.recursion"), {})
WRITE = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}


class _AnySettings(BaseModel):
    """``defaults.toml`` gives ``[collectors.claude]`` keys the stand-in does not model."""

    model_config = ConfigDict(extra="allow")


class _StandIn(Synthetic):
    """An idle plugin named ``claude``; the test feeds it the real collector's records."""

    def plugin(self) -> CollectorPlugin:
        return replace(super().plugin(), config_model=_AnySettings)


def run_hook(event: str, env: Mapping[str, str], cwd: Path) -> None:
    """``fylgja hook <event>`` as Claude runs it: a subprocess fed the hook payload on stdin."""
    payload = {"session_id": SUMMARIZER_SESSION, "hook_event_name": event, "cwd": str(cwd)}
    subprocess.run(
        [sys.executable, "-m", "fylgja._entry", "hook", event],
        input=json.dumps(payload).encode(),
        env=dict(env),
        cwd=cwd,
        capture_output=True,
        timeout=20,
        check=True,
    )


class Summarizer(FakeRunner):
    """The fake ``claude -p``: registers itself and runs the installed hooks, then answers."""

    def __init__(self, world: World) -> None:
        super().__init__()
        self.world = world
        self.on_call = self._session

    async def _session(self, call: Call) -> None:
        self.world.ps.starts[SUMMARIZER_PID] = 1_790_000_000.0
        write_row(
            self.world.registry,
            row_data(SUMMARIZER_PID, SUMMARIZER_SESSION, status="busy", cwd=str(call.cwd)),
        )
        for event in ("SessionStart", "UserPromptSubmit", "Stop"):
            run_hook(event, call.env, call.cwd)


def inbox_files() -> list[Path]:
    inbox = paths.inbox_dir("claude")
    return sorted(inbox.glob("*.json")) if inbox.is_dir() else []


async def feed(daemon: Daemon, records: list[Record]) -> None:
    """Replay a collector's records through the daemon's ``claude`` sink, stamped now (the
    collector ran on its own fake clock)."""
    sink = daemon.sink("claude")
    for record in records:
        sink.emit(record.model_copy(update={"observed_at": daemon.clock.now()}))
    await daemon.settle()


async def session_keys(api: httpx.AsyncClient) -> list[str]:
    now: dict[str, Any] = (await api.get("/now")).json()
    return [s["key"] for s in now.get("sessions", [])]


async def item_paths(api: httpx.AsyncClient) -> list[str]:
    items: dict[str, Any] = (await api.get("/items")).json()
    return sorted(item.get("path", "") for item in items["items"])


async def summary_ref(api: httpx.AsyncClient, scope: str) -> str:
    """The user session's key, or the id of the folder item that holds the session."""
    if scope == "session":
        return USER_KEY
    items: dict[str, Any] = (await api.get("/items")).json()
    return next(
        item["id"]
        for item in items["items"]
        if any(place["ref"] == USER_KEY for place in item["places"])
    )


@pytest.mark.parametrize("scope", ["session", "folder"])
async def test_a_summarizer_run_never_makes_a_session_appear_in_now(
    tmp_path: Path, scope: str
) -> None:
    world = World(tmp_path / "sessions", ignore_cwd=ignore_list(ClaudeSettings()))
    assert str(paths.summarizer_dir()) in ignore_list(ClaudeSettings())
    world.add(4101, USER_SESSION, cwd="/work/alpha", status="busy")
    runner = Summarizer(world)
    daemon = make_daemon(
        _StandIn("claude", frozenset({Capability.SESSIONS})),
        user_config=summaries_config(),
        clock=FakeClock(T0),
        summary_runner=runner,
    )
    await daemon.start()
    try:
        app = create_app(daemon, secret=SECRET)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as api:
            await world.collector.poll()
            await feed(daemon, list(world.sink.records))
            assert await session_keys(api) == [USER_KEY]
            paths_before = await item_paths(api)
            ref = await summary_ref(api, scope)

            # the summary job: the summarizer registers itself and runs the hooks
            asked = await api.post(f"/summaries/{scope}/{ref}", headers=WRITE, content=b"{}")
            assert asked.status_code == 202, asked.text
            await daemon.summaries.drain()
            call = runner.calls[0]
            assert call.cwd == paths.summarizer_dir()
            assert {k: call.env.get(k) for k in GUARD_ENV} == dict(GUARD_ENV)
            assert (world.registry / f"{SUMMARIZER_PID}.json").exists()
            assert inbox_files() == []  # every hook exited at once

            # control: without the guard the hook would have written an envelope ...
            unguarded = {k: v for k, v in call.env.items() if k not in GUARD_ENV}
            run_hook("SessionStart", unguarded, call.cwd)
            assert len(inbox_files()) == 1

            # ... which the drainer drops for its cwd, as the poller drops the registry row
            world.sink.records.clear()
            await world.collector.poll()
            drained = RecordingSink()
            drainer = InboxDrainer(
                paths.inbox_dir("claude"),
                drained,
                FakeClock(),
                LOGGER,
                use_kqueue=False,
                ignore_cwd=ignore_list(ClaudeSettings()),
            )
            result = await drainer.drain()
            assert (result.ignored, result.committed) == (1, 0)
            assert inbox_files() == []
            assert drained.records == []
            assert world.collector.stats.ignored >= 1
            await feed(daemon, list(world.sink.records))

            assert await session_keys(api) == [USER_KEY]
            listed = (await api.get("/sessions")).json()
            assert [s["key"] for s in listed] == [USER_KEY]
            assert await item_paths(api) == paths_before  # no item for the summarizer cwd
            story = (await api.get(f"/summaries/{scope}/{ref}")).json()
            assert story["state"] == "fresh"
            assert (await api.get(f"/summaries/session/{SUMMARIZER_KEY}")).status_code == 404
    finally:
        await daemon.stop()

    # control: a poller without the summarizer cwd on its list would have shown the session
    unguarded_world = World(tmp_path / "sessions", ignore_cwd=("CodexBar/ClaudeProbe",))
    unguarded_world.ps.starts.update(world.ps.starts)
    await unguarded_world.collector.poll()
    seen = {
        f"{item.payload.kind}:{item.payload.native_id}"
        for record in unguarded_world.sink.records
        for item in getattr(record, "items", ())
        if hasattr(item.payload, "native_id")
    }
    assert SUMMARIZER_KEY in seen
