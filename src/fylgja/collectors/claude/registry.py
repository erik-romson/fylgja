"""Change detection and continuity for the Claude registry (``PLAN-final-plan.md`` WP1.7, A11, A13).

:class:`RegistryPoller` lists the registry directory with the A13 glob, ``stat``s every match and
reads a file again only when its ``(st_ino, st_mtime_ns, st_size)`` changes. Claude rewrites a
row either by atomic replace (a new inode) or in place (the same inode); **the directory mtime is
never a gate**, because an in-place rewrite leaves it unchanged. A full reparse of every row runs
every ``full_reparse_seconds`` as a backstop for a rewrite that keeps all three values.

Only names that match ``^[0-9]+\\.json$`` are ever opened. ``*.key`` files and anything else are
counted and left alone.

:class:`Continuity` links a session to the one it replaced in the same process (A11): a ``/clear``
gives the process a new ``sessionId``, and the new session's predecessor is the old one.

Nothing here logs or exposes file contents.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from fylgja.collectors.claude import parse
from fylgja.collectors.seams import FileSystem

__all__ = ["Continuity", "PollResult", "RegistryPoller", "Signature"]

DEFAULT_FULL_REPARSE_SECONDS: Final = 60.0

Signature = tuple[int, int, int]
"""``(st_ino, st_mtime_ns, st_size)``."""


@dataclass
class _Entry:
    signature: Signature | None
    row: parse.RegistryRow | None


@dataclass(frozen=True)
class PollResult:
    """What one poll saw. ``rows`` holds the current usable row of each pid."""

    rows: Mapping[int, parse.RegistryRow]
    added: frozenset[int]
    """Pids that have a usable row now and had none at the previous poll."""
    removed: frozenset[int]
    """Pids that had a usable row at the previous poll and have none now."""
    changed: frozenset[int]
    """Pids whose file was read again and gave a different row."""
    rejected: Mapping[int, str]
    """Pid to the reason code of each file that is not a usable row right now."""
    reads: int
    """Files opened by this poll."""
    ignored_names: int
    """Entries of the directory that are not registry rows (``*.key`` and the like); not opened."""
    full_reparse: bool


@dataclass
class RegistryPoller:
    fs: FileSystem
    directory: Path
    full_reparse_seconds: float = DEFAULT_FULL_REPARSE_SECONDS
    _entries: dict[int, _Entry] = field(default_factory=dict, init=False)
    _last_full: float | None = field(default=None, init=False)
    total_reads: int = field(default=0, init=False)

    def poll(self, now: float) -> PollResult:
        """List, stat and reparse what changed. ``now`` is monotonic seconds.

        Raises :class:`OSError` when the directory cannot be listed; the caller keeps its last
        result and reports the failure.
        """
        names = self.fs.listdir(self.directory)
        full = self._last_full is None or now - self._last_full >= self.full_reparse_seconds
        files = {pid: name for name in names if (pid := parse.registry_pid(name)) is not None}
        ignored = len(names) - len(files)

        had = {pid for pid, entry in self._entries.items() if entry.row is not None}
        for pid in self._entries.keys() - files.keys():
            del self._entries[pid]

        changed: set[int] = set()
        rejected: dict[int, str] = {}
        reads = 0
        for pid, name in files.items():
            path = self.directory / name
            entry = self._entries.setdefault(pid, _Entry(None, None))
            try:
                info = self.fs.stat(path)
            except FileNotFoundError:  # removed between the listing and the stat
                del self._entries[pid]
                continue
            except OSError:
                rejected[pid] = "unreadable"
                continue
            signature = (info.ino, info.mtime_ns, info.size)
            if not full and entry.row is not None and entry.signature == signature:
                continue
            reads += 1
            outcome = self._read(path, pid)
            if isinstance(outcome, parse.RowRejected):
                rejected[pid] = outcome.reason  # keep the previous row; the next poll retries
                continue
            if outcome != entry.row and entry.row is not None:
                changed.add(pid)
            entry.row = outcome
            entry.signature = signature
        if full:
            self._last_full = now
        self.total_reads += reads
        rows = {pid: entry.row for pid, entry in self._entries.items() if entry.row is not None}
        return PollResult(
            rows=rows,
            added=frozenset(rows.keys() - had),
            removed=frozenset(had - rows.keys()),
            changed=frozenset(changed),
            rejected=rejected,
            reads=reads,
            ignored_names=ignored,
            full_reparse=full,
        )

    def _read(self, path: Path, pid: int) -> parse.RegistryRow | parse.RowRejected:
        try:
            data = self.fs.read_bytes(path)
        except FileNotFoundError:
            return parse.RowRejected("vanished")
        except OSError:
            return parse.RowRejected("unreadable")
        return parse.parse_row(data, file_pid=pid)


class Continuity:
    """Tracks which session each process hosts, to link a rotated session to its predecessor."""

    def __init__(self) -> None:
        self._current: dict[str, str] = {}
        self._seen: dict[str, set[str]] = {}
        self._predecessor: dict[str, str] = {}

    def observe(self, process_key: str, session_id: str) -> str | None:
        """Record that ``process_key`` hosts ``session_id`` and return its predecessor key.

        The predecessor is the session key of the session the process hosted before, set once
        when the id changes. A process that goes back to an earlier id adds no link, so the
        chain never loops.
        """
        current = self._current.get(process_key)
        seen = self._seen.setdefault(process_key, set())
        if current is not None and current != session_id and session_id not in seen:
            self._predecessor[session_id] = parse.session_key(current)
        self._current[process_key] = session_id
        seen.add(session_id)
        return self._predecessor.get(session_id)

    def retain(self, process_keys: Collection[str], session_ids: Collection[str]) -> None:
        """Forget processes and sessions that are no longer live."""
        for key in set(self._current) - set(process_keys):
            del self._current[key]
            self._seen.pop(key, None)
        for session_id in set(self._predecessor) - set(session_ids):
            del self._predecessor[session_id]
