"""The OpenCode collector (``PLAN-final-plan.md`` WP3.2, §4.6; ``PLAN.md`` §4.5).

Every ``poll_interval_seconds`` (2 s) the collector lists the running OpenCode processes and emits
their sessions as **complete** snapshots (sessions and their activity), so a session whose process
ended is closed in the store. The session key is ``opencode:<session id>``.

**One poll**

1. ``pgrep -x opencode`` lists this user's OpenCode processes.
2. ``lsof -nP -a -p <pids> -d ^txt,^mem -Fpfn`` gives each process's working directory.
3. ``opencode.db`` (read-only, ``mode=ro``) gives the top-level, not archived sessions of those
   directories; the newest ``n`` go to the ``n`` processes of a directory
   (:func:`~fylgja.collectors.opencode.parse.assign_sessions`).
4. Per session: the newest messages give busy or idle (an incomplete assistant message is a turn in
   flight), the tool parts of that message give the running tool, and the ``todo`` table gives the
   session's own task list (``facts.tasks``, with the ``in_progress`` row as the open task).
5. One ``ps -A -o pid=,ppid=,lstart=,comm=`` call runs when a live process is new; it gives the
   process key and the ancestor chain the resolver uses to find the host app.

A process whose directory has no session row (a fresh TUI that has not stored anything yet) is not
listed: there is no session id to key on.

**Task lists (WP4.3).** The ``todo`` rows of each listed session are also emitted as one complete
TODOS snapshot (:func:`~fylgja.collectors.opencode.parse.todo_snapshot`) whenever they differ from
the list last emitted for that session, scoped to the session. When a session is no longer listed
(its process ended), its list is gone with it: a complete, empty snapshot is emitted for it once.
A poll that fails emits no list at all, so no stored task is touched by a failed read.

**The optional TypeScript plugin.** ``fylgja.ts`` (package data; copied by hand to
``~/.config/opencode/plugins/``) writes ``fylgja.hook/1`` envelopes for ``session.status``,
``permission.asked`` and ``todo.updated`` into ``hook-inbox/opencode/``. This collector drains that
inbox with the Claude collector's :class:`~fylgja.collectors.claude.inbox.InboxDrainer` (one
implementation, delete-after-commit, quarantine) and treats every committed envelope as a wake-up:
the database stays the source of truth for state, and the poll runs at once instead of after the
interval. Without the plugin the inbox simply stays empty and the state is at most one poll old.
The events are stored as ``hook_event`` records; they do not drive the session state machine.

**Failures.** A missing ``pgrep`` or ``lsof``, a command that times out, a missing, unreadable or
unfamiliar database never crash the collector: the poll logs the first failure, keeps the last
state and retries, and :meth:`OpenCodeCollector.doctor` reports ``degraded``. A database whose
tables lack a column the collector queries is ``schema_mismatch``; the collector does not guess.
Nothing is logged or exposed from titles, commands, task text or paths.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from fylgja.collectors.base import Clock, CollectorContext, RecordSink
from fylgja.collectors.claude.inbox import InboxDrainer
from fylgja.collectors.codex import parse as process_parse
from fylgja.collectors.opencode import parse
from fylgja.collectors.opencode.adapter import (
    CommandFailure,
    OpenCodeDb,
    OpenCodeDbError,
    Seams,
    list_open_files,
    list_pids,
    read_process_table,
)
from fylgja.models import (
    HealthReport,
    HealthState,
    ProcessRef,
    Record,
    SessionActivity,
    SessionPayload,
)

__all__ = ["OpenCodeCollector", "OpenCodeSettings", "PollStats"]

INBOX_SOURCE: Final = "opencode"

_Listed = tuple[str, SessionPayload, SessionActivity]
_Failure = tuple[str, str]
_Lists = dict[str, tuple[parse.TodoRow, ...]]


class OpenCodeSettings(BaseModel):
    """``[collectors.opencode]``."""

    model_config = ConfigDict(extra="forbid")

    db_path: Path | None = None
    """``opencode.db``; default ``$XDG_DATA_HOME/opencode/opencode.db`` or
    ``~/.local/share/opencode/opencode.db``."""
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    heartbeat_seconds: float = Field(default=30.0, gt=0)
    process_name: str = Field(default="opencode", min_length=1)
    pgrep_command: str = "/usr/bin/pgrep"
    lsof_command: str = "/usr/sbin/lsof"
    ps_command: str = "/bin/ps"
    command_timeout_seconds: float = Field(default=5.0, gt=0)
    busy_stale_seconds: float = Field(default=1800.0, gt=0)
    """An incomplete assistant message with no sign of life for this long is not a turn in flight
    (the database keeps such rows from sessions whose process died)."""
    inbox_poll_seconds: float = Field(default=1.0, gt=0)
    inbox_watch: Literal["auto", "poll"] = "auto"
    """``auto`` watches the inbox with kqueue where it exists; ``poll`` only polls."""


class PollStats:
    """Counts from the last successful poll. Never holds contents."""

    def __init__(self) -> None:
        self.processes = 0
        self.sessions = 0
        self.without_session = 0


class _WakeSink:
    """The sink the inbox drainer commits to: every committed record wakes the poll loop."""

    def __init__(self, inner: RecordSink, wake: asyncio.Event) -> None:
        self._inner = inner
        self._wake = wake

    def emit(self, record: Record) -> None:
        self._inner.emit(record)

    async def emit_committed(self, record: Record) -> None:
        await self._inner.emit_committed(record)
        self._wake.set()


class OpenCodeCollector:
    def __init__(
        self,
        settings: OpenCodeSettings,
        context: CollectorContext,
        *,
        db_path: Path,
        seams: Seams,
    ) -> None:
        self._settings = settings
        self._context = context
        self._seams = seams
        self._db_path = db_path
        self._procs: dict[int, tuple[str, tuple[ProcessRef, ...], float]] = {}
        self._command_failure: _Failure | None = None
        self._db_problem: _Failure | None = None
        self._ps_failure: _Failure | None = None
        self._loop_failures: dict[str, str] = {}
        self._polled = False
        self._last_emitted: list[_Listed] | None = None
        self._last_emit_at = 0.0
        self._task_lists: _Lists = {}
        """The todo list last emitted for each session key."""
        self._wake = asyncio.Event()
        self.stats = PollStats()
        self.emissions = 0
        """Snapshot pairs that reached the sink."""
        self.task_emissions = 0
        """Todo-list snapshots that reached the sink."""
        self.inbox = InboxDrainer(
            context.paths.inbox_dir,
            _WakeSink(context.sink, self._wake),
            context.clock,
            context.logger,
            source=INBOX_SOURCE,
            kind=parse.KIND,
            poll_interval=settings.inbox_poll_seconds,
            use_kqueue=False if settings.inbox_watch == "poll" else None,
        )

    # -- one poll ---------------------------------------------------------------------------

    async def poll(self) -> bool:
        """Poll once. Returns whether sessions were (re)emitted. Failures are logged, not raised."""
        settings = self._settings
        runner = self._seams.runner
        timeout = settings.command_timeout_seconds
        try:
            pids = await list_pids(
                runner, settings.pgrep_command, settings.process_name, timeout=timeout
            )
            files = await list_open_files(runner, settings.lsof_command, pids, timeout=timeout)
        except CommandFailure as failure:
            self._set_command_failure((failure.kind, failure.detail))
            return False
        self._set_command_failure(None)

        live = [pid for pid in pids if pid in files and files[pid].cwd is not None]
        self.stats.processes = len(pids)
        if not live:
            self._set_db_problem(self._db_missing())
            self._polled = True
            self._procs.clear()
            self.stats.sessions = self.stats.without_session = 0
            emitted = self._emit([])
            self._emit_task_lists({})
            return emitted

        await self._refresh_processes(live)
        procs = [
            parse.ProcInfo(pid, cwd, self._procs[pid][2] if pid in self._procs else None)
            for pid in live
            if (cwd := files[pid].cwd) is not None
        ]
        try:
            listed, lists = self._read_sessions(procs)
        except OpenCodeDbError as error:
            self._set_db_problem((error.kind, error.detail))
            return False
        self._set_db_problem(None)
        self.stats.sessions = len(listed)
        self.stats.without_session = len(live) - len(listed)
        self._polled = True
        emitted = self._emit(listed)
        self._emit_task_lists(lists)
        return emitted

    def _db_missing(self) -> _Failure | None:
        try:
            present = self._seams.fs.exists(self._db_path)
        except OSError:
            present = False
        if present:
            return None
        return ("db_missing", "the OpenCode database does not exist")

    def _read_sessions(self, procs: list[parse.ProcInfo]) -> tuple[list[_Listed], _Lists]:
        """The listed sessions of ``procs`` and their todo lists. Raises OpenCodeDbError."""
        if (missing := self._db_missing()) is not None:
            raise OpenCodeDbError(*missing)
        now_ms = int(self._context.clock.now().timestamp() * 1000)
        stale_ms = int(self._settings.busy_stale_seconds * 1000)
        listed: list[_Listed] = []
        lists: _Lists = {}
        with OpenCodeDb.open(self._db_path) as db:
            columns = db.check_schema()
            rows = db.sessions({parse.norm_dir(proc.cwd) for proc in procs}, columns)
            chosen = parse.assign_sessions(procs, rows)
            for pid, row in sorted(chosen.items(), key=lambda item: item[1].id):
                messages = db.messages(row.id)
                open_message = parse.open_turn(messages)
                parts = [] if open_message is None else db.tool_parts(open_message.id)
                turn = parse.turn_facts(
                    messages,
                    now_ms=now_ms,
                    stale_ms=stale_ms,
                    session_updated_ms=row.updated_ms,
                    parts_updated_ms=max((p.updated_ms or 0 for p in parts), default=0),
                )
                tool = parse.current_tool(parts)
                todos = db.todos(row.id)
                proc = self._procs.get(pid)
                payload = parse.session_payload(
                    row,
                    pid=pid,
                    key=None if proc is None else proc[0],
                    ancestry=None if proc is None else proc[1],
                )
                listed.append(
                    (parse.session_key(row.id), payload, parse.activity_of(row, turn, tool, todos))
                )
                lists[parse.session_key(row.id)] = tuple(todos)
        return listed, lists

    async def _refresh_processes(self, pids: list[int]) -> None:
        """Keep key, ancestors and start of each live pid; ``ps`` is asked only for new ones."""
        self._procs = {pid: entry for pid, entry in self._procs.items() if pid in pids}
        if all(pid in self._procs for pid in pids):
            return
        try:
            table = await read_process_table(
                self._seams.runner,
                self._settings.ps_command,
                timeout=self._settings.command_timeout_seconds,
            )
        except CommandFailure as failure:
            self._set_ps_failure((failure.kind, failure.detail))
            return
        self._set_ps_failure(None)
        for pid in pids:
            entry = table.get(pid)
            if entry is not None and pid not in self._procs:
                started = process_parse.local_epoch(entry.started)
                self._procs[pid] = (
                    process_parse.process_key(pid, started),
                    process_parse.ancestor_chain(table, pid),
                    started,
                )

    def _emit(self, listed: list[_Listed]) -> bool:
        clock = self._context.clock
        mono = clock.monotonic()
        unchanged = self._last_emitted == listed
        if unchanged and mono - self._last_emit_at < self._settings.heartbeat_seconds:
            return False
        now = clock.now()
        sink = self._context.sink
        sink.emit(parse.session_snapshot(((key, payload) for key, payload, _ in listed), now))
        sink.emit(parse.activity_snapshot(((key, activity) for key, _, activity in listed), now))
        self._last_emitted = listed
        self._last_emit_at = mono
        self.emissions += 1
        return True

    def _emit_task_lists(self, lists: _Lists) -> int:
        """Emit each todo list that changed, and an empty one for each session no longer listed."""
        ended = sorted(key for key in self._task_lists if key not in lists)
        changed = sorted(key for key, rows in lists.items() if self._task_lists.get(key) != rows)
        if not ended and not changed:
            return 0
        now = self._context.clock.now()
        sink = self._context.sink
        for key in changed:
            sink.emit(parse.todo_snapshot(key, lists[key], now))
            self._task_lists[key] = lists[key]
        for key in ended:
            sink.emit(parse.todo_snapshot(key, (), now))  # the session ended: its list is gone
            del self._task_lists[key]
        self.task_emissions += len(changed) + len(ended)
        return len(changed) + len(ended)

    # -- failures ---------------------------------------------------------------------------

    def _set_command_failure(self, failure: _Failure | None) -> None:
        self._note("process scan", self._command_failure, failure)
        self._command_failure = failure

    def _set_db_problem(self, problem: _Failure | None) -> None:
        self._note("database", self._db_problem, problem)
        self._db_problem = problem

    def _set_ps_failure(self, failure: _Failure | None) -> None:
        self._note("process table", self._ps_failure, failure)
        self._ps_failure = failure

    def _note(self, what: str, before: _Failure | None, after: _Failure | None) -> None:
        """Log a change of state once, never every poll."""
        if after is not None and (before is None or before[0] != after[0]):
            self._context.logger.warning("opencode %s: %s", what, after[0])
        elif after is None and before is not None:
            self._context.logger.info("opencode %s recovered", what)

    # -- loops ------------------------------------------------------------------------------

    async def _wait(self) -> None:
        """Return after the poll interval, or at once when the inbox committed an envelope."""
        clock: Clock = self._context.clock
        sleeper = asyncio.ensure_future(clock.sleep(self._settings.poll_interval_seconds))
        waiter = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for future in (sleeper, waiter):
                future.cancel()
            await asyncio.gather(sleeper, waiter, return_exceptions=True)

    async def _poll_loop(self) -> None:
        while True:
            self._wake.clear()  # an envelope committed during the poll wakes the next wait
            await self.poll()
            await self._wait()

    async def _guarded(self, name: str, loop: Callable[[], Awaitable[None]]) -> None:
        """Run ``loop`` for ever: an unexpected error is logged once per kind and it restarts."""
        while True:
            try:
                await loop()
            except Exception as exc:
                kind = type(exc).__name__
                if self._loop_failures.get(name) != kind:
                    self._context.logger.error("opencode %s loop failed: %s", name, kind)
                self._loop_failures[name] = kind
                await self._context.clock.sleep(self._settings.poll_interval_seconds)
            else:  # pragma: no cover - the loops run until cancelled
                return

    async def run(self) -> None:
        """Poll the database and drain the hook inbox until cancelled."""
        tasks = [
            asyncio.ensure_future(self._guarded(name, loop))
            for name, loop in (("poll", self._poll_loop), ("inbox", self.inbox.run))
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)

    # -- health -----------------------------------------------------------------------------

    async def doctor(self) -> HealthReport:
        """The database exists and has the tables the collector reads. No side effects.

        Nothing is written and no process is started.
        """
        now = self._context.clock.now()
        failure = self._command_failure
        if failure is None:
            failure = self._db_problem
        if failure is None and not self._polled:
            failure = self._db_missing()
        if failure is None:
            failure = self.inbox.failure
        if failure is not None:
            return HealthReport(
                state=HealthState.DEGRADED, detail=failure[1], error_kind=failure[0], checked_at=now
            )
        stats = self.stats
        detail = (
            f"{stats.processes} opencode processes, {stats.sessions} sessions, "
            f"{stats.without_session} without a session row"
        )
        drained = self.inbox.totals
        if drained.handled:
            detail += f"; inbox: {drained.committed} drained, {drained.quarantined} quarantined"
        return HealthReport(state=HealthState.OK, detail=detail, checked_at=now)
