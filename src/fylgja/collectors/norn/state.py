"""The state of a norn run: events and files reduced to one :class:`Run` (pure).

A run comes from two sources (A10):

* **the plugin** (``plugin=True``): the events of norn's own subscriber, applied one by one with
  :func:`apply_event`. Its liveness is exact: ``running`` while the newest event, a heartbeat at
  worst, is at most ``heartbeat_timeout`` old *and* the run's pid is alive;
* **the files** (``plugin=False``): ``<state>.history``, ``run.json`` and the tail of
  ``events.jsonl``, turned into a run by :func:`run_from_files`. Nothing in them says whether the
  run is alive, so an unfinished run is ``running`` only when a process matches its ``argv`` and
  working directory (:func:`match_processes`), and ``stale`` otherwise.

:func:`status` gives the state shown to the person: ``running``, ``waiting_input``, ``paused``,
``stale`` (the heartbeat stopped, or the process is gone, without a finish event) or one of the
terminal states ``passed``, ``failed`` and ``cancelled``. :func:`merge` lets the plugin's view win
and fills the gaps from the files, for instance the end of a run whose ``RunFinished`` the daemon
missed while it was down.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final

from fylgja.collectors.norn import parse
from fylgja.collectors.norn.parse import (
    EventsTail,
    HistoryRecord,
    NornEvent,
    RunHeader,
    distinct,
)
from fylgja.models import ReportSection, SessionActivity, SessionPayload, Stage

__all__ = [
    "HEARTBEAT_TIMEOUT_SECONDS",
    "Run",
    "ScanProcess",
    "activity_of",
    "apply_event",
    "argv_matches",
    "children_of",
    "listed",
    "match_processes",
    "merge",
    "run_from_files",
    "session_payload",
    "status",
]

HEARTBEAT_TIMEOUT_SECONDS: Final = 90.0
"""A heartbeat this old or younger, with a live pid, is ``running`` (A10)."""

_PROGRESS: Final = frozenset({"StageStarted", "StageFinished", "CallingAgent", "TurnEvent"})
_LIVE_STATES: Final = frozenset({"running", "waiting_input", "paused"})


@dataclass(frozen=True)
class Run:
    """Everything the collector knows about one norn run."""

    key: str
    pipeline: str | None = None
    cwd: str | None = None
    state_key: str | None = None
    pid: int | None = None
    argv: tuple[str, ...] = ()
    provider: str | None = None
    started_at: datetime | None = None
    plugin: bool = False
    """The run has been seen through the plugin's events."""
    alive_at: datetime | None = None
    """The newest plugin event: the run's last proof of life."""
    updated_at: datetime | None = None
    """The newest sign of progress from any source (a history line's time for the files)."""
    last_ns: int = 0
    stage: str | None = None
    stage_index: int | None = None
    stage_total: int | None = None
    stage_names: tuple[str, ...] = ()
    waiting: str | None = None
    paused: bool = False
    outcome: str | None = None
    finished_at: datetime | None = None
    sessions: tuple[str, ...] = ()
    """The agent session ids the run drove, oldest first."""
    matched_pid: int | None = None
    """The process a file-only run was matched to."""
    params_args: str | None = None
    """``params.args`` of ``run.json``: where a ``norn ui`` run names its plan directory."""
    run_dir: str | None = None
    """The run's own directory (``<stem>.runs/<n>``), when the files showed it."""
    state_dir: str | None = None
    """The directory holding the run's ``.history`` file: the root every report read stays below."""

    @property
    def finished(self) -> bool:
        return self.outcome is not None

    @property
    def last_activity(self) -> datetime | None:
        """When the run last showed a sign of life or progress."""
        moments = [m for m in (self.alive_at, self.updated_at, self.started_at) if m is not None]
        return max(moments) if moments else None


# --------------------------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------------------------


