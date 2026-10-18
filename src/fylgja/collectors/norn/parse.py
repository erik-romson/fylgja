"""Pure parsers of the norn collector (``PLAN-final-plan.md`` WP3.3(b), A10, §4.6).

Everything here is a function over captured input: the payload of a ``fylgja.hook/1`` envelope
that norn's own subscriber plugin writes, the lines of a ``<state>.history`` file, a ``run.json``
and the tail of an ``events.jsonl``. None of it does I/O, none of it keeps prompts or outputs, and
every parser tolerates keys it does not know: norn's files are unversioned (R3).

**The envelope payload** (``docs/collectors/norn.md``). ``payload_text`` is a JSON object. Every
event carries the run's identity (``pipeline``, ``state_key``, ``history_run_id``, ``run_id`` (the
uuid of norn's event keys), ``pid``, ``cwd``), so a collector that restarted mid-run rebuilds the
run from the next heartbeat. The event is ``event`` (or the envelope's ``event_arg``); the rest
depends on it: ``argv``, ``provider`` and ``stage_total`` (``RunStarted``), ``stage``,
``stage_index``, ``attempt``, ``status`` and ``success`` (stage events), ``session_id``
(``CallingAgent`` and ``TurnEvent``), ``kind`` (``WaitingInput``) and ``success`` (``RunFinished``).

**The session key** is ``norn:<state>:<run id>``. ``<state>`` is the state key's file stem and six
hex digits of the hash of its directory (``plan_with_review@a1b2c3``), because two projects can
both have a ``plan_with_review`` whose history counter starts at 1, and a key with a slash would
not fit ``/sessions/{key}``. ``<run id>`` is the history run id, or ``u`` and the first eight
characters of the run's uuid when norn kept no history.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol, TypeGuard

from fylgja.collectors.claude.normalize import Envelope, EnvelopeError, datetime_of_ns
from fylgja.models import (
    ReportSection,
    SessionActivity,
    SessionActivitySnapshot,
    SessionPayload,
    SessionSnapshot,
    SnapshotItem,
)

__all__ = [
    "EVENT_NAMES",
    "KIND",
    "OUTCOME_STATES",
    "SNAPSHOT_SCOPE",
    "SOURCE",
    "WAIT_REASONS",
    "EventsTail",
    "HistoryRecord",
    "NornEvent",
    "RunHeader",
    "StageEntry",
    "activity_snapshot",
    "assemble",
    "claude_session_key",
    "cut_report",
    "events_of_lines",
    "parse_envelope_event",
    "parse_events_tail",
    "parse_history_tail",
    "parse_run_json",
    "plan_dir",
    "report_path",
    "run_key",
    "session_snapshot",
    "state_dir_and_stem",
]

SOURCE: Final = "norn"
KIND: Final = "norn"
SNAPSHOT_SCOPE: Final = "all"

EVENT_NAMES: Final = frozenset(
    {
        "RunStarted",
        "StageStarted",
        "StageFinished",
        "CallingAgent",
        "TurnEvent",
        "WaitingInput",
        "RunPaused",
        "RunResumed",
        "RunCancelled",
        "RunFinished",
        "Heartbeat",
    }
)
"""The events the subscriber plugin writes. Another name is accepted and only proves life."""

OUTCOME_STATES: Final = ("passed", "failed", "cancelled")
"""The terminal run states (the ``/now`` vocabulary: a successful run is ``passed``)."""

WAIT_REASONS: Final = {
    "agent": "agent question",
    "budget": "budget exceeded",
    "step": "step gate",
    "failure_recovery": "failure recovery",
    "timeout_recovery": "timeout recovery",
}
"""What ``WaitingInput.kind`` says to the person, for the one-liner."""

STAGE_STATUSES: Final = frozenset(
    {"passed", "failed", "skipped", "skipped_condition", "cached", "cancelled"}
)

MAX_TEXT: Final = 200
MAX_ARGV: Final = 64
MAX_SESSIONS: Final = 64
MAX_PARAMS_ARGS: Final = 4096
_SESSION_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_RUN_UUID: Final = re.compile(r"[0-9a-fA-F-]{8,64}")
_NEWLINES: Final = re.compile(r"[\r\n]+")


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value: object, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _NEWLINES.sub(" ", value).strip()
    return cleaned[:limit] or None


def _count(value: object) -> int | None:
    return value if _is_int(value) and 0 <= value <= 10**6 else None


def _session_id(value: object) -> str | None:
    return value if isinstance(value, str) and _SESSION_ID.fullmatch(value) else None


def _argv(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items = [_text(item, 512) for item in value[:MAX_ARGV]]
    return tuple(item for item in items if item is not None)


def _instant(value: object) -> datetime | None:
    """An ISO-8601 time as an aware UTC datetime (a naive one is taken as UTC)."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _loads(data: bytes | str) -> Any:
    try:
        return json.loads(data)
    except (ValueError, RecursionError):
        return None


