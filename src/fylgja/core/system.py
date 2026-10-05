"""The real filesystem and process runner for the core (``PLAN.md`` §4.9 seams).

The state engine reads the machine through the same seams as collectors
(:mod:`fylgja.collectors.seams`): the resolver's project lookup checks that paths exist and the host
adapters read the cmux store and the JetBrains opened-project lists. The daemon passes these real
implementations; tests pass :class:`~fylgja.testing.fakes.FakeFS` and
:class:`~fylgja.testing.fakes.FakeProcessRunner` instead.

Nothing here writes: :meth:`LocalFileSystem.unlink` exists only because the protocol has it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from pathlib import Path

from fylgja.collectors.seams import FileInfo, ProcessResult
from fylgja.collectors.supervisor import terminate_process_group

__all__ = ["LocalFileSystem", "SubprocessRunner"]


class LocalFileSystem:
    """The real filesystem (the read side of :class:`~fylgja.collectors.seams.FileSystem`)."""

    def read_bytes(self, path: Path | str) -> bytes:
        return Path(path).read_bytes()

    def read_text(self, path: Path | str, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    def exists(self, path: Path | str) -> bool:
        return Path(path).exists()

    def is_dir(self, path: Path | str) -> bool:
        return Path(path).is_dir()

    def listdir(self, path: Path | str) -> list[str]:
        return sorted(os.listdir(path))

    def stat(self, path: Path | str) -> FileInfo:
        info = Path(path).stat()
        return FileInfo(
            size=info.st_size,
            mtime=info.st_mtime,
            is_dir=Path(path).is_dir(),
            ino=info.st_ino,
            mtime_ns=info.st_mtime_ns,
        )

    def unlink(self, path: Path | str) -> None:
        Path(path).unlink()


class SubprocessRunner:
    """Runs a command to completion in its own process group; a missing binary raises
    :class:`FileNotFoundError` and a timeout kills the group and raises :class:`TimeoutError`."""

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
