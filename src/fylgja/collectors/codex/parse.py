"""Pure parsers of the Codex collector (``PLAN-final-plan.md`` WP3.1, §4.6).

Everything here is a function over captured text, bytes or mappings: ``pgrep`` and ``lsof`` output,
the ``ps`` table, the names in the Codex state directory, rows of the ``threads`` table and the
lines of a rollout file. None of it does I/O.

**Busy or idle.** A rollout records every turn: ``event_msg`` / ``task_started`` opens one and
``task_complete`` closes it. A turn the user interrupts ends with ``turn_aborted`` instead (49 of
251 turns in the revalidation sample), so both close it. The newest of the three decides:
``task_started`` last means busy, a closing event last means idle. A rollout with no turn event
says nothing (``busy`` is ``None``), which is not the same as idle.

**The current function call** (§4.6) is the newest ``function_call`` or ``custom_tool_call`` whose
``call_id`` has no ``*_output`` yet, while the turn is open. Only the tool name and a short target
are kept (the first line of the command, or the file of an ``apply_patch``); arguments never
travel on.

**The last user message** comes from ``event_msg`` / ``user_message`` (older rollouts), from an
``item_completed`` ``UserMessage`` (current ones), or from a ``response_item`` user message whose
text does not open with ``<`` or ``# AGENTS.md`` (Codex injects its environment and instructions as
user messages).

Torn or foreign lines are skipped, so a rollout read mid-write never raises.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Final

from fylgja.models import (
    ProcessRef,
    SessionActivity,
    SessionActivitySnapshot,
    SessionPayload,
    SessionSnapshot,
    SnapshotItem,
)

__all__ = [
    "BUSY",
    "IDLE",
    "KIND",
    "MAX_CHAIN",
    "OPTIONAL_THREAD_COLUMNS",
    "REQUIRED_THREAD_COLUMNS",
    "SNAPSHOT_SCOPE",
    "SOURCE",
    "SUPPORTED_STATE_VERSIONS",
    "OpenFiles",
    "PendingCall",
    "ProcessEntry",
    "RolloutFacts",
    "StateDbChoice",
    "ThreadRow",
    "activity_of",
    "analyze_rollout",
    "ancestor_chain",
    "call_target",
    "choose_state_db",
    "local_epoch",
    "missing_columns",
    "parse_lsof_fields",
    "parse_pgrep",
    "parse_process_table",
    "process_key",
    "rollout_thread_id",
    "session_key",
    "session_payload",
    "session_snapshot",
    "activity_snapshot",
    "thread_row",
]

SOURCE: Final = "codex"
KIND: Final = "codex"
SNAPSHOT_SCOPE: Final = "all"

BUSY: Final = "busy"
IDLE: Final = "idle"
"""The activity states of Codex (``fylgja.now/1`` ``SessionState``): no finer state exists."""

SUPPORTED_STATE_VERSIONS: Final = frozenset({5})
"""The ``state_N.sqlite`` versions whose ``threads`` table this collector knows (Codex 0.154.0)."""

REQUIRED_THREAD_COLUMNS: Final = ("id", "cwd", "title", "git_branch", "model")
OPTIONAL_THREAD_COLUMNS: Final = ("name", "cli_version", "created_at_ms")

MAX_CHAIN: Final = 64
MAX_PROMPT_CHARS: Final = 300
MAX_REPLY_CHARS: Final = 300
MAX_TITLE_CHARS: Final = 200
MAX_TARGET_CHARS: Final = 80

_STATE_DB: Final = re.compile(r"state_(\d+)\.sqlite")
_ROLLOUT: Final = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl"
)
_PS_LINE: Final = re.compile(
    r"\s*(\d+)\s+(\d+)\s+"
    r"([A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2} \d{4})"
    r"\s+(\S.*?)\s*"
)
_LSTART_FORMAT: Final = "%a %b %d %H:%M:%S %Y"
_PATCH_FILE: Final = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
_WHITESPACE: Final = re.compile(r"\s+")

# --------------------------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------------------------


def parse_pgrep(text: str) -> list[int]:
    """The pids of ``pgrep`` output, one per line, sorted and without duplicates."""
    return sorted({int(line) for line in map(str.strip, text.splitlines()) if line.isdigit()})


@dataclass(frozen=True)
class OpenFiles:
    """What ``lsof`` says about one process: its working directory and its open rollout files."""

    pid: int
    cwd: str | None
    rollouts: tuple[str, ...]


def rollout_thread_id(path: str) -> str | None:
    """The thread id in a rollout file name (``rollout-<ts>-<thread id>.jsonl``), else ``None``."""
    match = _ROLLOUT.fullmatch(PurePosixPath(path).name)
    return None if match is None else match[1]


def parse_lsof_fields(text: str) -> dict[int, OpenFiles]:
    """Parse ``lsof -Fpfn`` output: ``p`` starts a process, ``f`` a descriptor, ``n`` its name.

    The descriptor named ``cwd`` gives the working directory; every other name that is a rollout
    file is kept, once, in the order lsof listed it.
    """
    found: dict[int, OpenFiles] = {}
    pid: int | None = None
    fd = ""
    cwd: str | None = None
    rollouts: list[str] = []

    def flush() -> None:
        if pid is not None:
            found[pid] = OpenFiles(pid, cwd, tuple(rollouts))

    for line in text.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            flush()
            pid = int(value) if value.isdigit() else None
            fd, cwd, rollouts = "", None, []
        elif pid is None:
            continue
        elif tag == "f":
            fd = value
        elif tag == "n":
            if fd == "cwd":
                cwd = value
            elif rollout_thread_id(value) is not None and value not in rollouts:
                rollouts.append(value)
    flush()
    return found


@dataclass(frozen=True)
class ProcessEntry:
    ppid: int
    started: datetime
    """The process start as ``ps`` printed it: naive local wall-clock time."""
    comm: str


def parse_process_table(text: str) -> dict[int, ProcessEntry]:
    """Parse ``ps -A -o pid=,ppid=,lstart=,comm=`` output. ``comm`` may hold spaces.

    Lines that do not parse are skipped.
    """
    table: dict[int, ProcessEntry] = {}
    for line in text.splitlines():
        match = _PS_LINE.fullmatch(line)
        if match is None:
            continue
        try:
            started = datetime.strptime(_WHITESPACE.sub(" ", match[3]), _LSTART_FORMAT)
        except ValueError:
            continue
        table[int(match[1])] = ProcessEntry(int(match[2]), started, match[4])
    return table


def local_epoch(naive_local: datetime) -> float:
    """Epoch seconds of a naive local wall-clock time (``ps -o lstart=`` prints local time)."""
    return naive_local.astimezone().timestamp()


def ancestor_chain(table: Mapping[int, ProcessEntry], pid: int) -> tuple[ProcessRef, ...]:
    """``pid`` and its ancestors, nearest first, up to (not including) pid 1.

    The walk stops at a pid the table does not list, at a loop and after :data:`MAX_CHAIN` steps.
    """
    chain: list[ProcessRef] = []
    seen: set[int] = set()
    current = pid
    while current > 1 and current in table and current not in seen and len(chain) < MAX_CHAIN:
        seen.add(current)
        entry = table[current]
        chain.append(ProcessRef(pid=current, ppid=entry.ppid, comm=entry.comm))
        current = entry.ppid
    return tuple(chain)


def process_key(pid: int, started_at: float) -> str:
    """The A11 process key: the pid plus the process start time (epoch seconds)."""
    return f"{pid}-{int(started_at)}"


def session_key(thread_id: str) -> str:
    """The key records use for a Codex session (``codex:<thread id>``)."""
    return f"{KIND}:{thread_id}"


# --------------------------------------------------------------------------------------------
# The state database
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StateDbChoice:
    """The newest ``state_N.sqlite`` in the Codex state directory."""

    name: str
    version: int

    @property
    def supported(self) -> bool:
        return self.version in SUPPORTED_STATE_VERSIONS


def choose_state_db(names: Iterable[str]) -> StateDbChoice | None:
    """The state database with the highest ``N``; its ``-wal`` and ``-shm`` files do not count.

    The newest version is the one Codex writes, so an unknown newest version is not skipped in
    favour of an older one: the collector reports it and does not guess.
    """
    best: StateDbChoice | None = None
    for name in names:
        match = _STATE_DB.fullmatch(name)
        if match is not None and (best is None or int(match[1]) > best.version):
            best = StateDbChoice(name, int(match[1]))
    return best


def missing_columns(columns: Iterable[str]) -> list[str]:
    """The required ``threads`` columns that ``columns`` lacks, in the required order."""
    present = set(columns)
    return [name for name in REQUIRED_THREAD_COLUMNS if name not in present]


@dataclass(frozen=True)
class ThreadRow:
    """The columns of one ``threads`` row the collector uses. Empty strings become ``None``."""

    id: str
    cwd: str | None
    title: str | None
    branch: str | None
    model: str | None
    name: str | None = None
    cli_version: str | None = None
    created_at_ms: int | None = None

    @property
    def label(self) -> str | None:
        """What the thread is called: the name the user gave it, else the generated title."""
        return self.name or self.title


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def thread_row(row: Mapping[str, Any]) -> ThreadRow | None:
    """A :class:`ThreadRow` from a database row; a row without an id gives ``None``."""
    thread_id = _text(row.get("id"))
    if thread_id is None:
        return None
    created = row.get("created_at_ms")
    return ThreadRow(
        id=thread_id,
        cwd=_text(row.get("cwd")),
        title=_clip(_text(row.get("title")), MAX_TITLE_CHARS),
        branch=_text(row.get("git_branch")),
        model=_text(row.get("model")),
        name=_clip(_text(row.get("name")), MAX_TITLE_CHARS),
        cli_version=_text(row.get("cli_version")),
        created_at_ms=created
        if isinstance(created, int) and not isinstance(created, bool)
        else None,
    )


# --------------------------------------------------------------------------------------------
# Rollouts
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingCall:
    """A function call that has no output yet."""

    name: str
    target: str | None = None


@dataclass(frozen=True)
class RolloutFacts:
    """What a rollout (or its tail) says about the thread."""

    busy: bool | None = None
    """``True`` while a turn is open, ``False`` once it ended, ``None`` with no turn event."""
    last_user: str | None = None
    last_reply: str | None = None
    pending: PendingCall | None = None
    model: str | None = None
    cwd: str | None = None
    lines: int = field(default=0, compare=False)
    """How many lines parsed; torn and foreign lines do not count."""

    @property
    def state(self) -> str | None:
        return None if self.busy is None else BUSY if self.busy else IDLE


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    collapsed = _WHITESPACE.sub(" ", text).strip()
    if not collapsed:
        return None
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def _first_line(text: str) -> str | None:
    for line in text.splitlines():
        if line.strip():
            return _clip(line, MAX_TARGET_CHARS)
    return None


def _command_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        words: list[str] = value
        if len(words) >= 3 and words[1] in {"-c", "-lc"}:
            return words[2]  # ["bash", "-lc", "<script>"]: the script is the command
        return " ".join(words)
    return None


def call_target(name: str, raw: object) -> str | None:
    """A short target for a tool call: the command's first line or the file a patch touches.

    ``raw`` is the call's ``arguments`` (a JSON string) or ``input`` (free text). Nothing else of
    the arguments is kept.
    """
    if not isinstance(raw, str):
        return None
    if name == "apply_patch":
        match = _PATCH_FILE.search(raw)
        return None if match is None else _clip(match[1], MAX_TARGET_CHARS)
    try:
        arguments = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(arguments, dict):
        return None
    for key in ("cmd", "command"):
        command = _command_text(arguments.get(key))
        if command is not None:
            return _first_line(command)
    path = arguments.get("path")
    return _clip(path, MAX_TARGET_CHARS) if isinstance(path, str) else None


def _content_text(content: object, kinds: tuple[str, ...]) -> str | None:
    """The joined ``text`` of the parts of ``content`` whose type is in ``kinds``."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = [
        part["text"]
        for part in content
        if isinstance(part, dict)
        and str(part.get("type", "")).lower() in kinds
        and isinstance(part.get("text"), str)
    ]
    return "\n".join(parts) if parts else None