# --------------------------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------------------------


def state_dir_and_stem(state_key: str | None, cwd: str | None) -> tuple[str, str]:
    """``(directory, stem)`` of a run's state files.

    norn writes ``<state key minus its suffix>.history`` and ``.runs/``. The state key is a path,
    absolute or relative to the run's working directory (``run.json`` has both forms).
    """
    path = state_key or "run"
    if not posixpath.isabs(path) and cwd:
        path = posixpath.join(cwd, path)
    path = posixpath.normpath(path)
    directory, name = posixpath.split(path)
    stem, _ = posixpath.splitext(name)
    return directory, stem or name or "run"


def run_key(
    state_key: str | None,
    cwd: str | None,
    history_run_id: int | None,
    run_uuid: str | None,
) -> str | None:
    """The session key of a run, or ``None`` when nothing names the run."""
    if history_run_id is not None and history_run_id >= 0:
        run_id = str(history_run_id)
    elif run_uuid is not None and _RUN_UUID.fullmatch(run_uuid):
        run_id = "u" + run_uuid.replace("-", "")[:8].lower()
    else:
        return None
    directory, stem = state_dir_and_stem(state_key, cwd)
    digest = hashlib.sha1(directory.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
    return f"{KIND}:{stem}@{digest}:{run_id}"


def claude_session_key(session_id: str) -> str:
    """The key of an agent session norn drives (a Claude session, ``sdk-py``)."""
    return f"claude:{session_id}"


# --------------------------------------------------------------------------------------------
# The inbox envelope
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NornEvent:
    """One event of the subscriber plugin, reduced to what the collector keeps."""

    name: str
    at: datetime
    received_at_ns: int
    envelope_id: str
    key: str
    pipeline: str | None = None
    state_key: str | None = None
    cwd: str | None = None
    pid: int | None = None
    history_run_id: int | None = None
    argv: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None
    stage: str | None = None
    stage_index: int | None = None
    stage_total: int | None = None
    attempt: int | None = None
    status: str | None = None
    success: bool | None = None
    session_id: str | None = None
    wait_kind: str | None = None


def parse_envelope_event(envelope: Envelope) -> NornEvent:
    """The event of a ``fylgja.hook/1`` envelope. Raises :class:`EnvelopeError`.

    The message names the rule that failed and never quotes content.
    """
    if envelope.source != SOURCE:
        raise EnvelopeError("source does not match the inbox")
    payload = _loads(envelope.payload_text)
    if not isinstance(payload, dict):
        raise EnvelopeError("payload is not a JSON object")
    name = payload.get("event")
    if not isinstance(name, str) or not name:
        name = envelope.event_arg
    if not name or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}", name):
        raise EnvelopeError("no usable event name")
    history = payload.get("history_run_id")
    run_uuid = payload.get("run_id")
    cwd = _text(payload.get("cwd"), 1024)
    state_key = _text(payload.get("state_key"), 1024)
    key = run_key(
        state_key,
        cwd,
        history if _is_int(history) else None,
        run_uuid if isinstance(run_uuid, str) else None,
    )
    if key is None:
        raise EnvelopeError("no run id")
    pid = payload.get("pid")
    success = payload.get("success")
    stage = _text(payload.get("stage")) or _text(payload.get("stage_name"))
    return NornEvent(
        name=name,
        at=datetime_of_ns(envelope.received_at_ns),
        received_at_ns=envelope.received_at_ns,
        envelope_id=envelope.id,
        key=key,
        pipeline=_text(payload.get("pipeline")),
        state_key=state_key,
        cwd=cwd,
        pid=pid if _is_int(pid) and pid > 0 else None,
        history_run_id=history if _is_int(history) and history >= 0 else None,
        argv=_argv(payload.get("argv")),
        provider=_text(payload.get("provider"), 64),
        model=_text(payload.get("model"), 128),
        stage=stage,
        stage_index=_count(payload.get("stage_index")),
        stage_total=_count(payload.get("stage_total")),
        attempt=_count(payload.get("attempt")),
        status=_text(payload.get("status"), 32),
        success=success if isinstance(success, bool) else None,
        session_id=_session_id(payload.get("session_id")),
        wait_kind=_text(payload.get("kind"), 64),
    )


