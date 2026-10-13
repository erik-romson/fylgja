"""Deterministic fakes for collector tests (``PLAN.md`` §4.9, ``PLAN-final-plan.md`` WP1.5).

Every fake is in-memory and needs neither the network nor an installed tool.

- :class:`FakeClock` is virtual time: ``sleep`` blocks until :meth:`FakeClock.advance` passes its
  deadline.
- :class:`FakeProcessRunner` records argv and returns scripted output, exit codes, timeouts and
  errors.
- :class:`FakeFS` is a small in-memory filesystem with injectable errors.
- :class:`FakeLineStream` feeds lines, torn lines and end of stream to a reader.
- :class:`FakeStreamOpener` hands scripted line streams to a collector that opens a long-lived
  command (``aerospace subscribe``) and counts opens and open streams.
- :class:`RecordingSink` is a :class:`~fylgja.collectors.base.RecordSink` that keeps what it got.
"""

from __future__ import annotations

import asyncio
import errno
import heapq
import itertools
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from fylgja.collectors.seams import FileInfo, LineStream, ProcessResult
from fylgja.models import EntityKind, Record

__all__ = [
    "EPOCH",
    "FakeClock",
    "FakeFS",
    "FakeLineStream",
    "FakeProcessRunner",
    "FakeStreamOpener",
    "RecordingSink",
    "settle",
]

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


async def settle(rounds: int = 100) -> None:
    """Yield to the event loop so every runnable task makes progress."""
    for _ in range(rounds):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------------------------
# Clock
# --------------------------------------------------------------------------------------------


class FakeClock:
    """Virtual time. ``sleep`` returns once :meth:`advance` reaches its deadline."""

    def __init__(self, start: datetime | None = None) -> None:
        origin = EPOCH if start is None else start
        if origin.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self._origin = origin
        self._mono = 0.0
        self._waiters: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()
        self.sleeps: list[float] = []
        """Every requested sleep duration, in call order."""

    def now(self) -> datetime:
        return self._origin + timedelta(seconds=self._mono)

    def monotonic(self) -> float:
        return self._mono

    def suspend(self, seconds: float) -> None:
        """The machine sleeps: wall time jumps, monotonic time does not."""
        self._origin += timedelta(seconds=seconds)

    @property
    def pending(self) -> int:
        """Sleeps that have not reached their deadline."""
        return sum(1 for _, _, future in self._waiters if not future.done())

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (self._mono + seconds, next(self._seq), future))
        await future

    async def advance(self, seconds: float) -> None:
        """Move time forward, waking sleepers in deadline order."""
        await self.advance_to(self._mono + seconds)

    async def advance_to(self, target: float) -> None:
        await settle()
        while self._waiters and self._waiters[0][0] <= target:
            deadline, _, future = heapq.heappop(self._waiters)
            self._mono = max(self._mono, deadline)
            if not future.done():
                future.set_result(None)
            await settle()
        self._mono = max(self._mono, target)
        await settle()


# --------------------------------------------------------------------------------------------
# Process runner
# --------------------------------------------------------------------------------------------


def _bytes(value: bytes | str) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


@dataclass
class _Script:
    prefix: tuple[str, ...]
    stdout: bytes
    stderr: bytes
    returncode: int
    timeout: bool
    error: BaseException | None
    hang: bool
    remaining: int | None