def apply_event(run: Run | None, event: NornEvent) -> Run:
    """``run`` after ``event``. An event older than the last one applied is ignored.

    Every event also carries the run's identity, so the first event of any kind creates the run.
    """
    current = run if run is not None else Run(key=event.key)
    if event.received_at_ns < current.last_ns:
        return current
    if current.finished:
        return current  # nothing follows a finish event
    updated = replace(
        current,
        plugin=True,
        alive_at=_later(current.alive_at, event.at),
        updated_at=_later(current.updated_at, event.at),
        last_ns=event.received_at_ns,
        pipeline=event.pipeline or current.pipeline,
        cwd=event.cwd or current.cwd,
        state_key=event.state_key or current.state_key,
        pid=event.pid or current.pid,
        argv=event.argv or current.argv,
        provider=event.provider or current.provider,
    )
    if event.session_id and event.session_id not in updated.sessions:
        updated = replace(updated, sessions=distinct(updated.sessions, [event.session_id]))
    name = event.name
    if name in _PROGRESS or name == "RunResumed":
        updated = replace(updated, waiting=None, paused=False)
    if name == "RunStarted":
        updated = replace(
            updated,
            started_at=event.at,
            stage_total=event.stage_total or updated.stage_total,
        )
    elif name == "StageStarted":
        updated = _stage_started(updated, event)
    elif name == "CallingAgent" and event.stage:
        updated = _stage_started(updated, event)
    elif name == "Heartbeat" and event.stage and updated.stage is None:
        # a run rebuilt from a heartbeat: it names the stage the run is in
        updated = replace(updated, stage=event.stage, stage_index=event.stage_index)
    elif name == "WaitingInput":
        updated = replace(updated, waiting=event.wait_kind or "agent")
    elif name == "RunPaused":
        updated = replace(updated, paused=True)
    elif name == "RunCancelled":
        updated = replace(updated, outcome="cancelled", finished_at=event.at)
    elif name == "RunFinished":
        outcome = "failed" if event.success is False else "passed"
        updated = replace(updated, outcome=outcome, finished_at=event.at)
    return updated


def _later(first: datetime | None, second: datetime) -> datetime:
    return second if first is None or second > first else first


def _stage_started(run: Run, event: NornEvent) -> Run:
    name = event.stage
    if name is None:
        return run
    names = run.stage_names if name in run.stage_names else (*run.stage_names, name)
    index = event.stage_index or (names.index(name) + 1)
    return replace(
        run,
        stage=name,
        stage_names=names[-parse.MAX_SESSIONS :],
        stage_index=index,
        stage_total=event.stage_total or run.stage_total,
    )


# --------------------------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------------------------


def run_from_files(
    key: str,
    record: HistoryRecord,
    header: RunHeader | None,
    tail: EventsTail | None,
    *,
    stem: str,
    run_dir: str | None = None,
    state_dir: str | None = None,
) -> Run:
    """The run the files describe. ``stem`` names the pipeline when ``run.json`` is missing."""
    outcome = _file_outcome(record, header, tail)
    stage = None
    names: tuple[str, ...] = tuple(dict.fromkeys(s.name for s in record.stages))
    if outcome is None:
        stage = tail.stage if tail is not None and tail.stage else None
        if stage is None and record.stages:
            stage = record.stages[-1].name  # the last stage the run reported
        if stage is not None and stage not in names:
            names = (*names, stage)
    index = (names.index(stage) + 1) if stage in names and stage is not None else None
    last = record.stages[-1].name if record.stages else None
    finished_at = None
    if outcome is not None:
        finished_at = (header.finished_at if header is not None else None) or record.timestamp
    return Run(
        key=key,
        pipeline=(header.pipeline if header is not None else None) or stem,
        cwd=header.cwd if header is not None else None,
        state_key=header.state_key if header is not None else None,
        argv=header.argv if header is not None else (),
        provider=(header.provider if header is not None else None) or record.provider,
        started_at=header.started_at if header is not None else None,
        params_args=header.params_args if header is not None else None,
        run_dir=run_dir,
        state_dir=state_dir,
        updated_at=record.timestamp,
        stage=stage if outcome is None else last,
        stage_index=index if outcome is None else (len(names) or None),
        stage_total=len(names) if outcome is not None and names else None,
        stage_names=names,
        waiting=tail.waiting if tail is not None and outcome is None else None,
        paused=tail.paused if tail is not None and outcome is None else False,
        outcome=outcome,
        finished_at=finished_at,
        sessions=distinct(record.session_ids, tail.session_ids if tail is not None else ()),
    )


