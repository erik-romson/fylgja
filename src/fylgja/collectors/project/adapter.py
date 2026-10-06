"""I/O of the ``project`` collector (``PLAN-final-plan.md`` WP4.3). It never writes anything.

* **Known projects** come from the daemon's own store, read-only (``mode=ro`` and ``query_only``,
  :func:`fylgja.store.db.connect_reader`): the ``projects`` rows, which is what the live view's
  projects are built from. Only a project with a repository root has a repo-root ``TODO.md``; a
  store that does not exist yet knows no projects. A collector has no other view of the live
  view, and the store has one writer: this is a reader like the API's.
* **``TODO.md``** is read through the :class:`~fylgja.collectors.seams.FileSystem` seam: ``stat``,
  read, ``stat`` again. No file (``ENOENT``) is a complete, empty read. A file that changed between
  the two ``stat`` calls, or whose length differs from what was read, is a partial read; a
  directory, a file over the size limit and a permission or other ``OSError`` are failures. Every
  failure raises :class:`ReadFailed` with a short code and never with a path or file content.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from fylgja.collectors.codex.adapter import LocalFileSystem
from fylgja.collectors.seams import FileInfo, FileSystem, ProcessRunner
from fylgja.core.system import SubprocessRunner
from fylgja.store import db, queries

__all__ = [
    "FileSignature",
    "KnownProject",
    "LocalFileSystem",
    "ProjectActivity",
    "ReadFailed",
    "StoreUnreadable",
    "default_fs",
    "default_runner",
    "known_projects",
    "read_activity",
    "read_todo_md",
    "stat_todo_md",
]


@dataclass(frozen=True)
class KnownProject:
    """A project the daemon knows, with a repository root."""

    path: str
    """``projects.path``: the scope of its todos (``/now`` ``projects[]`` is keyed by it)."""
    root: str
    """``projects.repo_root``: where its ``TODO.md`` is."""


@dataclass(frozen=True)
class FileSignature:
    """What ``stat`` says about a file; an equal signature means an unchanged file."""

    size: int
    mtime_ns: int
    ino: int


class StoreUnreadable(Exception):
    """The store exists but cannot be read. ``kind`` is a short code."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class ReadFailed(Exception):
    """A ``TODO.md`` could not be read as a whole. ``kind`` is a short code."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def default_fs() -> FileSystem:
    """The real filesystem."""
    return LocalFileSystem()


def default_runner() -> ProcessRunner:
    """The real process runner, for ``git`` and ``gh``."""
    return SubprocessRunner()


def known_projects(database: Path) -> list[KnownProject]:
    """The projects with a repository root, one per root, ordered by root.

    When several project rows share a root, the one whose path is the root wins, else the first
    path. Raises :class:`StoreUnreadable`.
    """
    try:
        if not database.exists():
            return []
        connection = db.connect_reader(database)
        try:
            rows = queries.load_projects(connection)
        finally:
            connection.close()
    except (OSError, sqlite3.Error, KeyError, ValueError) as exc:
        raise StoreUnreadable(f"store_{type(exc).__name__}") from exc
    chosen: dict[str, KnownProject] = {}
    for row in rows:
        root = row.repo_root
        if not root:
            continue
        current = chosen.get(root)
        if current is None or (row.path == root and current.path != root):
            chosen[root] = KnownProject(path=row.path, root=root)
    return [chosen[root] for root in sorted(chosen)]


def _signature(info: FileInfo) -> FileSignature:
    return FileSignature(size=info.size, mtime_ns=info.mtime_ns, ino=info.ino)


def stat_todo_md(fs: FileSystem, path: str) -> FileSignature | None:
    """The file's signature, ``None`` when there is no file. Raises :class:`ReadFailed`."""
    try:
        info = fs.stat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReadFailed("todo_md_unreadable") from exc
    if info.is_dir:
        raise ReadFailed("todo_md_not_a_file")
    return _signature(info)


def read_todo_md(fs: FileSystem, path: str, before: FileSignature, *, max_bytes: int) -> bytes:
    """The whole file, which had signature ``before``. Raises :class:`ReadFailed`."""
    if before.size > max_bytes:
        raise ReadFailed("todo_md_too_large")
    try:
        data = fs.read_bytes(path)
        after = fs.stat(path)
    except FileNotFoundError as exc:  # removed while it was read
        raise ReadFailed("todo_md_partial_read") from exc
    except IsADirectoryError as exc:
        raise ReadFailed("todo_md_not_a_file") from exc
    except OSError as exc:
        raise ReadFailed("todo_md_unreadable") from exc
    if _signature(after) != before or len(data) != after.size:
        raise ReadFailed("todo_md_partial_read")
    return data


@dataclass(frozen=True)
class ProjectActivity:
    """Which projects the user is working in, from the daemon's store (read-only).

    A project is *active* when a live session is in it or it was focused within the window the
    activity was read for (``PLAN-final-plan.md`` §4.7: the GitHub calls are made only for those).
    """

    live: frozenset[str] = frozenset()
    """``projects.path`` of every project with a live session."""
    last_focus: Mapping[str, datetime] = field(default_factory=dict)
    """When each project was last focused within the window (an open span counts as now)."""
    focused: str | None = None
    """The project of the open focus span."""

    def recency(self, path: str) -> datetime | None:
        """The last time ``path`` was focused within the window, if it was."""
        return self.last_focus.get(path)

    def active(self, path: str) -> bool:
        return path in self.live or path in self.last_focus


def read_activity(database: Path, now: datetime, window_seconds: float) -> ProjectActivity:
    """The live sessions and the focus spans of the last ``window_seconds``.

    A store that does not exist yet has no activity. Raises :class:`StoreUnreadable`.
    """
    try:
        if not database.exists():
            return ProjectActivity()
        connection = db.connect_reader(database)
        try:
            sessions = queries.load_live_sessions(connection)
            spans = queries.load_spans(
                connection,
                since=db.to_db_time(now - timedelta(seconds=window_seconds)),
                until=db.to_db_time(now + timedelta(seconds=1)),
            )
        finally:
            connection.close()
    except (OSError, sqlite3.Error, KeyError, ValueError) as exc:
        raise StoreUnreadable(f"store_{type(exc).__name__}") from exc
    live = frozenset(row.project_path for row in sessions if row.project_path)
    last: dict[str, datetime] = {}
    focused: str | None = None
    for span in spans:  # oldest first
        if not span.project_path:
            continue
        seen = now if span.ended_at is None else span.ended_at
        last[span.project_path] = max(seen, last.get(span.project_path, seen))
        if span.ended_at is None:
            focused = span.project_path
    return ProjectActivity(live=live, last_focus=last, focused=focused)