def _injected(text: str) -> bool:
    """Codex's own user messages: the environment block, instructions and the like."""
    opening = text.lstrip()
    return opening.startswith(("<", "# AGENTS.md"))


class _Scan:
    """The running state of :func:`analyze_rollout`."""

    def __init__(self) -> None:
        self.busy: bool | None = None
        self.user: str | None = None
        self.reply: str | None = None
        self.calls: dict[str, PendingCall] = {}
        self.model: str | None = None
        self.cwd: str | None = None
        self.lines = 0

    def turn_started(self) -> None:
        self.busy = True
        self.calls.clear()  # a call left over from an earlier turn is not running now

    def turn_ended(self, last_message: object = None) -> None:
        self.busy = False
        self.calls.clear()
        reply = _clip(last_message if isinstance(last_message, str) else None, MAX_REPLY_CHARS)
        if reply is not None:
            self.reply = reply

    def event(self, payload: Mapping[str, Any]) -> None:
        kind = payload.get("type")
        if kind == "task_started":
            self.turn_started()
        elif kind == "task_complete":
            self.turn_ended(payload.get("last_agent_message"))
        elif kind == "turn_aborted":
            self.turn_ended()
        elif kind == "user_message":
            self._user(payload.get("message"))
        elif kind == "agent_message":
            self._reply(payload.get("message"))
        elif kind == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict):
                self._item(item)

    def _item(self, item: Mapping[str, Any]) -> None:
        if item.get("type") == "UserMessage":
            self._user(_content_text(item.get("content"), ("text", "input_text")))
        elif item.get("type") == "AgentMessage":
            self._reply(_content_text(item.get("content"), ("text", "output_text")))

    def _user(self, text: object) -> None:
        clipped = _clip(text if isinstance(text, str) else None, MAX_PROMPT_CHARS)
        if clipped is not None:
            self.user = clipped

    def _reply(self, text: object) -> None:
        clipped = _clip(text if isinstance(text, str) else None, MAX_REPLY_CHARS)
        if clipped is not None:
            self.reply = clipped

    def response(self, payload: Mapping[str, Any]) -> None:
        kind = payload.get("type")
        if kind == "message":
            role = payload.get("role")
            if role == "user":
                text = _content_text(payload.get("content"), ("input_text",))
                if text is not None and not _injected(text):
                    self._user(text)
            elif role == "assistant":
                self._reply(_content_text(payload.get("content"), ("output_text",)))
        elif kind in {"function_call", "custom_tool_call"}:
            call_id, name = payload.get("call_id"), payload.get("name")
            if isinstance(call_id, str) and isinstance(name, str) and name:
                raw = payload.get("arguments" if kind == "function_call" else "input")
                self.calls[call_id] = PendingCall(name, call_target(name, raw))
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                self.calls.pop(call_id, None)

    def meta(self, kind: object, payload: Mapping[str, Any]) -> None:
        cwd = _text(payload.get("cwd"))
        if cwd is not None:
            self.cwd = cwd
        if kind == "turn_context":
            model = _text(payload.get("model"))
            if model is not None:
                self.model = model

    def facts(self) -> RolloutFacts:
        pending = next(reversed(self.calls.values()), None) if self.busy else None
        return RolloutFacts(
            busy=self.busy,
            last_user=self.user,
            last_reply=self.reply,
            pending=pending,
            model=self.model,
            cwd=self.cwd,
            lines=self.lines,
        )


