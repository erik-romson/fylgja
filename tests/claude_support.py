"""Helpers shared by the Claude registry tests (the ``tests/`` tree has no packages).

``procStart`` is UTC wall-clock text and ``ps -o lstart=`` prints local time, so every helper builds
both texts from one instant and the tests pass in any time zone.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fylgja.collectors.claude.adapter import LocalFileSystem, Seams
from fylgja.collectors.claude.collector import ClaudeCollector, ClaudeSettings
from fylgja.collectors.context import build_context
from fylgja.collectors.seams import ProcessResult
from fylgja.testing.fakes import FakeClock, RecordingSink

FIXTURE_ROWS = Path(__file__).resolve().parent / "fixtures" / "claude" / "registry"
PS = "/bin/ps"
T0 = 1_790_000_000.0
"""A process start time (epoch seconds) used by the tests."""
_FORMAT = "%a %b %e %H:%M:%S %Y"


def proc_start_text(epoch: float) -> str:
    """The ``procStart`` text: UTC wall-clock."""
    return datetime.fromtimestamp(epoch, UTC).strftime(_FORMAT)


def ps_text(epoch: float) -> str:
    """The ``ps -o lstart=`` text: local wall-clock."""
    return datetime.fromtimestamp(epoch).strftime(_FORMAT)  # noqa: DTZ006 - local on purpose


def row_data(
    pid: int,
    session_id: str,
    *,
    status: str = "idle",
    name: str = "a name",
    cwd: str = "/work/project",
    start: float | None = T0,
    version: str = "2.1.289",
    entrypoint: str = "cli",
    **extra: Any,
) -> dict[str, Any]:
    """A registry row shaped like Claude's. ``start=None`` leaves out ``procStart``."""
    started_ms = int(((T0 if start is None else start) + 20) * 1000)
    data: dict[str, Any] = {
        "pid": pid,
        "sessionId": session_id,
        "cwd": cwd,
        "startedAt": started_ms,
        "version": version,
        "peerProtocol": 1,
        "peerFeatures": ["notify_idle"],
        "kind": "interactive",
        "entrypoint": entrypoint,
        "pidDomain": "darwin",
        "messagingSocketPath": f"/tmp/cc-socks/{pid}.sock",
        "name": name,
        "nameSource": "derived",
        "nameSince": started_ms,
        "updatedAt": started_ms,
        "status": status,
        "statusUpdatedAt": started_ms,
    }
    if start is not None:
        data["procStart"] = proc_start_text(start)
    data.update(extra)
    return data


def write_row(directory: Path, data: Mapping[str, Any]) -> Path:
    """Create or replace ``<pid>.json`` the way ``os.replace`` does (a new inode)."""
    path = directory / f"{data['pid']}.json"
    temp = directory / f".{data['pid']}.tmp"
    temp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temp, path)
    return path


def rewrite_in_place(path: Path, data: Mapping[str, Any]) -> None:
    """Rewrite ``path`` through the same inode, like Claude does."""
    with path.open("r+b") as handle:
        handle.truncate(0)
        handle.write(json.dumps(data).encode("utf-8"))


def copy_fixture_rows(directory: Path, names: Sequence[str] | None = None) -> list[Path]:
    """Copy the committed fixture rows into ``directory`` (all of them by default)."""
    directory.mkdir(parents=True, exist_ok=True)
    chosen = sorted(p.name for p in FIXTURE_ROWS.glob("*.json")) if names is None else names
    return [Path(shutil.copy(FIXTURE_ROWS / name, directory / name)) for name in chosen]


class PsRunner:
    """A ``ps -o pid=,lstart= -p <pids>`` that answers from a table of start times."""

    def __init__(self) -> None:
        self.starts: dict[int, float] = {}
        self.calls: list[tuple[str, ...]] = []
        self.error: BaseException | None = None
        self.returncode: int | None = None
        self.stdout: bytes | None = None

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors subprocess timeouts
        stdin: bytes | None = None,
    ) -> ProcessResult:
        self.calls.append(tuple(argv))
        if self.error is not None:
            raise self.error
        pids = [int(p) for p in argv[-1].split(",")]
        text = "".join(
            f"{pid:>5} {ps_text(self.starts[pid])}\n" for pid in pids if pid in self.starts
        )
        code = (0 if text else 1) if self.returncode is None else self.returncode
        out = text.encode() if self.stdout is None else self.stdout
        return ProcessResult(tuple(argv), code, out)


class World:
    """A collector over a real temporary registry directory, a fake ``ps`` and a fake clock."""

    def __init__(
        self,
        registry: Path,
        *,
        settings: ClaudeSettings | None = None,
        ignore_cwd: tuple[str, ...] = ("CodexBar/ClaudeProbe",),
    ) -> None:
        registry.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.clock = FakeClock()
        self.sink = RecordingSink()
        self.ps = PsRunner()
        self.settings = ClaudeSettings() if settings is None else settings
        self.collector = ClaudeCollector(
            self.settings,
            build_context("claude", self.sink, clock=self.clock),
            registry_dir=registry,
            seams=Seams(self.ps, LocalFileSystem()),
            ignore_cwd=ignore_cwd,
        )

    def add(self, pid: int, session_id: str, *, start: float | None = T0, **fields: Any) -> Path:
        """Write a row, make its process alive at ``start`` (``None``: no ``procStart``)."""
        self.ps.starts[pid] = T0 if start is None else start
        return write_row(self.registry, row_data(pid, session_id, start=start, **fields))
