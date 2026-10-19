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
import errno
import os
import stat
from collections.abc import Sequence
from pathlib import Path

from fylgja.collectors.seams import FileInfo, ProcessResult
from fylgja.collectors.supervisor import terminate_process_group

__all__ = ["LocalFileSystem", "SubprocessRunner", "read_regular_file"]


def read_regular_file(path: Path | str) -> bytes:
    """The bytes of a regular file. The file is opened without blocking and checked on the open
    descriptor, so a FIFO or a device put in its place raises :class:`OSError` instead of hanging
    the caller; a directory raises :class:`IsADirectoryError`."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode):
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(path))
        if not stat.S_ISREG(mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
    except BaseException:
        os.close(fd)
        raise
    with open(fd, "rb") as handle:  # noqa: PTH123 - takes over the checked descriptor
        return handle.read()


class LocalFileSystem:
    """The real filesystem (the read side of :class:`~fylgja.collectors.seams.FileSystem`)."""

    def read_bytes(self, path: Path | str) -> bytes:
        return read_regular_file(path)

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

    def realpath(self, path: Path | str) -> str:
        return os.path.realpath(path)


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
