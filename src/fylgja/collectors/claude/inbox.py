"""The hook inbox drain (``PLAN-final-plan.md`` WP2.2, A5; ``docs/api/inbox-envelope.md``).

``fylgja hook`` writes one envelope file per event into ``hook-inbox/claude/``. The drainer:

* **watches** the directory: a ``select.kqueue`` vnode watch on macOS, a 1 s poll everywhere else
  and whenever the watch cannot be set up (the directory does not exist yet, or the platform has
  no kqueue). The poll also runs while the watch is active, as a safety net;
* **reads** only the files named ``<received_ns>-<id>.json``, in name order, which is receive
  order. Dot files, ``dropped.count`` and ``quarantine/`` are never touched, and a ``.tmp-*`` file
  older than a minute (a writer killed mid-write) is deleted;
* **normalizes** each envelope into a minimized :class:`~fylgja.models.HookEvent`
  (:mod:`~fylgja.collectors.claude.normalize`, A5);
* **commits** it with ``emit_committed`` and **only then** unlinks the file (delete-after-commit).
  Files go to the sink in batches, so the writer groups them into few transactions, while a file
  is unlinked only after *its own* commit has resolved;
* **hands the envelope to a consumer** instead, when the inbox's owner keeps more than the A5
  allowlist (the norn collector, whose events are run state and not hook events): the validated
  envelope goes to ``consumer`` and the file is removed once it returned; the consumer raising
  :class:`~fylgja.collectors.claude.normalize.EnvelopeError` quarantines the file.
  ``read_in_thread`` false reads the files on the event loop: the consumer's inbox holds a few
  small envelopes, and the state then changes at deterministic points of the loop;
* **skips ignored working directories**: an event whose ``cwd`` matches ``ignore_cwd`` (the
  registry's list: CodexBar's probe, Fylgja's own summarizer) is removed without an event, so such a
  session never exists through its hooks either;
* **dedups on the envelope id**: the event's dedup key derives from it, so a file that survives a
  crash between commit and unlink is ingested a second time as a no-op and then removed;
* **quarantines** a file that cannot be used (not JSON, wrong schema, unattributable, or rejected
  by the ingest gate) by moving it to ``quarantine/``, where the nightly maintenance job deletes it
  after 7 days.

A file that could not be committed (a stopped writer, a database error) stays where it is and is
tried again at the next pass. The drainer logs counts and error kinds only, never file content.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import re
import select
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from fylgja.collectors.base import Clock, RecordSink
from fylgja.collectors.claude.normalize import (
    Envelope,
    EnvelopeError,
    normalize,
    parse_envelope,
)
from fylgja.collectors.claude.parse import cwd_ignored
from fylgja.models import HookEvent

__all__ = [
    "BATCH_SIZE",
    "ENVELOPE_NAME",
    "MAX_ENVELOPE_BYTES",
    "STALE_TMP_SECONDS",
    "DrainResult",
    "InboxDrainer",
    "kqueue_available",
]

BATCH_SIZE: Final = 256
MAX_ENVELOPE_BYTES: Final = 32 * 1024 * 1024
STALE_TMP_SECONDS: Final = 60.0
TMP_SWEEP_SECONDS: Final = 60.0
READ_WORKERS: Final = 8
ENVELOPE_NAME: Final = re.compile(r"[0-9]+-[0-9a-f-]{36}\.json")
_TMP_PREFIX: Final = ".tmp-"

_Consumer = Callable[[Envelope], None]
_Unlink = Callable[[Path], None]
_Rename = Callable[[Path, Path], None]


class _Consumed:
    """What :meth:`InboxDrainer._load` returns for an envelope the consumer took."""


def kqueue_available() -> bool:
    """Whether this platform can watch the inbox with ``select.kqueue``."""
    return sys.platform == "darwin" and hasattr(select, "kqueue")


@dataclass
class DrainResult:
    """What one drain pass did."""

    committed: int = 0
    """Files whose event was committed and that were then removed."""
    quarantined: int = 0
    failed: int = 0
    """Files left in place because they could not be committed or removed (tried again)."""
    ignored: int = 0
    """Files of an ignored working directory, removed without an event."""

    @property
    def handled(self) -> int:
        return self.committed + self.quarantined + self.ignored

    def add(self, other: DrainResult) -> None:
        self.committed += other.committed
        self.quarantined += other.quarantined
        self.failed += other.failed
        self.ignored += other.ignored


class _Watch:
    """A kqueue vnode watch on the inbox directory."""

    def __init__(self, directory: Path) -> None:
        self.fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            self.kq = select.kqueue()
            event = select.kevent(
                self.fd,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=(
                    select.KQ_NOTE_WRITE
                    | select.KQ_NOTE_EXTEND
                    | select.KQ_NOTE_DELETE
                    | select.KQ_NOTE_RENAME
                    | select.KQ_NOTE_REVOKE
                ),
            )
            self.kq.control([event], 0, 0)
        except BaseException:
            os.close(self.fd)
            raise

    def fileno(self) -> int:
        return self.kq.fileno()

    def consume(self) -> bool:
        """Take the pending events. ``False`` when the directory itself went away."""
        alive = True
        gone = select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE
        for event in self.kq.control(None, 16, 0):
            if event.fflags & gone:
                alive = False
        return alive

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.kq.close()
        with contextlib.suppress(OSError):
            os.close(self.fd)


class InboxDrainer:
    """Drains one hook inbox into a sink, delete-after-commit."""

    def __init__(
        self,
        directory: Path,
        sink: RecordSink,
        clock: Clock,
        logger: logging.LoggerAdapter[logging.Logger],
        *,
        source: str = "claude",
        kind: str = "claude",
        poll_interval: float = 1.0,
        batch_size: int = BATCH_SIZE,
        use_kqueue: bool | None = None,
        ignore_cwd: tuple[str, ...] = (),
        consumer: _Consumer | None = None,
        read_in_thread: bool = True,
        unlink: _Unlink | None = None,
        rename: _Rename | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._directory = directory
        self._quarantine = directory / "quarantine"
        self._sink = sink
        self._clock = clock
        self._logger = logger
        self._source = source
        self._kind = kind
        self._ignore_cwd = ignore_cwd
        self._consumer = consumer
        self._threaded = read_in_thread
        self._poll = poll_interval
        self._batch = batch_size
        self._want_kqueue = kqueue_available() if use_kqueue is None else use_kqueue
        self._unlink: _Unlink = os.unlink if unlink is None else unlink
        self._rename: _Rename = os.rename if rename is None else rename
        self._watch: _Watch | None = None
        self._last_sweep: float | None = None
        self._failure: tuple[str, str] | None = None
        self.totals = DrainResult()
        self.passes = 0
        self.watching = False
        """Whether the last wait used the kqueue watch rather than the poll alone."""

    # -- state ------------------------------------------------------------------------------

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def failure(self) -> tuple[str, str] | None:
        """``(error_kind, detail)`` while files cannot be committed or the inbox cannot be read."""
        return self._failure

    # -- one pass ---------------------------------------------------------------------------

    def _names(self) -> list[str] | None:
        """The envelope file names in order; ``None`` when the inbox does not exist."""
        try:
            names = os.listdir(self._directory)
        except FileNotFoundError:
            self._set_failure(None)
            return None
        except NotADirectoryError:
            self._set_failure(("inbox_not_a_directory", "the hook inbox is not a directory"))
            return None
        except OSError as exc:
            self._set_failure(("inbox_unreadable", f"cannot list the hook inbox ({_kind(exc)})"))
            return None
        return sorted(name for name in names if ENVELOPE_NAME.fullmatch(name) is not None)

    async def drain(self) -> DrainResult:
        """Drain the inbox until a listing shows nothing left to try. Never raises for a file.

        A file that stays (a failed commit) ends the drain, so a persistent failure cannot spin.
        """
        total = DrainResult()
        self._sweep_tmp()
        while True:
            names = self._names()
            if names is None:
                break
            if not names:
                self._set_failure(None)  # a clean listing with nothing left: all is well
                break
            result = DrainResult()
            for start in range(0, len(names), self._batch):
                result.add(await self._batch_of(names[start : start + self._batch]))
                await asyncio.sleep(0)  # let other tasks run between batches
            total.add(result)
            if result.failed or result.handled == 0:
                break
        self.totals.add(total)
        self.passes += 1
        return total

    async def _batch_of(self, names: list[str]) -> DrainResult:
        result = DrainResult()
        pending: list[tuple[str, HookEvent]] = []
        consumed: list[Path] = []
        files = [self._directory / name for name in names]
        # Opening a freshly written file can cost a millisecond on a busy machine, so the files
        # of a batch are read in parallel.
        contents = (
            await asyncio.to_thread(_read_many, files) if self._threaded else _read_each(files)
        )
        for path, content in zip(files, contents, strict=True):
            event = self._load(path, content, result)
            if isinstance(event, _Consumed):
                consumed.append(path)
            elif event is not None:
                pending.append((path.name, event))
        for path in consumed:
            self._remove(path, result)
        if not pending:
            if consumed and not result.failed:
                self._set_failure(None)
            return result
        # Submitted in name order; the writer keeps that order and commits them in few transactions.
        outcomes = await asyncio.gather(
            *(self._sink.emit_committed(event) for _, event in pending), return_exceptions=True
        )
        for (name, _), outcome in zip(pending, outcomes, strict=True):
            path = self._directory / name
            if outcome is None:
                self._remove(path, result)
            elif isinstance(outcome, asyncio.CancelledError):
                raise outcome
            elif isinstance(outcome, ValueError):
                # The ingest gate's ``RecordRejected``: the record can never be accepted.
                self._quarantine_file(path, str(outcome), result)
            elif isinstance(outcome, BaseException):
                result.failed += 1
                self._set_failure(
                    ("inbox_commit_failed", f"cannot commit hook events ({_kind(outcome)})")
                )
            else:  # pragma: no cover - emit_committed returns None
                self._remove(path, result)
        if not result.failed:
            self._set_failure(None)
        return result

    def _load(
        self, path: Path, content: bytes | None | OSError, result: DrainResult
    ) -> HookEvent | _Consumed | None:
        """The event of one file, or ``None`` after the file was quarantined or skipped.

        A :class:`_Consumed` says the envelope went to the consumer and the file is only left to
        remove.
        """
        if isinstance(content, OSError):
            if isinstance(content, FileNotFoundError):
                return None  # another pass removed it
            if content.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
                self._quarantine_file(path, "not a regular file", result)
            else:
                result.failed += 1
                self._set_failure(
                    ("inbox_unreadable", f"cannot read an envelope ({_kind(content)})")
                )
            return None
        if content is None:
            self._quarantine_file(path, "envelope too large", result)
            return None
        try:
            envelope = parse_envelope(content)
            if self._consumer is not None:
                if envelope.source != self._source:
                    raise EnvelopeError("source does not match the inbox")
                self._consumer(envelope)
                return _Consumed()
            event = normalize(envelope, source=self._source, kind=self._kind)
        except EnvelopeError as exc:
            self._quarantine_file(path, str(exc), result)
            return None
        cwd = event.payload.cwd
        if cwd is not None and self._ignore_cwd and cwd_ignored(cwd, self._ignore_cwd):
            # The registry drops these sessions too (CodexBar's probe, Fylgja's summarizer); a hook
            # event of one must not create a session of its own.
            self._remove_ignored(path, result)
            return None
        return event

    def _remove(self, path: Path, result: DrainResult) -> None:
        try:
            self._unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            result.failed += 1  # committed already: the replay is a no-op
            self._set_failure(
                ("inbox_unlink_failed", f"cannot remove a drained file ({_kind(exc)})")
            )
            return
        result.committed += 1

    def _remove_ignored(self, path: Path, result: DrainResult) -> None:
        try:
            self._unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            result.failed += 1
            self._set_failure(
                ("inbox_unlink_failed", f"cannot remove a drained file ({_kind(exc)})")
            )
            return
        result.ignored += 1

    def _quarantine_file(self, path: Path, reason: str, result: DrainResult) -> None:
        try:
            self._quarantine.mkdir(mode=0o700, exist_ok=True)
            self._rename(path, self._quarantine / path.name)
        except FileNotFoundError:
            return
        except OSError as exc:
            result.failed += 1
            self._set_failure(
                ("inbox_quarantine_failed", f"cannot quarantine a file ({_kind(exc)})")
            )
            return
        result.quarantined += 1
        self._logger.warning("hook inbox: quarantined a file: %s", reason)

    def _sweep_tmp(self) -> None:
        """Delete ``.tmp-*`` files a killed writer left behind (older than a minute)."""
        now = self._clock.monotonic()
        if self._last_sweep is not None and now - self._last_sweep < TMP_SWEEP_SECONDS:
            return
        self._last_sweep = now
        try:
            names = [n for n in os.listdir(self._directory) if n.startswith(_TMP_PREFIX)]
        except OSError:
            return
        cutoff = self._clock.now().timestamp() - STALE_TMP_SECONDS
        for name in names:
            path = self._directory / name
            try:
                info = os.lstat(path)
                if info.st_mtime < cutoff and (info.st_mode & 0o170000) == 0o100000:
                    self._unlink(path)
            except OSError:
                continue

    def _set_failure(self, failure: tuple[str, str] | None) -> None:
        before = self._failure
        if failure is not None and (before is None or before[0] != failure[0]):
            self._logger.warning("hook inbox failed: %s", failure[0])
        elif failure is None and before is not None:
            self._logger.info("hook inbox recovered")
        self._failure = failure

    # -- the watch --------------------------------------------------------------------------

    def _open_watch(self) -> None:
        if self._watch is not None or not self._want_kqueue:
            return
        try:
            self._watch = _Watch(self._directory)
        except FileNotFoundError:
            return  # the directory appears later; the poll finds the files meanwhile
        except (OSError, AttributeError):
            self._logger.info("hook inbox: no kqueue watch, polling instead")
            self._want_kqueue = False

    def _close_watch(self) -> None:
        watch, self._watch = self._watch, None
        if watch is not None:
            watch.close()

    async def _wait(self) -> None:
        """Return when the directory changed or the poll interval passed."""
        watch = self._watch
        self.watching = watch is not None
        if watch is None:
            await self._clock.sleep(self._poll)
            return
        loop = asyncio.get_running_loop()
        changed = asyncio.Event()
        try:
            loop.add_reader(watch.fileno(), changed.set)
        except (NotImplementedError, OSError, ValueError):
            self._close_watch()
            self._want_kqueue = False
            await self._clock.sleep(self._poll)
            return
        sleeper = asyncio.ensure_future(self._clock.sleep(self._poll))
        waiter = asyncio.ensure_future(changed.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            loop.remove_reader(watch.fileno())
            for future in (sleeper, waiter):
                future.cancel()
            await asyncio.gather(sleeper, waiter, return_exceptions=True)
        if changed.is_set() and not watch.consume():
            self._close_watch()  # the directory was replaced: watch the new one

    async def run(self) -> None:
        """Drain and wait until cancelled. The watch is closed on the way out."""
        try:
            while True:
                self._open_watch()  # before the pass, so nothing arrives unseen between the two
                await self.drain()
                await self._wait()
        finally:
            self._close_watch()


def _read_each(paths: list[Path]) -> list[bytes | None | OSError]:
    """Read ``paths`` one after the other on the calling thread (a few small files)."""
    contents: list[bytes | None | OSError] = []
    for path in paths:
        try:
            contents.append(_read_regular(path))
        except OSError as exc:
            contents.append(exc)
    return contents


def _read_many(paths: list[Path]) -> list[bytes | None | OSError]:
    """Read ``paths`` with a few threads; a failure is returned in its place, not raised."""

    def read(path: Path) -> bytes | None | OSError:
        try:
            return _read_regular(path)
        except OSError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
        return list(pool.map(read, paths))


def _read_regular(path: Path) -> bytes | None:
    """The file's bytes; ``None`` when it is larger than :data:`MAX_ENVELOPE_BYTES`.

    A symbolic link or a directory raises ``OSError`` (``O_NOFOLLOW``).
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(descriptor)
        if (info.st_mode & 0o170000) != 0o100000:
            raise OSError(errno.ENXIO, "not a regular file")
        if info.st_size > MAX_ENVELOPE_BYTES:
            return None
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _kind(exc: BaseException) -> str:
    return type(exc).__name__