class FakeProcessRunner:
    """Records argv and answers from a script.

    The longest scripted argv prefix that matches wins. A command with no script behaves like a
    missing command and raises :class:`FileNotFoundError`.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        """The argv of every call, in order."""
        self.stdin: list[bytes | None] = []
        self.timeouts: list[float | None] = []
        self._scripts: list[_Script] = []
        self._active = 0

    @property
    def active(self) -> int:
        """Calls that have started and not finished (a hanging command counts)."""
        return self._active

    def script(
        self,
        *argv_prefix: str,
        stdout: bytes | str = b"",
        stderr: bytes | str = b"",
        returncode: int = 0,
        timeout: bool = False,
        error: BaseException | None = None,
        hang: bool = False,
        times: int | None = None,
    ) -> None:
        """Script the answer for argv starting with ``argv_prefix``.

        ``timeout`` raises :class:`TimeoutError`, ``error`` raises that exception, ``hang`` blocks
        until the caller is cancelled. ``times`` limits how often the script answers.
        """
        if not argv_prefix:
            raise ValueError("a script needs an argv prefix")
        self._scripts.append(
            _Script(
                tuple(argv_prefix),
                _bytes(stdout),
                _bytes(stderr),
                returncode,
                timeout,
                error,
                hang,
                times,
            )
        )

    def _match(self, argv: tuple[str, ...]) -> _Script | None:
        best: _Script | None = None
        for script in self._scripts:
            if argv[: len(script.prefix)] != script.prefix:
                continue
            if script.remaining is not None and script.remaining <= 0:
                continue
            if best is None or len(script.prefix) > len(best.prefix):
                best = script
        return best

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors subprocess timeouts
        stdin: bytes | None = None,
    ) -> ProcessResult:
        command = tuple(argv)
        if not command:
            raise ValueError("empty argv")
        self.calls.append(command)
        self.stdin.append(stdin)
        self.timeouts.append(timeout)
        script = self._match(command)
        if script is None:
            raise FileNotFoundError(errno.ENOENT, "command not found", command[0])
        if script.remaining is not None:
            script.remaining -= 1
        self._active += 1
        try:
            if script.hang:
                await asyncio.Event().wait()
            if script.error is not None:
                raise script.error
            if script.timeout:
                raise TimeoutError(f"{command[0]} timed out")
            return ProcessResult(command, script.returncode, script.stdout, script.stderr)
        finally:
            self._active -= 1


# --------------------------------------------------------------------------------------------
# Filesystem
# --------------------------------------------------------------------------------------------


def _key(path: Path | str) -> PurePosixPath:
    return PurePosixPath(str(path))


class FakeFS:
    """An in-memory filesystem. Parent directories exist implicitly."""

    def __init__(self, files: Mapping[Path | str, bytes | str] | None = None) -> None:
        self._files: dict[PurePosixPath, tuple[bytes, float]] = {}
        self._inodes: dict[PurePosixPath, int] = {}
        self._next_inode = itertools.count(1)
        self._dirs: set[PurePosixPath] = {PurePosixPath("/")}
        self._errors: dict[PurePosixPath, OSError] = {}
        self.reads: list[PurePosixPath] = []
        """Every path read, in order."""
        for path, data in (files or {}).items():
            self.add_file(path, data)

    def _add_parents(self, path: PurePosixPath) -> None:
        self._dirs.update(path.parents)

    def add_file(self, path: Path | str, data: bytes | str = b"", *, mtime: float = 0.0) -> None:
        key = _key(path)
        self._add_parents(key)
        self._files[key] = (_bytes(data), mtime)
        self._inodes.setdefault(key, next(self._next_inode))  # a rewrite keeps the inode

    def replace_file(
        self, path: Path | str, data: bytes | str = b"", *, mtime: float = 0.0
    ) -> None:
        """Write ``path`` the way an atomic replace does: a new inode."""
        self._inodes.pop(_key(path), None)
        self.add_file(path, data, mtime=mtime)

    def add_dir(self, path: Path | str) -> None:
        key = _key(path)
        self._dirs.add(key)
        self._add_parents(key)

    def fail(self, path: Path | str, error: OSError) -> None:
        """Make every access to ``path`` raise ``error`` (for example :class:`PermissionError`)."""
        self._errors[_key(path)] = error

    def heal(self, path: Path | str) -> None:
        self._errors.pop(_key(path), None)

    def _check(self, key: PurePosixPath) -> None:
        error = self._errors.get(key)
        if error is not None:
            raise error

    def _missing(self, key: PurePosixPath) -> FileNotFoundError:
        return FileNotFoundError(errno.ENOENT, "no such file or directory", str(key))

    def read_bytes(self, path: Path | str) -> bytes:
        key = _key(path)
        self.reads.append(key)
        self._check(key)
        if key in self._dirs:
            raise IsADirectoryError(errno.EISDIR, "is a directory", str(key))
        if key not in self._files:
            raise self._missing(key)
        return self._files[key][0]

    def read_text(self, path: Path | str, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    def exists(self, path: Path | str) -> bool:
        key = _key(path)
        return key in self._files or key in self._dirs

    def is_dir(self, path: Path | str) -> bool:
        return _key(path) in self._dirs

    def listdir(self, path: Path | str) -> list[str]:
        key = _key(path)
        self._check(key)
        if key not in self._dirs:
            raise self._missing(key)
        names = {p.name for p in (*self._files, *self._dirs) if p != key and p.parent == key}
        return sorted(names)

    def stat(self, path: Path | str) -> FileInfo:
        key = _key(path)
        self._check(key)
        if key in self._dirs:
            return FileInfo(size=0, mtime=0.0, is_dir=True)
        if key not in self._files:
            raise self._missing(key)
        data, mtime = self._files[key]
        return FileInfo(
            size=len(data), mtime=mtime, ino=self._inodes[key], mtime_ns=round(mtime * 1e9)
        )

    def unlink(self, path: Path | str) -> None:
        key = _key(path)
        self._check(key)
        if key not in self._files:
            raise self._missing(key)
        del self._files[key]
        self._inodes.pop(key, None)


# --------------------------------------------------------------------------------------------
# Line stream
# --------------------------------------------------------------------------------------------


class FakeLineStream:
    """Lines arrive when the test feeds them. ``feed`` takes raw bytes, so a line can be torn."""

    def __init__(self, *chunks: bytes | str, eof: bool = False) -> None:
        self._buffer = bytearray()
        self._eof = False
        self._wake = asyncio.Event()
        for chunk in chunks:
            self.feed(chunk)
        if eof:
            self.feed_eof()

    @property
    def at_eof(self) -> bool:
        return self._eof and not self._buffer

    def feed(self, data: bytes | str) -> None:
        """Append raw bytes; they need not end in a newline."""
        if self._eof:
            raise ValueError("stream already ended")
        self._buffer += _bytes(data)
        self._wake.set()

    def feed_line(self, line: bytes | str) -> None:
        self.feed(_bytes(line) + b"\n")

    def feed_eof(self) -> None:
        self._eof = True
        self._wake.set()

    async def readline(self) -> bytes:
        while True:
            index = self._buffer.find(b"\n")
            if index >= 0:
                line = bytes(self._buffer[: index + 1])
                del self._buffer[: index + 1]
                return line
            if self._eof:
                rest = bytes(self._buffer)
                self._buffer.clear()
                return rest
            self._wake.clear()
            await self._wake.wait()

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._lines()

    async def _lines(self) -> AsyncIterator[bytes]:
        while True:
            line = await self.readline()
            if not line:
                return
            yield line


class FakeStreamOpener:
    """Opens scripted :class:`FakeLineStream` objects, one per open, in order.

    With no stream left, an open yields a new silent stream that stays open until the collector
    leaves it, unless ``error`` is set: then every such open raises it (for example
    :class:`FileNotFoundError` for a missing binary). Pass the instance where a collector wants a
    ``Callable[[Sequence[str]], AsyncContextManager[LineStream]]``.
    """

    def __init__(self, *streams: FakeLineStream, error: BaseException | None = None) -> None:
        self._queue = list(streams)
        self.error = error
        self.calls: list[tuple[str, ...]] = []
        """The argv of every open, in order."""
        self.active = 0
        """Streams that are open right now."""

    def add(self, stream: FakeLineStream) -> None:
        self._queue.append(stream)

    def __call__(self, argv: Sequence[str]) -> AbstractAsyncContextManager[LineStream]:
        return self._open(tuple(argv))

    @asynccontextmanager
    async def _open(self, argv: tuple[str, ...]) -> AsyncIterator[LineStream]:
        self.calls.append(argv)
        if self._queue:
            stream = self._queue.pop(0)
        elif self.error is not None:
            raise self.error
        else:
            stream = FakeLineStream()
        self.active += 1
        try:
            yield stream
        finally:
            self.active -= 1


# --------------------------------------------------------------------------------------------
# Sink
# --------------------------------------------------------------------------------------------


@dataclass
class RecordingSink:
    """A :class:`~fylgja.collectors.base.RecordSink` that records every record it receives."""

    records: list[Record] = field(default_factory=list)
    committed: list[Record] = field(default_factory=list)
    """The records ``emit_committed`` resolved for, in order (also present in ``records``)."""
    commit_error: Exception | None = None
    """When set, ``emit_committed`` raises it and the record is not committed."""

    def emit(self, record: Record) -> None:
        self.records.append(record)

    async def emit_committed(self, record: Record) -> None:
        await asyncio.sleep(0)
        if self.commit_error is not None:
            raise self.commit_error
        self.records.append(record)
        self.committed.append(record)

    def of_kind(self, kind: EntityKind) -> list[Record]:
        return [r for r in self.records if r.entity_kind is kind]

    def of_source(self, source: str) -> list[Record]:
        return [r for r in self.records if r.source == source]

    def clear(self) -> None:
        self.records.clear()
        self.committed.clear()
