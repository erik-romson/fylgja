"""The ``project`` collector: repo-root ``TODO.md`` files as read-only todos (WP4.3).

Every ``poll_interval_seconds`` (5 s) the collector asks the store which projects the daemon knows
(:func:`~fylgja.collectors.project.adapter.known_projects`) and looks at each one's repo-root
``TODO.md``:

* a file whose ``stat`` signature is unchanged since its last good read is not read again;
* otherwise it is read as a whole and parsed (:mod:`~fylgja.collectors.project.parse`), and when
  its tasks differ from what was last emitted for it, **one complete TODOS snapshot** of the file
  is emitted, scoped to the project. The core reconciles it (:mod:`fylgja.core.todo_import`);
* no file (``ENOENT``) is a complete, empty read: its snapshot has no items;
* a parse error, a permission error or a partial read emits **nothing** for that file, so the
  stored list stays as it was, and :meth:`ProjectCollector.doctor` reports ``degraded`` until a
  read of the file succeeds. A store that cannot be read emits nothing at all.

A project the store no longer lists is simply not read any more: absence of a read is not an
empty read. Step 41 adds the project state (git, PR, builds) to this collector.

Nothing is logged or exposed from task text or file paths: failures are short codes and counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from fylgja.collectors.base import CollectorContext
from fylgja.collectors.project import adapter, parse
from fylgja.collectors.seams import FileSystem
from fylgja.models import HealthReport, HealthState

__all__ = ["PollStats", "ProjectCollector", "ProjectSettings"]

STORE_UNREADABLE: Final = "store_unreadable"
POLL_FAILED: Final = "poll_failed"


class ProjectSettings(BaseModel):
    """``[collectors.project]``."""

    model_config = ConfigDict(extra="forbid")

    database: Path | None = None
    """The store the known projects are read from; default the daemon's own (read-only)."""
    poll_interval_seconds: float = Field(default=5.0, gt=0)
    max_todo_md_bytes: int = Field(default=parse.MAX_BYTES, gt=0, le=parse.MAX_BYTES)


@dataclass(frozen=True)
class _FileState:
    """The last good read of one ``TODO.md`` (and what was emitted for it)."""

    signature: adapter.FileSignature | None
    """``None``: there was no file."""
    project: str
    items: tuple[parse.TodoMdItem, ...]


class PollStats:
    """Counts from the last poll. Never holds contents or paths."""

    def __init__(self) -> None:
        self.projects = 0
        self.files = 0
        """Projects whose ``TODO.md`` exists."""
        self.tasks = 0


