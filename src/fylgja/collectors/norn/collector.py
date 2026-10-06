"""The norn collector (``PLAN-final-plan.md`` WP3.3(b), A10, §4.6; decision 0037).

norn keeps no pid, lock or heartbeat of its own (R3), so a run's liveness comes from two places:

**The plugin (primary).** norn's ``fylgja`` event subscriber (WP3.3(a), in the norn repository)
writes ``fylgja.hook/1`` envelopes with ``source`` ``norn`` into ``hook-inbox/norn/``: run
lifecycle, stage changes, ``WaitingInput``, agent session ids and a heartbeat with the pid every
30 s. The inbox machinery is the Claude collector's
:class:`~fylgja.collectors.claude.inbox.InboxDrainer` (one implementation, delete-after-commit,
quarantine), in its *consumer* mode: the events are run
state, not hook events, so the drainer hands each validated envelope to :meth:`_consume` and
removes the file afterwards. A run is ``running`` while its newest event is at most 90 s old and its
pid is alive, ``waiting_input`` after ``WaitingInput``, in a terminal state after its finish event,
and ``stale`` when the heartbeat stops (or the pid is gone) without one.

**The files (fallback and backfill).** Every ``scan_interval_seconds`` the collector walks the
configured ``roots`` (to ``max_depth``) and the directories of runs the plugin reported, for norn's
``*.history`` files modified within ``max_age_seconds``. The last line of each run id wins; the
sibling ``*.runs/<n>/run.json`` and the tail of ``events.jsonl`` add the header and where the run
is. A run the plugin never reported is checked against the process table by ``argv`` and working
directory; an unfinished run with no match is ``stale``.

**Records.** Complete ``session`` and ``session_activity`` snapshots of the listed runs, key
``norn:<state>:<run id>`` (:func:`~fylgja.collectors.norn.parse.run_key`), so a run that is no
longer listed is closed in the store. A finished run stays for ``finished_seconds``, a stale one for
``max_age_seconds``. The agent sessions a run drove are linked to it with one ``session`` event each
(``parent_key``), so ``/now`` shows them under the run and not as independent sessions.

**Failures.** A missing ``ps``/``lsof``, an unreadable state directory or inbox never crash the
collector: it logs the kind once, keeps the last state and reports ``degraded``. Prompts, outputs
and paths are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from fylgja.collectors.base import Clock, CollectorContext
from fylgja.collectors.claude.inbox import InboxDrainer
from fylgja.collectors.claude.normalize import Envelope
from fylgja.collectors.norn import parse, state
from fylgja.collectors.norn.adapter import (
    CommandFailure,
    Seams,
    StateFile,
    find_state_files,
    list_candidates,
    read_events_tail,
    read_history,
    read_run_header,
    run_dirs,
)
from fylgja.models import (
    HealthReport,
    HealthState,
    SessionActivity,
    SessionEvent,
    SessionPayload,
)

__all__ = ["NornCollector", "NornSettings", "ScanStats"]

INBOX_SOURCE: Final = "norn"
MAX_PLUGIN_RUNS: Final = 256
MAX_SEEN_IDS: Final = 4096
RUNS_PER_FILE: Final = 8
RUN_DIRS_READ: Final = 24

_Listed = tuple[str, SessionPayload, SessionActivity]
_Failure = tuple[str, str]


class NornSettings(BaseModel):
    """``[collectors.norn]``."""

    model_config = ConfigDict(extra="forbid")

    roots: list[str] = Field(default_factory=lambda: ["~/projects"])
    """Directories walked for norn's ``*.history`` files (``~`` is expanded)."""
    max_depth: int = Field(default=3, ge=0, le=8)
    scan_interval_seconds: float = Field(default=30.0, gt=0)
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    heartbeat_seconds: float = Field(default=30.0, gt=0)
    """How often an unchanged snapshot is emitted again."""
    heartbeat_timeout_seconds: float = Field(default=90.0, gt=0)
    """A run whose newest plugin event is older than this is ``stale`` (A10)."""
    max_age_seconds: float = Field(default=21600.0, gt=0)
    """Files older than this are not read, and a stale run is listed for this long."""
    finished_seconds: float = Field(default=900.0, ge=0)
    """A finished run stays listed this long."""
    ps_command: str = "/bin/ps"
    lsof_command: str = "/usr/sbin/lsof"
    command_timeout_seconds: float = Field(default=5.0, gt=0)
    inbox_poll_seconds: float = Field(default=1.0, gt=0)
    inbox_watch: Literal["auto", "poll"] = "auto"
    """``auto`` watches the inbox with kqueue where it exists; ``poll`` only polls."""


