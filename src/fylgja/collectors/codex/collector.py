"""The Codex collector (``PLAN-final-plan.md`` WP3.1, §4.6; ``PLAN.md`` §4.4).

Every ``poll_interval_seconds`` (2 s) the collector lists the running Codex CLI processes and emits
them as **complete** snapshots (sessions and their activity), so a session whose process ended is
closed in the store. The session key is ``codex:<thread id>``.

**One poll**

1. ``pgrep -x codex`` lists this user's Codex processes.
2. ``lsof -nP -a -p <pids> -d ^txt,^mem -Fpfn`` gives each process's working directory and the
   rollout file it has open (``rollout-<timestamp>-<thread id>.jsonl``): the file name carries the
   thread id. A process with no rollout open (no conversation yet) has no thread and is not listed.
   When several rollouts are open (sub-agent threads run inside the parent process) the thread that
   ``thread_spawn_edges`` does not list as a child wins, else the oldest file.
3. The state database (``~/.codex/state_N.sqlite``, opened read-only through a ``mode=ro`` URI)
   gives the thread's cwd, branch, model, title and name.
4. The tail of the rollout gives busy or idle (``task_started`` newer than ``task_complete`` or
   ``turn_aborted``), the last user message and the current function call
   (:mod:`~fylgja.collectors.codex.parse`). The tail is read again only when the file changed.
5. One ``ps -A -o pid=,ppid=,lstart=,comm=`` call runs when a live process is new; it gives the
   process key and the ancestor chain the resolver uses to find the host app.

**Schema detection.** The newest ``state_N.sqlite`` must be a version in
:data:`~fylgja.collectors.codex.parse.SUPPORTED_STATE_VERSIONS`. An unknown ``N`` makes the health
``degraded`` (``unknown_state_schema``) and the database is not opened: the collector does not guess
a layout. The sessions are still listed, from the process and the rollout alone (no title, branch
or name). A known version whose ``threads`` table lacks a column is ``state_schema_mismatch``, an
unreadable database ``state_db_unreadable``.

**Failures.** A missing ``pgrep`` or ``lsof``, or a command that times out, never crashes the
collector: the poll logs the first failure, keeps the last state and retries, and
:meth:`CodexCollector.doctor` reports ``degraded``. Nothing is logged or exposed from titles,
prompts, commands or paths.

**Optional hooks (off by default; nothing is installed).** Codex 0.154 has stable lifecycle hooks
(``~/.codex/hooks.json`` or ``<repo>/.codex/hooks.json``; events ``SessionStart``,
``UserPromptSubmit``, ``PreToolUse``, ``PermissionRequest``, ``PostToolUse``, ``PreCompact``,
``PostCompact``, ``SubagentStart``, ``SubagentStop`` and ``Stop``) and the legacy ``notify`` command
(``agent-turn-complete``, with the payload as the last argument, not on stdin). A hook command
``fylgja hook --source codex <Event>`` spools the event into ``hook-inbox/codex/``. This collector
does not drain that inbox yet: the poll above is the whole integration. ``docs/collectors/codex.md``
has the configuration to add by hand.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from fylgja.collectors.base import CollectorContext
from fylgja.collectors.codex import parse
from fylgja.collectors.codex.adapter import (
    CommandFailure,
    Seams,
    StateDb,
    StateDbError,
    list_open_files,
    list_pids,
    read_process_table,
    read_rollout_tail,
    real_path,
)
from fylgja.models import HealthReport, HealthState, ProcessRef, SessionActivity, SessionPayload

__all__ = ["CodexCollector", "CodexSettings", "PollStats"]

DEFAULT_TAIL_BYTES: Final = 256 * 1024
DEFAULT_TAIL_MAX_BYTES: Final = 4 * 1024 * 1024
TAIL_GROWTH: Final = 4

_Listed = tuple[str, SessionPayload, SessionActivity]
_Failure = tuple[str, str]


class CodexSettings(BaseModel):
    """``[collectors.codex]``."""

    model_config = ConfigDict(extra="forbid")

    codex_home: Path | None = None
    """The Codex state directory; default ``~/.codex``."""
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    heartbeat_seconds: float = Field(default=30.0, gt=0)
    process_name: str = Field(default="codex", min_length=1)
    pgrep_command: str = "/usr/bin/pgrep"
    lsof_command: str = "/usr/sbin/lsof"
    ps_command: str = "/bin/ps"
    command_timeout_seconds: float = Field(default=5.0, gt=0)
    tail_bytes: int = Field(default=DEFAULT_TAIL_BYTES, gt=0)
    """Bytes read from the end of a rollout; widened up to ``tail_max_bytes`` until a turn event
    shows."""
    tail_max_bytes: int = Field(default=DEFAULT_TAIL_MAX_BYTES, gt=0)


class PollStats:
    """Counts from the last successful poll. Never holds contents."""

    def __init__(self) -> None:
        self.processes = 0
        self.sessions = 0
        self.without_rollout = 0
        self.without_row = 0
        self.unreadable_rollouts = 0
        self.state_db: str | None = None


class CodexCollector:
    def __init__(
        self,
        settings: CodexSettings,
        context: CollectorContext,
        *,
        codex_home: Path,
        seams: Seams,
    ) -> None:
        self._settings = settings
        self._context = context
        self._seams = seams
        self._home = codex_home
        self._procs: dict[int, tuple[str, tuple[ProcessRef, ...]]] = {}
        self._tails: dict[str, tuple[tuple[int, int], parse.RolloutFacts]] = {}
        self._command_failure: _Failure | None = None
        self._state_problem: _Failure | None = None
        self._ps_failure: _Failure | None = None
        self._loop_failure: str | None = None
        self._polled = False
        self._last_emitted: list[_Listed] | None = None
        self._last_emit_at = 0.0
        self.stats = PollStats()
        self.emissions = 0
        """Snapshot pairs that reached the sink."""

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

        choice, problem = self._inspect_state_files()
        picks = self._rollouts_by_pid(files)
        threads, children, problem = self._read_threads(choice, problem, picks)
        self._set_state_problem(problem)

        chosen = self._choose(picks, children)
        await self._refresh_processes(pids)
        listed = self._listed(chosen, files, threads)
        self.stats.processes = len(pids)
        self.stats.sessions = len(listed)
        self.stats.without_rollout = sum(1 for pid in pids if pid not in picks)
        self.stats.state_db = None if choice is None else choice.name
        self._polled = True
        return self._emit(listed)

    def _inspect_state_files(self) -> tuple[parse.StateDbChoice | None, _Failure | None]:
        """The newest state database and what is wrong with the state directory, from names only."""
        try:
            names = self._seams.fs.listdir(self._home)
        except (FileNotFoundError, NotADirectoryError):
            return None, ("codex_home_missing", "the Codex state directory does not exist")
        except PermissionError:
            return None, ("codex_home_unreadable", "the Codex state directory is not readable")
        except OSError as exc:
            return None, (
                "codex_home_error",
                f"cannot list the Codex state directory ({type(exc).__name__})",
            )
        choice = parse.choose_state_db(names)
        if choice is None:
            return None, ("state_db_missing", "no state_N.sqlite in the Codex state directory")
        if not choice.supported:
            known = ", ".join(str(v) for v in sorted(parse.SUPPORTED_STATE_VERSIONS))
            return choice, (
                "unknown_state_schema",
                f"{choice.name} is a schema version this collector does not know (known: {known})",
            )
        return choice, None

    def _rollouts_by_pid(
        self, files: dict[int, parse.OpenFiles]
    ) -> dict[int, list[tuple[str, str]]]:
        """``{pid: [(thread id, rollout path)]}`` for the rollouts inside the Codex directory.

        A rollout outside the directory is never read, whatever the process has open.
        """
        roots = {PurePosixPath(self._home), PurePosixPath(real_path(self._home))}
        picks: dict[int, list[tuple[str, str]]] = {}
        for pid, info in sorted(files.items()):
            found = [
                (thread_id, path)
                for path in sorted(info.rollouts, key=lambda p: PurePosixPath(p).name)
                if (thread_id := parse.rollout_thread_id(path)) is not None
                and any(PurePosixPath(path).is_relative_to(root) for root in roots)
            ]
            if found:
                picks[pid] = found
        return picks

    def _read_threads(
        self,
        choice: parse.StateDbChoice | None,
        problem: _Failure | None,
        picks: dict[int, list[tuple[str, str]]],
    ) -> tuple[dict[str, parse.ThreadRow], set[str], _Failure | None]:
        """The thread rows of the open rollouts, and which of them are sub-agent threads."""
        if choice is None or not choice.supported or not picks:
            return {}, set(), problem
        ids = {thread_id for found in picks.values() for thread_id, _ in found}
        try:
            with StateDb.open(self._home / choice.name) as db:
                raw = db.threads(ids)
                children = db.spawn_children(ids) if len(ids) > 1 else set()
        except StateDbError as error:
            return {}, set(), (error.kind, error.detail)
        rows = {
            thread_id: row
            for thread_id, values in raw.items()
            if (row := parse.thread_row(values)) is not None
        }
        return rows, children, problem

    @staticmethod
    def _choose(
        picks: dict[int, list[tuple[str, str]]], children: set[str]
    ) -> dict[int, tuple[str, str]]:
        """One thread per process, and one process per thread (the lowest pid keeps it)."""
        chosen: dict[int, tuple[str, str]] = {}
        taken: set[str] = set()
        for pid, found in sorted(picks.items()):
            main = [item for item in found if item[0] not in children] or found
            thread_id, path = main[0]
            if thread_id in taken:
                continue
            taken.add(thread_id)
            chosen[pid] = (thread_id, path)
        return chosen

    async def _refresh_processes(self, pids: list[int]) -> None:
        """Keep the process key and ancestors of each live pid; ask ``ps`` only for new ones."""
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
                key = parse.process_key(pid, parse.local_epoch(entry.started))
                self._procs[pid] = (key, parse.ancestor_chain(table, pid))

    def _listed(
        self,
        chosen: dict[int, tuple[str, str]],
        files: dict[int, parse.OpenFiles],
        threads: dict[str, parse.ThreadRow],
    ) -> list[_Listed]:
        stats = self.stats
        stats.without_row = stats.unreadable_rollouts = 0
        listed: list[_Listed] = []
        live_paths: set[str] = set()
        for pid, (thread_id, path) in sorted(chosen.items(), key=lambda item: item[1][0]):
            live_paths.add(path)
            thread = threads.get(thread_id)
            if thread is None:
                stats.without_row += 1
            facts = self._facts(path)
            if facts is None:
                stats.unreadable_rollouts += 1
            proc = self._procs.get(pid)
            payload = parse.session_payload(
                thread_id,
                pid=pid,
                thread=thread,
                facts=facts,
                process_cwd=files[pid].cwd,
                key=None if proc is None else proc[0],
                ancestry=None if proc is None else proc[1],
            )
            listed.append((parse.session_key(thread_id), payload, parse.activity_of(thread, facts)))
        self._tails = {path: tail for path, tail in self._tails.items() if path in live_paths}
        return listed

    def _facts(self, path: str) -> parse.RolloutFacts | None:
        """The rollout's facts; the file is read again only when its size or time changed.

        A rollout that cannot be read is treated as absent: the session is still listed.
        """
        fs = self._seams.fs
        try:
            info = fs.stat(path)
            stamp = (info.size, info.mtime_ns)
            cached = self._tails.get(path)
            if cached is not None and cached[0] == stamp:
                return cached[1]
            limit = self._settings.tail_bytes
            while True:
                tail = read_rollout_tail(fs, path, limit)
                facts = parse.analyze_rollout(tail.lines)
                if (
                    facts.busy is not None
                    or tail.complete
                    or limit >= self._settings.tail_max_bytes
                ):
                    break
                limit = min(limit * TAIL_GROWTH, self._settings.tail_max_bytes)
        except OSError:
            self._tails.pop(path, None)
            return None
        self._tails[path] = ((tail.size, tail.mtime_ns), facts)
        return facts

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

    # -- failures ---------------------------------------------------------------------------

    def _set_command_failure(self, failure: _Failure | None) -> None:
        self._note("process scan", self._command_failure, failure)
        self._command_failure = failure

    def _set_state_problem(self, problem: _Failure | None) -> None:
        self._note("state database", self._state_problem, problem)
        self._state_problem = problem

    def _set_ps_failure(self, failure: _Failure | None) -> None:
        self._note("process table", self._ps_failure, failure)
        self._ps_failure = failure

    def _note(self, what: str, before: _Failure | None, after: _Failure | None) -> None:
        """Log a change of state once, never every poll."""
        if after is not None and (before is None or before[0] != after[0]):
            self._context.logger.warning("codex %s: %s", what, after[0])
        elif after is None and before is not None:
            self._context.logger.info("codex %s recovered", what)

    # -- loop -------------------------------------------------------------------------------

    async def run(self) -> None:
        """Poll until cancelled. An unexpected error is logged once per kind; the loop goes on."""
        while True:
            try:
                await self.poll()
            except Exception as exc:
                kind = type(exc).__name__
                if self._loop_failure != kind:
                    self._context.logger.error("codex poll failed: %s", kind)
                self._loop_failure = kind
            else:
                self._loop_failure = None
            await self._context.clock.sleep(self._settings.poll_interval_seconds)

    # -- health -----------------------------------------------------------------------------

    async def doctor(self) -> HealthReport:
        """The state directory is readable and its newest database is a known version.

        No side effects: nothing is written and no process is started.
        """
        now = self._context.clock.now()
        failure = self._command_failure
        if failure is None and self._loop_failure is not None:
            failure = ("poll_error", f"the poll failed ({self._loop_failure})")
        if failure is None:
            failure = self._state_problem
        if failure is None and not self._polled:
            _, failure = self._inspect_state_files()
        if failure is not None:
            return HealthReport(
                state=HealthState.DEGRADED, detail=failure[1], error_kind=failure[0], checked_at=now
            )
        stats = self.stats
        detail = (
            f"{stats.processes} codex processes, {stats.sessions} sessions; "
            f"database {stats.state_db or 'not read yet'}; "
            f"{stats.without_rollout} without an open rollout, {stats.without_row} without a "
            f"database row, {stats.unreadable_rollouts} unreadable rollouts"
        )
        return HealthReport(state=HealthState.OK, detail=detail, checked_at=now)