class ProjectCollector:
    def __init__(
        self,
        settings: ProjectSettings,
        context: CollectorContext,
        *,
        database: Path,
        fs: FileSystem,
    ) -> None:
        self._settings = settings
        self._context = context
        self._database = database
        self._fs = fs
        self._files: dict[str, _FileState] = {}
        self._failures: dict[str, str] = {}
        """Path of each ``TODO.md`` whose last read failed, with the failure code."""
        self._store_failure: str | None = None
        self._loop_failure: str | None = None
        self._polled = False
        self.stats = PollStats()
        self.emissions = 0
        """Snapshots that reached the sink."""

    # -- one poll ---------------------------------------------------------------------------

    def poll(self) -> int:
        """Read every known project's ``TODO.md`` once. Returns the snapshots emitted."""
        try:
            projects = adapter.known_projects(self._database)
        except adapter.StoreUnreadable as error:
            self._set_store_failure(error.kind)
            return 0
        self._set_store_failure(None)
        now = self._context.clock.now()
        emitted = 0
        failures: dict[str, str] = {}
        seen: dict[str, _FileState] = {}
        for project in projects:
            path = parse.todo_md_path(project.root)
            if path in seen or path in failures:
                continue
            last = self._files.get(path)
            try:
                state = self._read(path, project.path, last)
            except (adapter.ReadFailed, parse.TodoMdError) as error:
                failures[path] = error.kind
                if last is not None:
                    seen[path] = last  # the stored list stays as the last good read left it
                continue
            seen[path] = state
            if last is None or (last.project, last.items) != (state.project, state.items):
                self._context.sink.emit(
                    parse.todo_md_snapshot(path, state.project, state.items, now)
                )
                emitted += 1
        self._files = seen
        self._set_failures(failures)
        self._polled = True
        self.stats.projects = len(projects)
        self.stats.files = sum(1 for s in seen.values() if s.signature is not None)
        self.stats.tasks = sum(len(s.items) for s in seen.values())
        self.emissions += emitted
        return emitted

    def _read(self, path: str, project: str, last: _FileState | None) -> _FileState:
        """The file as it is now. Raises :class:`ReadFailed` or :class:`TodoMdError`."""
        signature = adapter.stat_todo_md(self._fs, path)
        if last is not None and last.signature == signature:
            return _FileState(signature, project, last.items)  # unchanged since the last read
        if signature is None:
            return _FileState(None, project, ())
        data = adapter.read_todo_md(
            self._fs, path, signature, max_bytes=self._settings.max_todo_md_bytes
        )
        items = tuple(parse.parse_todo_md(parse.decode(data)))
        return _FileState(signature, project, items)

    # -- failures ---------------------------------------------------------------------------

    def _set_store_failure(self, kind: str | None) -> None:
        if kind is not None and self._store_failure is None:
            self._context.logger.warning("project store read failed: %s", kind)
        elif kind is None and self._store_failure is not None:
            self._context.logger.info("project store read recovered")
        self._store_failure = kind

    def _set_failures(self, failures: dict[str, str]) -> None:
        """Log a change of the set of failure kinds once, never every poll."""
        before = sorted(set(self._failures.values()))
        after = sorted(set(failures.values()))
        if after and after != before:
            self._context.logger.warning(
                "%d TODO.md file(s) could not be read: %s", len(failures), ", ".join(after)
            )
        elif not after and before:
            self._context.logger.info("every TODO.md is readable again")
        self._failures = failures

    # -- loop -------------------------------------------------------------------------------

    async def run(self) -> None:
        """Poll until cancelled. An unexpected error is logged once and the loop goes on."""
        while True:
            try:
                self.poll()
            except Exception as exc:
                kind = type(exc).__name__
                if self._loop_failure != kind:
                    self._context.logger.error("project poll failed: %s", kind)
                self._loop_failure = kind
            else:
                self._loop_failure = None
            await self._context.clock.sleep(self._settings.poll_interval_seconds)

    # -- health -----------------------------------------------------------------------------

    async def doctor(self) -> HealthReport:
        """The store and every known ``TODO.md`` were read. No side effects."""
        now = self._context.clock.now()
        if self._store_failure is not None:
            return HealthReport(
                state=HealthState.DEGRADED,
                detail="the store's projects cannot be read",
                error_kind=STORE_UNREADABLE,
                checked_at=now,
            )
        if self._loop_failure is not None:
            return HealthReport(
                state=HealthState.DEGRADED,
                detail=f"the poll failed ({self._loop_failure})",
                error_kind=POLL_FAILED,
                checked_at=now,
            )
        if self._failures:
            kinds = sorted(set(self._failures.values()))
            return HealthReport(
                state=HealthState.DEGRADED,
                detail=(
                    f"{len(self._failures)} of {self.stats.projects} TODO.md files could not be "
                    f"read as a whole ({', '.join(kinds)}); their todos are kept as they were"
                ),
                error_kind=kinds[0],
                checked_at=now,
            )
        stats = self.stats
        detail = (
            f"{stats.projects} projects; {stats.files} TODO.md files with {stats.tasks} tasks"
            if self._polled
            else "not polled yet"
        )
        return HealthReport(state=HealthState.OK, detail=detail, checked_at=now)