def _file_outcome(
    record: HistoryRecord, header: RunHeader | None, tail: EventsTail | None
) -> str | None:
    if tail is not None and tail.outcome is not None:
        return tail.outcome
    if header is not None and header.finished_at is not None:
        return "passed" if header.success is not False else "failed"
    if not record.in_progress:
        return "passed" if record.success is True else "failed"
    return None


def merge(plugin_run: Run | None, file_run: Run | None) -> Run | None:
    """One run from the plugin's view and the files' view of the same key.

    The plugin's view wins; the files fill what it lacks: the pipeline, working directory,
    ``argv``, the session ids, and the end of a run whose finish event never arrived.
    """
    if plugin_run is None or file_run is None:
        return plugin_run or file_run
    merged = replace(
        plugin_run,
        pipeline=plugin_run.pipeline or file_run.pipeline,
        cwd=plugin_run.cwd or file_run.cwd,
        state_key=plugin_run.state_key or file_run.state_key,
        argv=plugin_run.argv or file_run.argv,
        provider=plugin_run.provider or file_run.provider,
        started_at=plugin_run.started_at or file_run.started_at,
        params_args=plugin_run.params_args or file_run.params_args,
        run_dir=plugin_run.run_dir or file_run.run_dir,
        state_dir=plugin_run.state_dir or file_run.state_dir,
        sessions=distinct(plugin_run.sessions, file_run.sessions),
    )
    if merged.finished:
        return merged
    if file_run.finished:
        return replace(
            merged,
            outcome=file_run.outcome,
            finished_at=file_run.finished_at,
            stage=file_run.stage or merged.stage,
            stage_total=file_run.stage_total or merged.stage_total,
            waiting=None,
            paused=False,
        )
    if merged.stage is None and file_run.stage is not None:
        merged = replace(merged, stage=file_run.stage, stage_index=file_run.stage_index)
    return merged


# --------------------------------------------------------------------------------------------
# Liveness
# --------------------------------------------------------------------------------------------


def status(
    run: Run,
    *,
    now: datetime,
    pid_alive: Callable[[int], bool],
    heartbeat_timeout: float = HEARTBEAT_TIMEOUT_SECONDS,
) -> str:
    """The state of ``run``: ``running``, ``waiting_input``, ``paused``, ``stale`` or terminal.

    The terminal states are ``passed``, ``failed`` and ``cancelled``.

    A run seen through the plugin is alive while its newest event is at most ``heartbeat_timeout``
    seconds old and its pid is alive (an unknown pid is taken on trust). A run known only from the
    files is alive while the process it was matched to is.
    """
    if run.outcome is not None:
        return run.outcome
    if run.plugin:
        beat = run.alive_at
        if beat is None or (now - beat).total_seconds() > heartbeat_timeout:
            return "stale"
        if run.pid is not None and not pid_alive(run.pid):
            return "stale"
    else:
        if run.matched_pid is None or not pid_alive(run.matched_pid):
            return "stale"
    if run.waiting is not None:
        return "waiting_input"
    return "paused" if run.paused else "running"


def listed(
    run: Run,
    state: str,
    *,
    now: datetime,
    finished_seconds: float,
    max_age_seconds: float,
) -> bool:
    """Whether a run in ``state`` is still shown.

    A run that is alive is always shown. A finished one stays for ``finished_seconds`` after its
    end, a stale one for ``max_age_seconds`` after its last sign of life.
    """
    if state in _LIVE_STATES:
        return True
    moment = run.finished_at if state in parse.OUTCOME_STATES else run.last_activity
    if moment is None:
        return False
    limit = finished_seconds if state in parse.OUTCOME_STATES else max_age_seconds
    return (now - moment).total_seconds() <= limit


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------