# --------------------------------------------------------------------------------------------
# Files: .history
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StageEntry:
    """One ``stage_log`` entry of a history line."""

    name: str
    status: str | None = None
    attempt: int | None = None
    session_id: str | None = None
    provider: str | None = None


@dataclass(frozen=True)
class HistoryRecord:
    """The last ``.history`` line of one run id."""

    run_id: int
    timestamp: datetime | None
    success: bool | None
    in_progress: bool
    failed_stage: str | None
    provider: str | None
    session_id: str | None
    stages: tuple[StageEntry, ...]

    @property
    def session_ids(self) -> tuple[str, ...]:
        """Every agent session id of the run, oldest first, without repeats."""
        found: dict[str, None] = {}
        for stage in self.stages:
            if stage.session_id:
                found[stage.session_id] = None
        if self.session_id:
            found.setdefault(self.session_id, None)
        return tuple(found)


def _stage_entry(raw: object) -> StageEntry | None:
    if not isinstance(raw, dict):
        return None
    name = _text(raw.get("name"))
    if name is None:
        return None
    return StageEntry(
        name=name,
        status=_text(raw.get("status"), 32),
        attempt=_count(raw.get("attempt")),
        session_id=_session_id(raw.get("session_id")),
        provider=_text(raw.get("provider"), 64),
    )


def _history_record(raw: object) -> HistoryRecord | None:
    if not isinstance(raw, dict):
        return None
    run_id = raw.get("run_id")
    if not _is_int(run_id) or run_id < 0:
        return None
    success = raw.get("success")
    log = raw.get("stage_log")
    stages = [_stage_entry(item) for item in log] if isinstance(log, list) else []
    return HistoryRecord(
        run_id=run_id,
        timestamp=_instant(raw.get("timestamp")),
        success=success if isinstance(success, bool) else None,
        in_progress=raw.get("in_progress") is True,
        failed_stage=_text(raw.get("failed_stage")),
        provider=_text(raw.get("agent_provider"), 64),
        session_id=_session_id(raw.get("session_id")),
        stages=tuple(s for s in stages if s is not None),
    )


def parse_history_tail(data: bytes, *, complete: bool = False) -> dict[int, HistoryRecord]:
    """The records of a ``.history`` file's tail: the last line of each run id wins.

    ``complete`` says ``data`` starts at the beginning of the file; otherwise its first line may be
    cut and is skipped. A torn last line (a writer in the middle of a line) is skipped too, and a
    line that is not JSON never stops the others.
    """
    lines = data.split(b"\n")
    if not complete:
        lines = lines[1:]
    records: dict[int, HistoryRecord] = {}
    for line in lines:
        if not line.strip():
            continue
        record = _history_record(_loads(line))
        if record is not None:
            records[record.run_id] = record
    return records


