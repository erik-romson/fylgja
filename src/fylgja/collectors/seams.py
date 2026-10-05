"""Seams collectors use for the outside world (``PLAN.md`` §4.9).

A collector takes its process runner, filesystem view and line streams through these protocols, so
tests can substitute the fakes in :mod:`fylgja.testing.fakes` and need neither AeroSpace nor an
agent CLI. Time is the :class:`~fylgja.collectors.base.Clock` protocol in
:mod:`fylgja.collectors.base`.

This module holds types only. It is public, fully typed and imports nothing outside the standard
library.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "FileInfo",
    "FileSystem",
    "LineStream",
    "ProcessResult",
    "ProcessRunner",
]


@dataclass(frozen=True)
class ProcessResult:
    """What a finished command produced."""

    argv: tuple[str, ...]
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


@runtime_checkable
class ProcessRunner(Protocol):
    """Runs one command to completion.

    A missing command raises :class:`FileNotFoundError`; a command that outlives ``timeout``
    raises :class:`TimeoutError`. A non-zero exit status is a result, not an exception.
    """

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors subprocess timeouts
        stdin: bytes | None = None,
    ) -> ProcessResult: ...


@dataclass(frozen=True)
class FileInfo:
    size: int
    mtime: float
    is_dir: bool = False


@runtime_checkable
class FileSystem(Protocol):
    """The read side of the filesystem, plus deletion for delete-after-commit inboxes.

    Errors are the ordinary ones: :class:`FileNotFoundError`, :class:`PermissionError`,
    :class:`IsADirectoryError`.
    """

    def read_bytes(self, path: Path | str) -> bytes: ...

    def read_text(self, path: Path | str, encoding: str = "utf-8") -> str: ...

    def exists(self, path: Path | str) -> bool: ...

    def is_dir(self, path: Path | str) -> bool: ...

    def listdir(self, path: Path | str) -> list[str]:
        """Entry names (not paths) of a directory, sorted."""
        ...

    def stat(self, path: Path | str) -> FileInfo: ...

    def unlink(self, path: Path | str) -> None: ...


@runtime_checkable
class LineStream(Protocol):
    """A stream of newline-terminated lines, such as ``aerospace subscribe`` output.

    ``readline`` returns the next line including its newline. At the end of the stream it returns
    what is left (a torn last line has no newline), then ``b""`` for every later call.
    """

    async def readline(self) -> bytes: ...