def session_payload(run: Run) -> SessionPayload:
    """The ``session`` record of a run: kind ``norn``, native id ``<state>:<run id>``."""
    native_id = run.key.partition(":")[2]
    return SessionPayload(
        kind=parse.KIND,
        native_id=native_id,
        pid=run.pid or run.matched_pid,
        cwd=run.cwd,
        name=run.pipeline,
        started_at=run.started_at,
    )


def activity_of(
    run: Run, state: str, report: Sequence[ReportSection] | None = None
) -> SessionActivity:
    """The ``session_activity`` of a run (§4.6): pipeline, stage, waiting reason and state.

    A finished or stale run has a stage even without a name: the one-liner then reads
    ``<pipeline> <state>``. ``report`` holds the sections of the reports a finished run left.
    """
    waiting = run.waiting if state == "waiting_input" else None
    stage: Stage | None = None
    if run.stage is not None or state in parse.OUTCOME_STATES or state == "stale":
        stage = Stage(name=run.stage, index=run.stage_index, total=run.stage_total)
    return SessionActivity(
        state=state,
        waiting_reason=None if waiting is None else parse.WAIT_REASONS.get(waiting, waiting),
        stage=stage,
        title=run.pipeline,
        report=list(report) if report else None,
    )


def children_of(run: Run) -> tuple[tuple[str, str], ...]:
    """``(child session key, parent key)`` of the agent sessions the run drove (``sdk-py``).

    Only Claude sessions are nested: ids of another agent (Codex, OpenCode) are not in the Claude
    registry. A run that names no provider uses Claude, norn's default.
    """
    provider = (run.provider or "claude-code").lower()
    if not provider.startswith("claude"):
        return ()
    return tuple((parse.claude_session_key(session), run.key) for session in run.sessions)


# --------------------------------------------------------------------------------------------
# Process matching (runs without the plugin)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanProcess:
    """One process of the table: its pid and argument vector, and its directory when known."""

    pid: int
    argv: tuple[str, ...]
    cwd: str | None = None


def argv_matches(run_argv: Sequence[str], process_argv: Sequence[str]) -> bool:
    """Whether a process was started with a run's ``argv``.

    ``run.json`` holds the arguments norn saw (``["…/norn/__main__.py", "ui"]``), while the process
    line starts with the interpreter and, for ``python -m norn``, names the module and not the
    file (``python3 -m norn ui``). The run's arguments must appear in the process line in order:
    the first one is compared by file name, or stands for the module after ``-m`` when it is a
    ``__main__.py``.
    """
    count = len(run_argv)
    if count == 0:
        return False
    head = posixpath.basename(run_argv[0])
    rest = tuple(run_argv[1:])
    for start in range(len(process_argv) - count + 1):
        by_name = posixpath.basename(process_argv[start]) == head
        by_module = head == "__main__.py" and start > 0 and process_argv[start - 1] == "-m"
        if (by_name or by_module) and tuple(process_argv[start + 1 : start + count]) == rest:
            return True
    return False


def _same_dir(left: str, right: str) -> bool:
    def norm(path: str) -> str:
        path = posixpath.normpath(path)
        return path.removeprefix("/private") if path.startswith("/private/") else path

    return norm(left) == norm(right)


def match_processes(runs: Sequence[Run], processes: Sequence[ScanProcess]) -> dict[str, int]:
    """The process of each unfinished run that has one: ``{run key: pid}``.

    The match is by ``argv`` and, when both sides know it, working directory. A process serves
    one run: the newest runs choose first, because one ``norn ui`` process hosts many runs one
    after another and only the newest of them can be running.
    """
    taken: set[int] = set()
    found: dict[str, int] = {}
    ordered = sorted(
        runs,
        key=lambda r: (r.started_at.timestamp() if r.started_at is not None else 0.0, r.key),
        reverse=True,
    )
    for run in ordered:
        if run.finished or not run.argv:
            continue
        for process in processes:
            if process.pid in taken or not argv_matches(run.argv, process.argv):
                continue
            if run.cwd and process.cwd and not _same_dir(run.cwd, process.cwd):
                continue
            taken.add(process.pid)
            found[run.key] = process.pid
            break
    return found