# --------------------------------------------------------------------------------------------
# Files: run.json
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunHeader:
    """What ``run.json`` says about a run."""

    pipeline: str | None
    run_id: int | None
    history_run_id: int | None
    state_key: str | None
    provider: str | None
    started_at: datetime | None
    finished_at: datetime | None
    success: bool | None
    failed_stage: str | None
    cwd: str | None
    argv: tuple[str, ...]
    params_args: str | None = None
    """``params.args`` (at most :data:`MAX_PARAMS_ARGS` characters): the plan directory of a run
    launched from ``norn ui`` is in here and not in ``argv``."""


def parse_run_json(text: bytes | str) -> RunHeader | None:
    """The header of a ``run.json``; ``None`` when it is not a JSON object."""
    raw = _loads(text)
    if not isinstance(raw, dict):
        return None
    run_id = raw.get("run_id")
    history = raw.get("history_run_id")
    success = raw.get("success")
    params = raw.get("params")
    return RunHeader(
        pipeline=_text(raw.get("pipeline")),
        run_id=run_id if _is_int(run_id) else None,
        history_run_id=history if _is_int(history) else None,
        state_key=_text(raw.get("state_key"), 1024),
        provider=_text(raw.get("provider"), 64),
        started_at=_instant(raw.get("started_at")),
        finished_at=_instant(raw.get("finished_at")),
        success=success if isinstance(success, bool) else None,
        failed_stage=_text(raw.get("failed_stage")),
        cwd=_text(raw.get("cwd"), 1024),
        argv=_argv(raw.get("argv")),
        params_args=_text(params.get("args"), MAX_PARAMS_ARGS)
        if isinstance(params, dict)
        else None,
    )


# --------------------------------------------------------------------------------------------
# Files: events.jsonl (tail)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EventsTail:
    """What the end of an ``events.jsonl`` says about where the run is."""

    stage: str | None = None
    """The stage that started last and has not finished."""
    waiting: str | None = None
    """The ``WaitingInput`` kind the run is blocked in."""
    paused: bool = False
    outcome: str | None = None
    """``passed``, ``failed`` or ``cancelled`` when the tail holds the run's end."""
    session_ids: tuple[str, ...] = ()


_PROGRESS = frozenset({"StageStarted", "StageFinished", "TurnEvent", "CallingAgent", "RunResumed"})


