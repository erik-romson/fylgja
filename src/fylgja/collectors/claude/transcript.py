"""Incremental tail of Claude session transcripts (``PLAN-final-plan.md`` WP1.7).

A transcript is ``~/.claude/projects/<cwd with every non-alphanumeric character as "-">/
<sessionId>.jsonl``, appended to by a live session. :class:`TranscriptTail` remembers a byte
offset and returns only the objects completed since the last poll:

- a **torn last line** (a write caught half-way) is carried and completed by the next poll; a
  carried line that already parses as an object (a last line with no newline) is delivered at once;
- the tail **resets** and starts from byte 0 when the file shrinks, when its inode changes (an
  atomic replace) or when the first bytes differ from those seen first (a rewrite in place);
- one poll reads at most ``max_read`` bytes, so a very large transcript is consumed over several
  polls instead of blocking the collector;
- a line longer than ``max_line`` is skipped, never buffered without limit.

:class:`SessionTranscript` pairs a tail with a :class:`~fylgja.collectors.claude.FactsAccumulator`
and rebuilds the facts from the start whenever the tail resets. :func:`locate` finds the file of a
session. Only ``*.jsonl`` files named after a session id are ever opened.

Nothing here logs or exposes transcript content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from fylgja.collectors.claude.facts import FactsAccumulator, PendingTool, decode_line
from fylgja.collectors.seams import FileSystem, RangeReader

__all__ = [
    "SessionTranscript",
    "TailUpdate",
    "TranscriptTail",
    "locate",
    "project_slug",
]

DEFAULT_MAX_READ: Final = 8 << 20
DEFAULT_MAX_LINE: Final = 16 << 20
HEAD_BYTES: Final = 256

_SESSION_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,200}")
_SLUG_UNSAFE: Final = re.compile(r"[^A-Za-z0-9]")


def project_slug(cwd: str) -> str:
    """The directory name Claude uses for a working directory (``/a/.b`` becomes ``-a--b``)."""
    return _SLUG_UNSAFE.sub("-", cwd)


def locate(fs: FileSystem, projects_dir: Path, session_id: str, cwd: str | None) -> Path | None:
    """The transcript file of a session, or ``None`` when there is none (yet).

    The directory named after the cwd is tried first; otherwise every project directory is
    searched, because a session may have been started in another directory and moved. A session
    id that is not a plain identifier is never turned into a path.
    """
    if _SESSION_ID.fullmatch(session_id) is None:
        return None
    name = f"{session_id}.jsonl"
    if cwd:
        candidate = projects_dir / project_slug(cwd) / name
        if fs.exists(candidate):
            return candidate
    try:
        directories = fs.listdir(projects_dir)
    except OSError:
        return None
    for directory in directories:
        candidate = projects_dir / directory / name
        if fs.exists(candidate):
            return candidate
    return None


@dataclass
class TailUpdate:
    """What one poll of a tail found."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    """Objects completed since the previous poll, oldest first."""
    reset: bool = False
    """The file was replaced or shrank: ``entries`` start from byte 0 and earlier state is void."""
    exists: bool = True
    more: bool = False
    """The read limit was reached and the file has more to read."""


class TranscriptTail:
    """Reads the complete lines appended to one file since the last poll."""

    def __init__(
        self,
        fs: FileSystem,
        path: Path,
        *,
        max_read: int = DEFAULT_MAX_READ,
        max_line: int = DEFAULT_MAX_LINE,
    ) -> None:
        self._fs = fs
        self.path = path
        self._max_read = max_read
        self._max_line = max_line
        self.offset = 0
        """Bytes consumed so far, including a carried torn line."""
        self._ino = 0
        self._head = b""
        self._carry = b""
        self._skipping = False

    @property
    def carried(self) -> int:
        """Bytes of a torn last line waiting for its end."""
        return len(self._carry)

    def _read(self, offset: int, length: int) -> bytes:
        if isinstance(self._fs, RangeReader):
            return self._fs.read_range(self.path, offset, length)
        return self._fs.read_bytes(self.path)[offset : offset + length]

    def _restart(self) -> None:
        self.offset = 0
        self._head = b""
        self._carry = b""
        self._skipping = False

    def poll(self) -> TailUpdate:
        """Read what was appended. A missing file is ``exists=False`` and resets the tail."""
        update = TailUpdate()
        try:
            info = self._fs.stat(self.path)
            update.reset = self._replaced(info.size, info.ino)
            if update.reset:
                self._restart()
            self._ino = info.ino
            if info.size == self.offset:
                return update
            data = self._read(self.offset, min(self._max_read, info.size - self.offset))
        except FileNotFoundError:
            was_known = self.offset > 0 or self._ino != 0
            self._restart()
            self._ino = 0
            return TailUpdate(reset=was_known, exists=False)
        if self.offset == 0:
            self._head = data[:HEAD_BYTES]
        self.offset += len(data)
        update.more = self.offset < info.size
        self._split(data, update.entries)
        return update

    def _replaced(self, size: int, ino: int) -> bool:
        """Did the file shrink, change inode, or change its first bytes since they were read?"""
        if self.offset == 0:
            return False
        if size < self.offset or (ino != 0 and self._ino != 0 and ino != self._ino):
            return True
        # A rewrite in place that grew the file: the first bytes no longer match.
        return size > self.offset and self._read(0, len(self._head)) != self._head

    def _split(self, data: bytes, out: list[dict[str, Any]]) -> None:
        buffer = self._carry + data
        self._carry = b""
        *lines, rest = buffer.split(b"\n")
        for line in lines:
            if self._skipping:
                self._skipping = False  # the end of an over-long line
                continue
            self._add(line, out)
        if self._skipping:
            return  # still inside an over-long line: drop it as it arrives
        if len(rest) > self._max_line:
            self._skipping = True
            return
        entry = decode_line(rest) if rest.strip() else None
        if entry is not None:
            out.append(entry)  # a last line without a newline that is already whole
            self._carry = b""
            return
        self._carry = rest

    def _add(self, line: bytes, out: list[dict[str, Any]]) -> None:
        if len(line) > self._max_line or not line.strip():
            return
        entry = decode_line(line)
        if entry is not None:
            out.append(entry)


class SessionTranscript:
    """The tail of one session's transcript and the facts folded from it."""

    def __init__(self, tail: TranscriptTail) -> None:
        self.tail = tail
        self._facts = FactsAccumulator()
        self.present = True
        """The file existed at the last :meth:`update`."""
        self.backlog = False
        """The file has bytes the last :meth:`update` did not read."""

    @property
    def path(self) -> Path:
        return self.tail.path

    def update(self) -> bool:
        """Poll the file once and fold in new lines. Returns whether the facts may have changed.

        One poll reads at most the tail's ``max_read`` bytes; :attr:`backlog` says whether the
        file has more, which the next call reads.
        """
        update = self.tail.poll()
        self.backlog = update.more
        self.present = update.exists
        changed = False
        if update.reset:
            self._facts = FactsAccumulator()
            changed = True
        if update.entries:
            self._facts.feed_all(update.entries)
            changed = True
        return changed

    def facts(self) -> dict[str, Any]:
        return self._facts.facts()

    def pending_tool(self) -> PendingTool | None:
        return self._facts.pending_tool()
