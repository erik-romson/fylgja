"""The Claude collector, registry half (``PLAN-final-plan.md`` WP1.7; ``PLAN.md`` §4.3).

Every ``poll_interval_seconds`` (2 s) the collector lists the registry directory, reparses the
rows whose file changed, asks ``ps`` for the start time of each row's pid and emits the live
sessions as **complete** snapshots (sessions and their activity), so a session that ended is closed
in the store.

- A row is live when its pid is alive and the process start time equals ``procStart``; a stale row
  whose pid was reused is not live.
- Rows whose cwd matches ``ignore_cwd`` (CodexBar's probe sessions and Fylgja's own summarizer) are
  dropped.
- The A11 continuity record: ``process_key`` is the pid plus the process start time, and a session
  that replaces another in the same process (``/clear``) names it as ``predecessor_key``.
- State comes from the registry ``status`` until hook state arrives (WP2.3).
- Each live session carries the chain of its ancestor processes (WP1.8), so the resolver can find
  the host app without I/O. One ``ps -A -o pid=,ppid=,comm=`` call runs in a poll that has a live
  session without a chain (a new session or a new process); the chains are kept until the process
  is gone. A failing call only costs the chains: the sessions are still listed.

A snapshot is emitted when it differs from the last one, and at least every
``heartbeat_seconds`` so ``last_seen`` stays fresh. A missing registry or a failing ``ps`` never
crashes the collector: the loop logs the first failure, keeps the last state and retries, and
:meth:`ClaudeCollector.doctor` reports ``degraded``.

Nothing is logged or exposed from a row's contents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from fylgja import paths
from fylgja.collectors.base import CollectorContext
from fylgja.collectors.claude import parse, transcript
from fylgja.collectors.claude.adapter import PsFailure, Seams, ps_process_table, ps_start_times
from fylgja.collectors.claude.registry import Continuity, PollResult, RegistryPoller
from fylgja.models import HealthReport, HealthState, ProcessRef, SessionActivity, SessionPayload

__all__ = ["ClaudeCollector", "ClaudeSettings", "PollStats", "ignore_list"]

DEFAULT_IGNORE_CWD: Final = ("CodexBar/ClaudeProbe",)

_ListedSession = tuple[str, SessionPayload, SessionActivity]


class ClaudeSettings(BaseModel):
    """``[collectors.claude]``."""

    model_config = ConfigDict(extra="forbid")

    registry_dir: Path | None = None
    """The registry directory; default ``~/.claude/sessions``."""
    projects_dir: Path | None = None
    """Where the transcripts are; default ``~/.claude/projects``."""
    transcript_max_read_bytes: int = Field(default=transcript.DEFAULT_MAX_READ, gt=0)
    """Bytes of one transcript read per poll; a larger backlog is read over several polls."""
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    full_reparse_seconds: float = Field(default=60.0, gt=0)
    heartbeat_seconds: float = Field(default=30.0, gt=0)
    ignore_cwd: list[str] = Field(default_factory=lambda: list(DEFAULT_IGNORE_CWD))
    """Rows whose cwd matches are dropped. The summarizer cwd is always added to this list."""
    ps_command: str = "/bin/ps"
    command_timeout_seconds: float = Field(default=5.0, gt=0)


class PollStats:
    """Counts from the last successful poll. Never holds row contents."""

    def __init__(self) -> None:
        self.files = 0
        self.rows = 0
        self.live = 0
        self.dead = 0
        self.reused = 0
        self.ignored = 0
        self.rejected = 0
        self.versions: tuple[str, ...] = ()
        self.non_row_files = 0


class ClaudeCollector:
    def __init__(
        self,
        settings: ClaudeSettings,
        context: CollectorContext,
        *,
        registry_dir: Path,
        seams: Seams,
        ignore_cwd: tuple[str, ...],
        projects_dir: Path | None = None,
    ) -> None:
        self._settings = settings
        self._context = context
        self._seams = seams
        self._ignore_cwd = ignore_cwd
        self._directory = registry_dir
        self._projects = (
            projects_dir if projects_dir is not None else registry_dir.parent / "projects"
        )
        self._transcripts: dict[str, transcript.SessionTranscript] = {}
        self._poller = RegistryPoller(
            seams.fs, registry_dir, full_reparse_seconds=settings.full_reparse_seconds
        )
        self._continuity = Continuity()
        self._registry_failure: tuple[str, str] | None = None
        self._ps_failure: tuple[str, str] | None = None
        self._chain_failure: tuple[str, str] | None = None
        self._chains: dict[int, tuple[str, tuple[ProcessRef, ...]]] = {}
        self._polled = False
        self._last_emitted: list[_ListedSession] | None = None
        self._last_emit_at = 0.0
        self.stats = PollStats()
        self.emissions = 0
        """Snapshot pairs that reached the sink."""

    # -- one poll ---------------------------------------------------------------------------

    async def poll(self) -> bool:
        """Poll once. Returns whether sessions were (re)emitted. Failures are logged, not raised."""
        clock = self._context.clock
        try:
            result = self._poller.poll(clock.monotonic())
        except OSError as exc:
            self._fail_registry(exc)
            return False
        self._set_registry_failure(None)
        candidates = {
            pid: row
            for pid, row in result.rows.items()
            if not parse.cwd_ignored(row.cwd, self._ignore_cwd)
        }
        try:
            starts = await ps_start_times(
                self._seams.runner,
                self._settings.ps_command,
                candidates.keys(),
                timeout=self._settings.command_timeout_seconds,
            )
        except PsFailure as failure:
            self._set_ps_failure((failure.kind, failure.detail))
            return False
        self._set_ps_failure(None)

        await self._refresh_chains(candidates, starts)
        listed = self._live_sessions(candidates, starts)
        self._update_stats(result, candidates)
        self._polled = True
        return self._emit(listed)

    async def _refresh_chains(
        self, candidates: dict[int, parse.RegistryRow], starts: dict[int, float]
    ) -> None:
        """Keep the ancestor chain of every live process; ask ``ps`` only for new ones."""
        keys = {
            pid: parse.process_key(pid, starts[pid])
            for pid, row in candidates.items()
            if pid in starts and parse.check_liveness(row, starts[pid]) is parse.Liveness.LIVE
        }
        self._chains = {
            pid: entry for pid, entry in self._chains.items() if keys.get(pid) == entry[0]
        }
        missing = [pid for pid in keys if pid not in self._chains]
        if not missing:
            return
        try:
            table = await ps_process_table(
                self._seams.runner,
                self._settings.ps_command,
                timeout=self._settings.command_timeout_seconds,
            )
        except PsFailure as failure:
            self._set_chain_failure((failure.kind, failure.detail))
            return
        self._set_chain_failure(None)
        for pid in missing:
            chain = parse.ancestor_chain(table, pid)
            if chain:
                self._chains[pid] = (keys[pid], chain)

    def _live_sessions(
        self, candidates: dict[int, parse.RegistryRow], starts: dict[int, float]
    ) -> list[_ListedSession]:
        listed: dict[str, _ListedSession] = {}
        process_keys: set[str] = set()
        dead = reused = 0
        for pid in sorted(candidates):
            row = candidates[pid]
            started = starts.get(pid)
            verdict = parse.check_liveness(row, started)
            if verdict is not parse.Liveness.LIVE or started is None:
                if verdict is parse.Liveness.DEAD:
                    dead += 1
                else:
                    reused += 1
                continue
            key = parse.process_key(pid, started)
            process_keys.add(key)
            predecessor = self._continuity.observe(key, row.session_id)
            tail = self._tail(row)
            facts = None if tail is None else tail.facts()
            chain = self._chains.get(pid)
            payload = parse.session_payload(
                row,
                process_key=key,
                predecessor_key=predecessor,
                facts=facts,
                ancestry=None if chain is None else chain[1],
            )
            listed[parse.session_key(row.session_id)] = (
                parse.session_key(row.session_id),
                payload,
                parse.activity_of(row, facts, None if tail is None else tail.pending_tool()),
            )
        live_ids = {item[1].native_id for item in listed.values()}
        self._transcripts = {sid: t for sid, t in self._transcripts.items() if sid in live_ids}
        self._continuity.retain(process_keys, live_ids)
        self.stats.dead = dead
        self.stats.reused = reused
        return list(listed.values())

    def _tail(self, row: parse.RegistryRow) -> transcript.SessionTranscript | None:
        """The session's transcript, brought up to date; ``None`` while it has no file.

        A transcript that cannot be read is treated as absent: the session is still listed, from
        its registry row alone.
        """
        tail = self._transcripts.get(row.session_id)
        try:
            if tail is None:
                path = transcript.locate(self._seams.fs, self._projects, row.session_id, row.cwd)
                if path is None:
                    return None
                tail = transcript.SessionTranscript(
                    transcript.TranscriptTail(
                        self._seams.fs, path, max_read=self._settings.transcript_max_read_bytes
                    )
                )
                self._transcripts[row.session_id] = tail
            tail.update()
            if not tail.present:  # moved or deleted: look for it again at the next poll
                del self._transcripts[row.session_id]
                return None
        except OSError:
            self._transcripts.pop(row.session_id, None)
            return None
        return tail

    def _update_stats(self, result: PollResult, candidates: dict[int, parse.RegistryRow]) -> None:
        stats = self.stats
        stats.files = len(result.rows) + len(result.rejected)
        stats.rows = len(result.rows)
        stats.ignored = len(result.rows) - len(candidates)
        stats.rejected = len(result.rejected)
        stats.live = len(candidates) - stats.dead - stats.reused
        stats.versions = tuple(sorted({row.version for row in result.rows.values() if row.version}))
        stats.non_row_files = result.ignored_names

    def _emit(self, listed: list[_ListedSession]) -> bool:
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

    def _fail_registry(self, exc: OSError) -> None:
        if isinstance(exc, FileNotFoundError | NotADirectoryError):
            failure = ("registry_missing", "the Claude registry directory does not exist")
        elif isinstance(exc, PermissionError):
            failure = ("registry_unreadable", "the Claude registry directory is not readable")
        else:
            failure = ("registry_error", f"cannot list the registry ({type(exc).__name__})")
        self._set_registry_failure(failure)

    def _set_registry_failure(self, failure: tuple[str, str] | None) -> None:
        self._note("registry", self._registry_failure, failure)
        self._registry_failure = failure

    def _set_ps_failure(self, failure: tuple[str, str] | None) -> None:
        self._note("ps", self._ps_failure, failure)
        self._ps_failure = failure

    def _set_chain_failure(self, failure: tuple[str, str] | None) -> None:
        self._note("process table", self._chain_failure, failure)
        self._chain_failure = failure

    def _note(
        self, what: str, before: tuple[str, str] | None, after: tuple[str, str] | None
    ) -> None:
        """Log a change of state once, never every poll."""
        if after is not None and (before is None or before[0] != after[0]):
            self._context.logger.warning("claude %s failed: %s", what, after[0])
        elif after is None and before is not None:
            self._context.logger.info("claude %s recovered", what)

    # -- loop -------------------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            await self.poll()
            await self._context.clock.sleep(self._settings.poll_interval_seconds)

    # -- health -----------------------------------------------------------------------------

    async def doctor(self) -> HealthReport:
        """The directory is readable and ``ps`` answers. No side effects, no key file touched."""
        now = self._context.clock.now()
        failure = self._registry_failure or self._ps_failure
        if failure is None and not self._polled:
            try:
                self._seams.fs.listdir(self._directory)
            except OSError as exc:
                self._fail_registry(exc)
                failure = self._registry_failure
        if failure is not None:
            return HealthReport(
                state=HealthState.DEGRADED, detail=failure[1], error_kind=failure[0], checked_at=now
            )
        stats = self.stats
        if stats.rejected and not stats.rows:
            return HealthReport(
                state=HealthState.DEGRADED,
                detail=f"none of {stats.rejected} registry files is a usable row",
                error_kind="unparseable_rows",
                checked_at=now,
            )
        versions = ", ".join(stats.versions) if stats.versions else "none"
        detail = (
            f"{stats.live} live of {stats.rows} rows; versions {versions}; "
            f"{stats.rejected} rejected, {stats.ignored} ignored by cwd; "
            f"{stats.non_row_files} other files left unopened"
        )
        return HealthReport(state=HealthState.OK, detail=detail, checked_at=now)


def ignore_list(settings: ClaudeSettings) -> tuple[str, ...]:
    """``ignore_cwd`` plus the summarizer cwd, which is always ignored."""
    return (*settings.ignore_cwd, str(paths.summarizer_dir()))
