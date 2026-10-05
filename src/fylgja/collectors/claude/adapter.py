"""I/O seams of the Claude collector (``PLAN-final-plan.md`` WP1.7).

The collector reads the registry through a :class:`~fylgja.collectors.seams.FileSystem` and asks
``ps`` for process start times through a :class:`~fylgja.collectors.seams.ProcessRunner`.
:func:`default_seams` builds the real ones; tests replace that function (or pass their own
:class:`Seams`) and use :class:`~fylgja.testing.fakes.FakeProcessRunner`.

Nothing here writes: :class:`LocalFileSystem` has the protocol's ``unlink`` only because the
protocol has it, and the collector never calls it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fylgja.collectors.claude import parse
from fylgja.collectors.seams import FileInfo, FileSystem, ProcessResult, ProcessRunner
from fylgja.collectors.supervisor import terminate_process_group

__all__ = [
    "LocalFileSystem",
    "PsFailure",
    "Seams",
    "SubprocessRunner",
    "default_registry_dir",
    "default_seams",
    "local_epoch",
    "ps_start_times",
]


def default_registry_dir() -> Path:
    """``~/.claude/sessions``, with ``HOME`` read at call time."""
    return Path.home() / ".claude" / "sessions"


class LocalFileSystem:
    """The real filesystem."""

    def read_bytes(self, path: Path | str) -> bytes:
        with open(path, "rb") as handle:  # noqa: PTH123 - `open` is what the key-file test spies on
            return handle.read()

    def read_text(self, path: Path | str, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    def exists(self, path: Path | str) -> bool:
        return os.path.exists(path)  # noqa: PTH110

    def is_dir(self, path: Path | str) -> bool:
        return os.path.isdir(path)  # noqa: PTH112

    def listdir(self, path: Path | str) -> list[str]:
        return sorted(os.listdir(path))

    def stat(self, path: Path | str) -> FileInfo:
        info = os.stat(path)  # noqa: PTH116
        return FileInfo(
            size=info.st_size,
            mtime=info.st_mtime,
            is_dir=os.path.isdir(path),  # noqa: PTH112
            ino=info.st_ino,
            mtime_ns=info.st_mtime_ns,
        )

    def unlink(self, path: Path | str) -> None:
        os.unlink(path)  # noqa: PTH108


class SubprocessRunner:
    """Runs a command to completion; a missing binary raises ``FileNotFoundError``."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors subprocess timeouts
        stdin: bytes | None = None,
    ) -> ProcessResult:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout)
        except BaseException:  # timeout or cancellation: leave nothing running
            await terminate_process_group(process)
            raise
        code = -1 if process.returncode is None else process.returncode
        return ProcessResult(tuple(argv), code, stdout, stderr)


@dataclass(frozen=True)
class Seams:
    runner: ProcessRunner
    fs: FileSystem


def default_seams() -> Seams:
    """The real process runner and filesystem."""
    return Seams(runner=SubprocessRunner(), fs=LocalFileSystem())


def local_epoch(naive_local: datetime) -> float:
    """Epoch seconds of a naive local wall-clock time (``ps -o lstart=`` prints local time)."""
    return naive_local.astimezone().timestamp()


class PsFailure(Exception):
    """``ps`` could not tell. ``kind`` is a short code and ``detail`` is safe to show."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


async def ps_start_times(
    runner: ProcessRunner,
    ps: str,
    pids: Collection[int],
    *,
    timeout: float,  # noqa: ASYNC109 - a subprocess timeout
) -> dict[int, float]:
    """Start times (epoch seconds) of the processes among ``pids``; a dead pid is absent.

    ``ps`` exits 1 when none of the pids exists, which is an answer, not a failure.
    """
    if not pids:
        return {}
    argv = [ps, "-o", "pid=,lstart=", "-p", ",".join(str(pid) for pid in sorted(pids))]
    try:
        result = await runner.run(argv, timeout=timeout)
    except FileNotFoundError as exc:
        raise PsFailure("ps_missing", f"ps not found: {ps}") from exc
    except PermissionError as exc:
        raise PsFailure("ps_not_executable", f"ps is not executable: {ps}") from exc
    except TimeoutError as exc:
        raise PsFailure("ps_timeout", "ps timed out") from exc
    except OSError as exc:
        raise PsFailure("ps_failed", f"cannot run ps: {type(exc).__name__}") from exc
    if result.returncode not in (0, 1):
        raise PsFailure("ps_failed", f"ps exited with status {result.returncode}")
    text = result.stdout.decode("utf-8", errors="replace")
    return {pid: local_epoch(start) for pid, start in parse.parse_ps_starts(text).items()}