@dataclass
class ScanStats:
    """Counts from the last scan. Never holds contents."""

    files: int = 0
    file_runs: int = 0
    unreadable: int = 0
    truncated: bool = False
    roots_found: int = 0


@dataclass(frozen=True)
class _FileEntry:
    """What one ``.history`` file gave, kept while the file is unchanged."""

    mtime_ns: int
    size: int
    records: dict[int, parse.HistoryRecord]
    headers: dict[int, tuple[str, parse.RunHeader]]
    """``history_run_id`` to ``(run directory, header)``."""


class NornCollector:
    def __init__(
        self,
        settings: NornSettings,
        context: CollectorContext,
        *,
        seams: Seams,
    ) -> None:
        self._settings = settings
        self._context = context
        self._seams = seams
        self._plugin: dict[str, state.Run] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._files: dict[str, state.Run] = {}
        self._file_cache: dict[str, _FileEntry] = {}
        self._matched: dict[str, int] = {}
        self._linked: dict[str, str] = {}
        self._scan_at: float | None = None
        self._command_failure: _Failure | None = None
        self._scan_failure: _Failure | None = None
        self._loop_failures: dict[str, str] = {}
        self._polled = False
        self._last_emitted: list[_Listed] | None = None
        self._last_emit_at = 0.0
        self._wake = asyncio.Event()
        self.stats = ScanStats()
        self.emissions = 0
        """Snapshot pairs that reached the sink."""
        self.states: dict[str, str] = {}
        """The state of every listed run by key, as of the last poll (never holds contents)."""
        self.inbox = InboxDrainer(
            context.paths.inbox_dir,
            context.sink,
            context.clock,
            context.logger,
            source=INBOX_SOURCE,
            kind=parse.KIND,
            poll_interval=settings.inbox_poll_seconds,
            use_kqueue=False if settings.inbox_watch == "poll" else None,
            consumer=self._consume,
            read_in_thread=False,
        )

    # -- the plugin's events ----------------------------------------------------------------

    def _consume(self, envelope: Envelope) -> None:
        """Apply one envelope to the run it names. Raises ``EnvelopeError`` for a bad one."""
        event = parse.parse_envelope_event(envelope)
        if event.envelope_id in self._seen:
            return  # a replay of a file that survived a crash
        self._seen[event.envelope_id] = None
        while len(self._seen) > MAX_SEEN_IDS:
            self._seen.popitem(last=False)
        self._plugin[event.key] = state.apply_event(self._plugin.get(event.key), event)
        if len(self._plugin) > MAX_PLUGIN_RUNS:
            oldest = min(self._plugin.values(), key=lambda r: r.last_ns)
            del self._plugin[oldest.key]
        self._wake.set()

    # -- the file scan ----------------------------------------------------------------------

    def _roots(self) -> list[str]:
        return [os.path.expanduser(root) for root in self._settings.roots]  # noqa: PTH111

    def _known_directories(self) -> list[str]:
        found: dict[str, None] = {}
        for run in self._plugin.values():
            if run.state_key is not None or run.cwd is not None:
                found[parse.state_dir_and_stem(run.state_key, run.cwd)[0]] = None
        return list(found)

    def _scan_files(self, now: datetime) -> dict[str, state.Run]:
        """The runs the state files describe (no process match yet)."""
        fs = self._seams.fs
        settings = self._settings
        walk = find_state_files(
            fs,
            self._roots(),
            self._known_directories(),
            max_depth=settings.max_depth,
            min_mtime=now.timestamp() - settings.max_age_seconds,
        )
        self.stats.files = len(walk.files)
        self.stats.unreadable = walk.unreadable
        self.stats.truncated = walk.truncated
        self.stats.roots_found = walk.roots_found
        runs: dict[str, state.Run] = {}
        live_paths = set()
        for file in walk.files:
            live_paths.add(file.path)
            try:
                entry = self._entry(file)
            except OSError:
                self.stats.unreadable += 1
                continue
            runs.update(self._runs_of(file, entry, now))
        self._file_cache = {p: e for p, e in self._file_cache.items() if p in live_paths}
        self.stats.file_runs = len(runs)
        return runs

    def _entry(self, file: StateFile) -> _FileEntry:
        cached = self._file_cache.get(file.path)
        if cached is not None and (cached.mtime_ns, cached.size) == (file.mtime_ns, file.size):
            return cached
        fs = self._seams.fs
        records = read_history(fs, file)
        headers: dict[int, tuple[str, parse.RunHeader]] = {}
        for _, directory in run_dirs(fs, file, newest=RUN_DIRS_READ):
            header = read_run_header(fs, directory)
            if header is not None and header.history_run_id is not None:
                headers.setdefault(header.history_run_id, (directory, header))
        entry = _FileEntry(file.mtime_ns, file.size, records, headers)
        self._file_cache[file.path] = entry
        return entry

    def _runs_of(self, file: StateFile, entry: _FileEntry, now: datetime) -> dict[str, state.Run]:
        horizon = now.timestamp() - self._settings.max_age_seconds
        recent = [
            r
            for r in entry.records.values()
            if r.timestamp is None or r.timestamp.timestamp() >= horizon
        ]
        runs: dict[str, state.Run] = {}
        state_key = f"{file.directory}/{file.stem}"
        for record in sorted(recent, key=lambda r: r.run_id, reverse=True)[:RUNS_PER_FILE]:
            key = parse.run_key(state_key, None, record.run_id, None)
            if key is None:  # pragma: no cover - a history id always names a run
                continue
            known = entry.headers.get(record.run_id)
            header = None if known is None else known[1]
            tail = None
            if record.in_progress and known is not None:
                tail = read_events_tail(self._seams.fs, known[0])
            runs[key] = state.run_from_files(key, record, header, tail, stem=file.stem)
        return runs

    async def _match(self) -> None:
        """Match the unfinished runs the plugin never reported to running processes."""
        pending = [
            run
            for key, run in self._files.items()
            if not run.finished and key not in self._plugin and run.argv
        ]
        if not pending:
            self._matched = {}
            self._set_command_failure(None)
            return
        try:
            processes = await list_candidates(
                self._seams.runner,
                ps=self._settings.ps_command,
                lsof=self._settings.lsof_command,
                wanted=[run.argv for run in pending],
                timeout=self._settings.command_timeout_seconds,
            )
        except CommandFailure as failure:
            self._set_command_failure((failure.kind, failure.detail))
            return  # keep the last matches: a failed command does not say a run is gone
        self._set_command_failure(None)
        self._matched = state.match_processes(pending, processes)

    async def scan(self) -> None:
        """Read the state files and match unreported unfinished runs to processes."""
        now = self._context.clock.now()
        try:
            self._files = self._scan_files(now)
        except OSError as exc:  # pragma: no cover - the walk handles the expected errors
            self._set_scan_failure(("scan_failed", f"cannot scan ({type(exc).__name__})"))
            return
        roots = self._roots()
        if roots and self.stats.roots_found == 0 and not self._known_directories():
            self._set_scan_failure(("roots_missing", "no scan root exists"))
        elif self.stats.unreadable:
            self._set_scan_failure(("state_unreadable", "a state directory cannot be read"))
        else:
            self._set_scan_failure(None)
        await self._match()
        self._scan_at = self._context.clock.monotonic()

    # -- one poll ---------------------------------------------------------------------------

    async def poll(self) -> bool:
        """Scan when due, evaluate every run and emit what changed. Returns whether it emitted."""
        clock = self._context.clock
        if self._scan_at is None or (
            clock.monotonic() - self._scan_at >= self._settings.scan_interval_seconds
        ):
            await self.scan()
        listed = self._evaluate(clock.now())
        self._polled = True
        return self._emit(listed)

    def _evaluate(self, now: datetime) -> list[_Listed]:
        settings = self._settings
        alive = self._seams.pid_alive
        listed: list[_Listed] = []
        states: dict[str, str] = {}
        runs: dict[str, state.Run] = {}
        for key in sorted(self._plugin.keys() | self._files.keys()):
            run = state.merge(self._plugin.get(key), self._files.get(key))
            if run is None:  # pragma: no cover - the key comes from one of the two
                continue
            if not run.plugin and key in self._matched:
                run = replace(run, matched_pid=self._matched[key])
            current = state.status(
                run,
                now=now,
                pid_alive=alive,
                heartbeat_timeout=settings.heartbeat_timeout_seconds,
            )
            if not state.listed(
                run,
                current,
                now=now,
                finished_seconds=settings.finished_seconds,
                max_age_seconds=settings.max_age_seconds,
            ):
                continue
            runs[key] = run
            states[key] = current
            listed.append((key, state.session_payload(run), state.activity_of(run, current)))
        self.states = states
        self._prune(now)
        self._link(runs, states, now)
        return listed

    def _prune(self, now: datetime) -> None:
        """Forget plugin runs that are over and no longer listed."""
        limit = max(self._settings.finished_seconds, self._settings.max_age_seconds)
        for key, run in list(self._plugin.items()):
            moment = run.last_activity
            if key not in self.states and moment is not None:
                if (now - moment).total_seconds() > limit:
                    del self._plugin[key]

    def _link(self, runs: dict[str, state.Run], states: dict[str, str], now: datetime) -> None:
        """Emit one ``session`` event per agent session a live run drove (``parent_key``)."""
        self._linked = {child: parent for child, parent in self._linked.items() if parent in runs}
        for key, run in runs.items():
            if states[key] in parse.OUTCOME_STATES:
                continue
            for child, parent in state.children_of(run):
                if self._linked.get(child) == parent:
                    continue
                self._linked[child] = parent
                self._context.sink.emit(
                    SessionEvent(
                        source=parse.SOURCE,
                        entity_id=child,
                        observed_at=now,
                        payload=SessionPayload(
                            kind=child.partition(":")[0],
                            native_id=child.partition(":")[2],
                            parent_key=parent,
                        ),
                    )
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

    # -- failures ---------------------------------------------------------------------------

    def _set_command_failure(self, failure: _Failure | None) -> None:
        self._note("process scan", self._command_failure, failure)
        self._command_failure = failure

    def _set_scan_failure(self, failure: _Failure | None) -> None:
        self._note("state scan", self._scan_failure, failure)
        self._scan_failure = failure

    def _note(self, what: str, before: _Failure | None, after: _Failure | None) -> None:
        """Log a change of state once, never every poll."""
        if after is not None and (before is None or before[0] != after[0]):
            self._context.logger.warning("norn %s: %s", what, after[0])
        elif after is None and before is not None:
            self._context.logger.info("norn %s recovered", what)

    # -- loops ------------------------------------------------------------------------------

    async def _wait(self) -> None:
        """Return after the poll interval, or at once when the inbox delivered an envelope."""
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
            self._wake.clear()  # an envelope applied during the poll wakes the next wait
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
                    self._context.logger.error("norn %s loop failed: %s", name, kind)
                self._loop_failures[name] = kind
                await self._context.clock.sleep(self._settings.poll_interval_seconds)
            else:  # pragma: no cover - the loops run until cancelled
                return

    async def run(self) -> None:
        """Drain the plugin's inbox and poll the files until cancelled."""
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
        """Whether the roots, the process table and the inbox can be read. No side effects."""
        now = self._context.clock.now()
        failure = self._scan_failure or self._command_failure or self.inbox.failure
        if failure is not None:
            return HealthReport(
                state=HealthState.DEGRADED, detail=failure[1], error_kind=failure[0], checked_at=now
            )
        counts: dict[str, int] = {}
        for value in self.states.values():
            counts[value] = counts.get(value, 0) + 1
        summary = ", ".join(f"{n} {name}" for name, n in sorted(counts.items())) or "no runs"
        detail = f"{summary}; {self.stats.files} state files"
        drained = self.inbox.totals
        if drained.handled:
            detail += f"; inbox: {drained.committed} drained, {drained.quarantined} quarantined"
        return HealthReport(state=HealthState.OK, detail=detail, checked_at=now)