def analyze_rollout(lines: Iterable[bytes | str]) -> RolloutFacts:
    """Fold the lines of a rollout, oldest first, into :class:`RolloutFacts`.

    A line that is not a JSON object with a ``type`` is skipped; so is an event of a kind this
    function does not know. The result never holds more than the clipped texts above.
    """
    scan = _Scan()
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload")
        kind = entry.get("type")
        if not isinstance(payload, dict) or not isinstance(kind, str):
            continue
        scan.lines += 1
        if kind == "event_msg":
            scan.event(payload)
        elif kind == "response_item":
            scan.response(payload)
        elif kind in {"session_meta", "turn_context"}:
            scan.meta(kind, payload)
    return scan.facts()


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------


def session_payload(
    thread_id: str,
    *,
    pid: int,
    thread: ThreadRow | None,
    facts: RolloutFacts | None,
    process_cwd: str | None,
    key: str | None,
    ancestry: tuple[ProcessRef, ...] | None,
) -> SessionPayload:
    """The ``session`` payload. The database row wins, the rollout and the process fill the gaps."""
    cwd = (thread.cwd if thread else None) or (facts.cwd if facts else None) or process_cwd
    started = (
        None
        if thread is None or thread.created_at_ms is None
        else datetime.fromtimestamp(thread.created_at_ms / 1000, UTC)
    )
    return SessionPayload(
        kind=KIND,
        native_id=thread_id,
        pid=pid,
        cwd=cwd,
        name=thread.name if thread else None,
        model=(thread.model if thread else None) or (facts.model if facts else None),
        branch=thread.branch if thread else None,
        started_at=started,
        process_key=key,
        version=thread.cli_version if thread else None,
        ancestry=ancestry or None,
    )


def _title_for(label: str | None, prompt: str | None) -> str | None:
    """The title, unless it is just the start of the prompt (Codex titles a thread by its first
    message, and "X: X" says nothing)."""
    if label is None:
        return None
    if prompt is not None and prompt.startswith(label.rstrip("….").rstrip()):
        return None
    return label


def activity_of(thread: ThreadRow | None, facts: RolloutFacts | None) -> SessionActivity:
    """The Codex ``SessionActivity`` (§4.6): title, last user message, current function call.

    The current call is only set while a turn is open.
    """
    prompt = None if facts is None else facts.last_user
    pending = None if facts is None else facts.pending
    return SessionActivity(
        state=None if facts is None else facts.state,
        current_tool=None if pending is None else pending.name,
        current_target=None if pending is None else pending.target,
        last_prompt=prompt,
        last_reply=None if facts is None else facts.last_reply,
        title=_title_for(None if thread is None else thread.label, prompt),
        branch=None if thread is None else thread.branch,
    )


def session_snapshot(
    items: Iterable[tuple[str, SessionPayload]], observed_at: datetime
) -> SessionSnapshot:
    """A complete snapshot: a session missing from it is ended in the store."""
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
