"""Helpers shared by the norn collector tests (the ``tests/`` tree has no packages).

Two committed fixture sets, both from ``scripts/gen_norn_fixtures.py``:

* ``tests/fixtures/norn/files/<case>/`` is norn's own state-file layout (``<stem>.history``,
  ``<stem>.checkpoint`` and ``<stem>.runs/<n>/{run.json,events.jsonl}``). A test copies a case under
  its own directory, because the collector reads real files and the walk gates on their mtime;
* ``tests/fixtures/norn/inbox/<scenario>/`` holds the ``fylgja.hook/1`` envelopes of the subscriber
  plugin, one file per event, in receive order.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fylgja.collectors.context import build_context
from fylgja.collectors.norn import adapter
from fylgja.collectors.norn.collector import NornCollector, NornSettings
from fylgja.testing.fakes import FakeClock, FakeProcessRunner, RecordingSink

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "norn"
FILES = FIXTURES / "files"
INBOX = FIXTURES / "inbox"
PS = "/bin/ps"
LSOF = "/usr/sbin/lsof"

FILES_NOW = datetime(2026, 10, 5, 13, 30, tzinfo=UTC)
"""Two minutes after the last line of the ``finished`` fixture."""
INBOX_NOW = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
"""The instant of the first inbox envelope."""
SUBSCRIBER_PID = 51234

FINISHED_KEY = "norn:plan_with_review@"  # followed by six hex digits and ":1"


def copy_case(tmp_path: Path, case: str, *, at: datetime = FILES_NOW, age: float = 60.0) -> Path:
    """Copy ``files/<case>`` to ``tmp_path/<case>``, every mtime ``age`` seconds before ``at``."""
    target = tmp_path / case
    shutil.copytree(FILES / case, target)
    stamp = (at - timedelta(seconds=age)).timestamp()
    for path in target.rglob("*"):
        os.utime(path, (stamp, stamp))
    return target


def inbox_files(scenario: str) -> list[Path]:
    return sorted((INBOX / scenario).glob("*.json"))


def event_name(path: Path) -> str:
    return str(json.loads(path.read_text(encoding="utf-8"))["event_arg"])


def index_of(scenario: str, event: str) -> int:
    """The position of the first envelope of ``scenario`` that carries ``event``."""
    names = [event_name(path) for path in inbox_files(scenario)]
    return names.index(event)


def deliver(directory: Path, scenario: str, *, start: int = 0, stop: int | None = None) -> int:
    """Copy envelopes ``start`` to ``stop`` (exclusive) of the scenario into ``directory``.

    They keep their names, so the drain takes them in receive order. Returns how many were copied.
    """
    directory.mkdir(parents=True, exist_ok=True)
    chosen = inbox_files(scenario)[start:stop]
    for path in chosen:
        shutil.copyfile(path, directory / path.name)
    return len(chosen)


def ps_table(*lines: tuple[int, str]) -> str:
    return "".join(f"{pid:>6} {args}\n" for pid, args in lines)


def lsof_cwds(cwds: dict[int, str]) -> str:
    return "".join(f"p{pid}\nfcwd\nn{cwd}\n" for pid, cwd in cwds.items())


class Rig:
    """One collector over fake time, a recording sink, scripted ``ps``/``lsof`` and fake pids."""

    def __init__(
        self,
        *,
        roots: list[str] | None = None,
        start: datetime = FILES_NOW,
        alive: set[int] | None = None,
        **settings: Any,
    ) -> None:
        self.clock = FakeClock(start)
        self.sink = RecordingSink()
        self.runner = FakeProcessRunner()
        self.alive: set[int] = set() if alive is None else alive
        self.settings = NornSettings(
            roots=[] if roots is None else roots, inbox_watch="poll", **settings
        )
        self.seams = adapter.Seams(
            self.runner, adapter.LocalFileSystem(), pid_alive=lambda pid: pid in self.alive
        )
        self.collector = NornCollector(
            self.settings, build_context("norn", self.sink, clock=self.clock), seams=self.seams
        )

    @property
    def inbox_dir(self) -> Path:
        return self.collector.inbox.directory

    def script_processes(self, table: str = "", cwds: dict[int, str] | None = None) -> None:
        self.runner.script(PS, stdout=table)
        self.runner.script(LSOF, stdout=lsof_cwds(cwds or {}))

    async def poll(self) -> None:
        await self.collector.poll()

    async def drain(self) -> None:
        await self.collector.inbox.drain()

    def states(self) -> dict[str, str]:
        """The state of every listed run by the part of its key after ``norn:``."""
        return {key.removeprefix("norn:"): value for key, value in self.collector.states.items()}


def by_suffix(states: dict[str, str], suffix: str) -> str | None:
    """The state of the one run whose key ends with ``suffix`` (``:<run id>``)."""
    found = [value for key, value in states.items() if key.endswith(suffix)]
    assert len(found) <= 1, states
    return found[0] if found else None
