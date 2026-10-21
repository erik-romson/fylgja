"""The Phase 3 acceptance payload: one ``/now`` over all four session collectors (WP3.6).

The real Claude, Codex, OpenCode and norn collectors each run one poll (or inbox drain) over their
own sanitized fixtures, on fake clocks, fake process tables and a temporary directory. Whatever
they emit is replayed through the ingest-gate sink of a daemon whose plugins are idle stand-ins
for the four, so the records take the daemon's real path (gate, writer, live view, resolver), and
``GET /now`` is read back.

``scripts/record_now_fixtures.py`` writes the result to ``tests/fixtures/now/phase3-agents.json``,
and ``tests/integration/test_phase3_agents.py`` builds it again and compares. Nothing here writes
into the repository.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from api_support import T0, Synthetic, client, make_daemon
from claude_support import World, row_data, write_row
from codex_support import Rig as CodexRig
from codex_support import make_home
from codex_support import script_processes as script_codex
from norn_support import INBOX_NOW, SUBSCRIBER_PID, deliver, index_of
from norn_support import Rig as NornRig
from opencode_support import Rig as OpenCodeRig
from opencode_support import build_db
from opencode_support import script_processes as script_opencode
from pydantic import BaseModel, ConfigDict
from transcript_support import CLAUDE_TRANSCRIPTS

from fylgja.collectors.base import Capability, CollectorPlugin
from fylgja.collectors.claude import transcript
from fylgja.models import Record
from fylgja.testing.fakes import FakeClock

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE = FIXTURES / "now" / "phase3-agents.json"
KINDS = ("claude", "codex", "opencode", "norn")
RUN_SESSION = "00000000-0000-4000-8000-00000000a001"
"""The Claude session id the norn fixtures name as driven by the run (``sdk-py``)."""
NOW = T0
"""The daemon's fixed clock, the instant of the other recorded ``/now`` fixtures."""
RESTAMPED = KINDS
"""Each collector ran on its own fixture clock: every record is replayed at ``NOW`` instead, so
the ingest gate accepts it and the order of arrival is the order of replay."""
PROJECT = "/work/example/claude-app"


async def _claude(root: Path) -> list[Record]:
    world = World(root / "claude" / "sessions")
    world.add(4101, "00000000-0000-4000-8000-000000004101", cwd=PROJECT, status="busy")
    world.add(4102, "00000000-0000-4000-8000-000000004102", cwd=PROJECT, status="waiting")
    world.add(4105, "00000000-0000-4000-8000-000000004105", cwd=PROJECT, status="idle")
    # the session the norn run drives: entrypoint sdk-py, started by the run's agent call
    world.ps.starts[4110] = 1_790_000_000.0
    write_row(
        world.registry,
        row_data(4110, RUN_SESSION, status="busy", cwd=PROJECT, entrypoint="sdk-py"),
    )
    # transcripts: a session running a tool, and one with a task list
    projects = root / "claude" / "projects" / transcript.project_slug(PROJECT)
    projects.mkdir(parents=True)
    for number, name in ((4101, "running-tool-synthetic"), (4105, "task-tools-synthetic")):
        shutil.copy(
            CLAUDE_TRANSCRIPTS / f"{name}.jsonl",
            projects / f"00000000-0000-4000-8000-00000000{number}.jsonl",
        )
    await world.collector.poll()
    return list(world.sink.records)


async def _codex(root: Path) -> list[Record]:
    home = make_home(root)
    rig = CodexRig(home)
    script_codex(rig.runner, home)
    await rig.collector.poll()
    return list(rig.sink.records)


async def _opencode(root: Path) -> list[Record]:
    rig = OpenCodeRig(build_db(root / "opencode.db"))
    script_opencode(rig.runner)
    await rig.collector.poll()
    return list(rig.sink.records)


async def _norn() -> list[Record]:
    """A run that asked a question (``WaitingInput``) after its agent session started."""
    rig = NornRig(start=INBOX_NOW, alive={SUBSCRIBER_PID})
    deliver(rig.inbox_dir, "lifecycle", stop=index_of("lifecycle", "WaitingInput") + 1)
    for envelope in rig.inbox_dir.glob("*.json"):  # the recorded home is a placeholder home
        text = envelope.read_text(encoding="utf-8")
        envelope.write_text(
            text.replace("/Users/user/projects/", "/work/example/"), encoding="utf-8"
        )
    await rig.drain()
    await rig.clock.advance(40)
    await rig.poll()
    return list(rig.sink.records)


class _AnySettings(BaseModel):
    """The built-in tables of ``defaults.toml`` carry keys the stand-ins do not model."""

    model_config = ConfigDict(extra="allow")


class _StandIn(Synthetic):
    """An idle plugin under a built-in name; the test replays the real collector's records."""

    def plugin(self) -> CollectorPlugin:
        return replace(super().plugin(), config_model=_AnySettings)


def _at_now(record: Record) -> Record:
    return record.model_copy(update={"observed_at": NOW})


def _plugins() -> list[Synthetic]:
    return [_StandIn(name, frozenset({Capability.SESSIONS})) for name in KINDS]


@contextmanager
def _utc() -> Iterator[None]:
    """Local time is UTC inside: ``ps -o lstart=`` start times are read as local time, and the
    recorded payload must not depend on the machine's time zone."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = before
        time.tzset()


async def build_now(workdir: Path | None = None) -> dict[str, Any]:
    """Run the four collectors over their fixtures and return what ``GET /now`` answers."""
    with _utc():
        return await _build_now(workdir)


async def _build_now(workdir: Path | None) -> dict[str, Any]:
    owned = workdir is None
    root = Path(tempfile.mkdtemp(prefix="fylgja-phase3-")) if workdir is None else workdir
    try:
        records: dict[str, list[Record]] = {
            "claude": await _claude(root),
            "codex": await _codex(root),
            "opencode": await _opencode(root),
        }
        records["norn"] = await _norn()
        daemon = make_daemon(*_plugins(), clock=FakeClock(NOW))
        await daemon.start()
        try:
            for name in KINDS:
                for record in records[name]:
                    daemon.sink(name).emit(_at_now(record) if name in RESTAMPED else record)
            await daemon.settle()
            async with client(daemon) as http:
                response = await http.get("/now")
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            return payload
        finally:
            await daemon.stop()
    finally:
        if owned:
            shutil.rmtree(root, ignore_errors=True)
