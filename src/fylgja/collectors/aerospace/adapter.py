"""I/O seams of the AeroSpace collector (``PLAN-final-plan.md`` WP1.6).

The collector never starts a process itself. It takes a
:class:`~fylgja.collectors.seams.ProcessRunner` for the one-shot list commands and a stream opener
for the long-lived ``aerospace subscribe --all``.
:func:`default_seams` builds the real ones; tests replace that function (or pass their own
:class:`Seams`) and use :class:`~fylgja.testing.fakes.FakeProcessRunner` and
:class:`~fylgja.testing.fakes.FakeLineStream`.

Subprocesses start in their own process group and are killed with it when the caller is cancelled.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Final

from fylgja.collectors.seams import LineStream, ProcessResult, ProcessRunner
from fylgja.collectors.supervisor import terminate_process_group
from fylgja.config import ConfigError, load_config, load_defaults

__all__ = [
    "OVERLONG_LINE",
    "STREAM_LINE_LIMIT",
    "Seams",
    "StreamOpener",
    "SubprocessRunner",
    "default_seams",
    "open_subprocess_stream",
    "resolve_binary",
]

logger = logging.getLogger("fylgja.collector.aerospace")

STREAM_LINE_LIMIT: Final = 1 << 20
OVERLONG_LINE: Final = b"\x00overlong\n"
"""What the stream returns for a line longer than :data:`STREAM_LINE_LIMIT`: not valid JSON."""

StreamOpener = Callable[[Sequence[str]], AbstractAsyncContextManager[LineStream]]


@dataclass(frozen=True)
class Seams:
    runner: ProcessRunner
    open_stream: StreamOpener


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


class _ProcessLineStream:
    def __init__(self, reader: asyncio.StreamReader) -> None:
        self._reader = reader

    async def readline(self) -> bytes:
        try:
            return await self._reader.readline()
        except ValueError:  # the line exceeded the reader's limit; the reader dropped it
            return OVERLONG_LINE


@asynccontextmanager
async def open_subprocess_stream(argv: Sequence[str]) -> AsyncIterator[LineStream]:
    """Start ``argv`` and yield its stdout as lines. The process group is killed on exit."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        limit=STREAM_LINE_LIMIT,
    )
    try:
        assert process.stdout is not None
        yield _ProcessLineStream(process.stdout)
    finally:
        await terminate_process_group(process)


def default_seams() -> Seams:
    """The real process runner and subprocess stream."""
    return Seams(runner=SubprocessRunner(), open_stream=open_subprocess_stream)


def resolve_binary() -> str:
    """The binary from ``[paths] aerospace``; an invalid config falls back to the defaults."""
    try:
        return str(load_config().paths.aerospace)
    except ConfigError:
        logger.warning("config is invalid; using the default aerospace path")
        paths = load_defaults()["paths"]
        return str(paths["aerospace"])