def events_of_lines(lines: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    """The JSON objects of ``lines``; anything else is skipped."""
    for line in lines:
        if line.strip():
            item = _loads(line)
            if isinstance(item, dict):
                yield item


def parse_events_tail(data: bytes, *, complete: bool = False) -> EventsTail:
    """Summarize the tail of an ``events.jsonl``. The first line is skipped unless ``complete``."""
    lines = data.split(b"\n")
    if not complete:
        lines = lines[1:]
    stage: str | None = None
    waiting: str | None = None
    paused = False
    outcome: str | None = None
    sessions: dict[str, None] = {}
    for item in events_of_lines(lines):
        kind = item.get("type")
        if kind in _PROGRESS:
            waiting = None
            paused = False
        if kind == "StageStarted":
            stage = _text(item.get("name"))
        elif kind == "StageFinished":
            if _text(item.get("name")) == stage:
                stage = None
        elif kind == "WaitingInput":
            waiting = _text(item.get("kind"), 64) or "agent"
        elif kind == "RunPaused":
            paused = True
        elif kind == "RunCancelled":
            outcome = "cancelled"
        elif kind == "RunFinished":
            outcome = "failed" if item.get("success") is False else "passed"
        elif kind == "TurnEvent":
            inner = item.get("event")
            found = _session_id(inner.get("session_id")) if isinstance(inner, dict) else None
            if found:
                sessions[found] = None
    return EventsTail(
        stage=stage,
        waiting=waiting,
        paused=paused,
        outcome=outcome,
        session_ids=tuple(sessions),
    )


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------


def session_snapshot(
    items: Iterable[tuple[str, SessionPayload]], observed_at: datetime
) -> SessionSnapshot:
    """A complete snapshot: a run missing from it is ended in the store."""
    return SessionSnapshot(
        source=SOURCE,
        entity_id=SNAPSHOT_SCOPE,
        observed_at=observed_at,
        items=tuple(SnapshotItem(entity_id=key, payload=payload) for key, payload in items),
        complete=True,
    )


def activity_snapshot(
    items: Iterable[tuple[str, SessionActivity]], observed_at: datetime
) -> SessionActivitySnapshot:
    return SessionActivitySnapshot(
        source=SOURCE,
        entity_id=SNAPSHOT_SCOPE,
        observed_at=observed_at,
        items=tuple(SnapshotItem(entity_id=key, payload=payload) for key, payload in items),
        complete=True,
    )


def distinct(values: Sequence[str], extra: Iterable[str] = ()) -> tuple[str, ...]:
    """``values`` then ``extra`` without repeats, in order, capped at :data:`MAX_SESSIONS`."""
    seen: dict[str, None] = dict.fromkeys(values)
    for value in extra:
        seen.setdefault(value, None)
    return tuple(seen)[:MAX_SESSIONS]


# --------------------------------------------------------------------------------------------
# Run reports (docs/decisions/0057, J21)
# --------------------------------------------------------------------------------------------

PIPELINE_PREFIX: Final = "implement_features"
"""Only these pipelines leave a plan directory with reports."""
MAX_SECTION_BYTES: Final = 4096
"""Every cut section is cut to this many UTF-8 bytes."""
REPORT_DIGEST_MAX_BYTES: Final = 24576
"""All sections of a run together stay under this (the journal's ``RUN_DIGEST_MAX_BYTES``)."""
MAX_REPORT_LINES: Final = 20
_H2: Final = re.compile(r"^##\s+(.*?)\s*$")
_OPEN_ITEM: Final = re.compile(r"^\s*-\s+\[ \]\s+(.*?)\s*$")
_REPORT_PATH: Final = re.compile(r"tmp/[A-Za-z0-9._-]+-run/report\.md")
_DEVIATION: Final = re.compile(r"deviation|judgement|judgment", re.IGNORECASE)
_NOTHING: Final = ("no deviation", "none")
_HANDOFF_SECTIONS: Final = ("for the human", "known limitations", "overview")


class RunSource(Protocol):
    """What :func:`plan_dir` reads: a :class:`RunHeader`, or a run that carries the same fields."""

    @property
    def pipeline(self) -> str | None: ...

    @property
    def cwd(self) -> str | None: ...

    @property
    def argv(self) -> tuple[str, ...]: ...

    @property
    def params_args(self) -> str | None: ...


def plan_dir(source: RunSource, has_index: Callable[[str], bool]) -> str | None:
    """The plan directory of an ``implement_features*`` run, or ``None``.

    The candidates are ``argv[3:]`` for ``norn run <pipeline> <args>`` and the words of
    ``params.args`` otherwise (``norn ui``). The plan directory is the first token that, as is or
    resolved against the run's working directory, is a directory ``has_index`` accepts (one that
    holds ``index.md``).
    """
    if source.pipeline is None or not source.pipeline.startswith(PIPELINE_PREFIX):
        return None
    if len(source.argv) > 1 and source.argv[1] == "run":
        tokens: list[str] = list(source.argv[3:])
    else:
        try:
            tokens = shlex.split(source.params_args or "")
        except ValueError:
            tokens = []
    for token in tokens:
        if not token or token.startswith("-"):
            continue
        candidates = [token]
        if not posixpath.isabs(token) and source.cwd:
            candidates.insert(0, posixpath.join(source.cwd, token))
        for candidate in candidates:
            path = posixpath.normpath(candidate)
            if has_index(path):
                return path
    return None


def report_path(readme_text: str | None, index_text: str | None) -> str | None:
    """The first ``tmp/<name>-run/report.md`` that ``README.md`` names, else ``index.md``."""
    for text in (readme_text, index_text):
        found = _REPORT_PATH.search(text or "")
        if found is not None:
            return found.group(0)
    return None


def _cut_bytes(text: str, limit: int = MAX_SECTION_BYTES) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore").rstrip()


def _h2_sections(text: str) -> list[tuple[str, str]]:
    """``(heading, body)`` of each H2 section; an H3 does not end one."""
    found: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        heading = _H2.match(line)
        if heading is not None:
            found.append((heading.group(1), []))
        elif found:
            found[-1][1].append(line)
    return [(heading, "\n".join(body).strip()) for heading, body in found]


def _named(sections: list[tuple[str, str]], path: str, names: Sequence[str]) -> list[ReportSection]:
    """The sections called ``names`` (case-insensitive), in the order of ``names``, with text."""
    out: list[ReportSection] = []
    for name in names:
        for heading, body in sections:
            if heading.casefold() == name and body:
                out.append(ReportSection(path=path, heading=heading, text=_cut_bytes(body)))
                break
    return out


def _followups(path: str, text: str) -> list[ReportSection]:
    out: list[ReportSection] = []
    heading = "Follow-ups"
    for line in text.split("\n"):
        title = _H2.match(line)
        if title is not None:
            heading = title.group(1) or heading
            continue
        item = _OPEN_ITEM.match(line)
        if item is not None and item.group(1) and item.group(1).casefold() != "none":
            out.append(ReportSection(path=path, heading=heading, text=_cut_bytes(item.group(1))))
    return out


def _review(path: str, text: str, *, has_followups: bool) -> list[ReportSection]:
    out: list[ReportSection] = []
    first = text.split("\n", 1)[0].strip()
    if first.startswith("VERDICT:"):
        out.append(ReportSection(path=path, heading="Verdict", text=_cut_bytes(first)))
    names = ["blocking findings"] if has_followups else ["blocking findings", "for the human"]
    return out + _named(_h2_sections(text), path, names)


def _run_report(path: str, text: str) -> list[ReportSection]:
    lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        stop = line.startswith("## STOP in")
        if not stop:
            if not _DEVIATION.search(line):
                continue
            bare = line.lstrip("-* \t").casefold()
            if bare.startswith(_NOTHING):
                continue
        lines.append(line)
        if len(lines) >= MAX_REPORT_LINES:
            break
    if not lines:
        return []
    return [ReportSection(path=path, heading="Run report", text=_cut_bytes("\n".join(lines)))]


def cut_report(name: str, text: str, *, has_followups: bool = True) -> list[ReportSection]:
    """The sections J21 takes from one report file. ``name`` is its path; the file name decides.

    ``has_followups`` says ``followups.md`` was read: ``review.md``'s "For the human" is taken only
    when it was not. An unknown file name gives nothing.
    """
    base = posixpath.basename(name)
    if base == "followups.md":
        return _followups(name, text)
    if base == "handoff.md":
        return _named(_h2_sections(text), name, _HANDOFF_SECTIONS)
    if base == "review.md":
        return _review(name, text, has_followups=has_followups)
    if base == "report.md":
        return _run_report(name, text)
    return []


def assemble(
    sections: Iterable[ReportSection], limit: int = REPORT_DIGEST_MAX_BYTES
) -> list[ReportSection]:
    """``sections`` in order, until the next one would take the digest past ``limit`` bytes."""
    kept: list[ReportSection] = []
    used = 0
    for section in sections:
        size = len(section.heading.encode("utf-8")) + len(section.text.encode("utf-8"))
        if used + size > limit:
            break
        used += size
        kept.append(section)
    return kept
