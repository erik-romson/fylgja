"""Pure parsers and builders for the Claude session registry (``PLAN-final-plan.md`` WP1.7).

Everything here is a function over captured bytes, text or already-parsed values: no I/O and no
clock. The adapter lists and reads the files and runs ``ps``; the registry module decides what
changed; the collector turns rows into records.

The registry (``~/.claude/sessions/<pid>.json``, R5) is read strictly:

- only names that match the A13 glob ``^[0-9]+\\.json$`` are registry rows (never ``*.key``);
- a row must be a JSON object with an integer ``pid`` equal to the file name, a ``sessionId`` and
  a ``cwd``; anything else is rejected;
- unknown fields are ignored, and optional fields of the wrong type are dropped.

Names, working directories and session ids are free text or identifiers: they are carried through
and never logged here (the ingest gate redacts what needs it).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Final

from fylgja.collectors.claude.facts import PendingTool, open_task
from fylgja.models import (
    ProcessRef,
    SessionActivity,
    SessionActivitySnapshot,
    SessionPayload,
    SessionSnapshot,
    SnapshotItem,
)

__all__ = [
    "BRIDGE_URL_PREFIX",
    "KIND",
    "SNAPSHOT_SCOPE",
    "SOURCE",
    "STATUS_TO_STATE",
    "Liveness",
    "RegistryRow",
    "RowRejected",
    "activity_of",
    "activity_snapshot",
    "ancestor_chain",
    "check_liveness",
    "cwd_ignored",
    "map_state",
    "parse_proc_start",
    "parse_ps_starts",
    "parse_ps_table",
    "parse_row",
    "process_key",
    "registry_pid",
    "remote_url",
    "session_key",
    "session_payload",
    "session_snapshot",
]

SOURCE: Final = "claude"
KIND: Final = "claude"
SNAPSHOT_SCOPE: Final = "all"
BRIDGE_URL_PREFIX: Final = "https://claude.ai/code/"

MAX_ROW_BYTES: Final = 1 << 20
"""A registry row is a few hundred bytes; a bigger file is not one."""
REUSE_SLACK_SECONDS: Final = 5.0
"""Without ``procStart``: a process that started this long after the row's ``startedAt`` is not the
process that wrote the row."""
PROC_START_TOLERANCE_SECONDS: Final = 1.0

_REGISTRY_NAME: Final = re.compile(r"[0-9]+\.json")
_BRIDGE_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,200}")
_LSTART_FORMAT: Final = "%a %b %d %H:%M:%S %Y"
_PS_TABLE_LINE: Final = re.compile(r"\s*(\d+)\s+(\d+)\s+(\S.*?)\s*")

MAX_CHAIN: Final = 64
"""The longest ancestor chain kept; a real one is under ten."""

STATUS_TO_STATE: Final[Mapping[str, str]] = {
    "idle": "idle",
    "busy": "working",
    "waiting": "waiting",
}
"""Registry ``status`` to the normalized activity state, until hooks give a finer one (WP2.3)."""


# --------------------------------------------------------------------------------------------
# File names
# --------------------------------------------------------------------------------------------


def registry_pid(name: str) -> int | None:
    """The pid of a registry file name, or ``None`` when ``name`` is not ``<digits>.json``."""
    if _REGISTRY_NAME.fullmatch(name) is None:
        return None
    return int(name.removesuffix(".json"))


# --------------------------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistryRow:
    """One registry file. Every field but the identifying three is optional."""

    pid: int
    session_id: str
    cwd: str
    started_at_ms: int | None = None
    proc_start: datetime | None = None
    """``procStart`` as written: wall-clock text in UTC (observed), so the datetime is naive UTC."""
    version: str | None = None
    kind: str | None = None
    entrypoint: str | None = None
    name: str | None = None
    status: str | None = None
    waiting_for: str | None = None
    bridge_session_id: str | None = None
    peer_protocol: int | None = None
    messaging_socket_path: str | None = None
    """Kept for the messaging adapter (A9, A14); never emitted."""


@dataclass(frozen=True)
class RowRejected:
    """A file that is not a usable registry row. ``reason`` is a short code, never file content."""

    reason: str


def _str(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _int(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def parse_proc_start(text: str) -> datetime | None:
    """A ``procStart`` or ``ps -o lstart=`` value (``Mon Oct  5 11:38:48 2026``) as a naive time."""
    try:
        return datetime.strptime(text.strip(), _LSTART_FORMAT)  # noqa: DTZ007 - naive on purpose
    except ValueError:
        return None


def parse_row(data: bytes, *, file_pid: int) -> RegistryRow | RowRejected:
    """Parse the bytes of ``<file_pid>.json``."""
    if len(data) > MAX_ROW_BYTES:
        return RowRejected("too_large")
    try:
        decoded = json.loads(data)
    except (ValueError, RecursionError):
        return RowRejected("not_json")
    if not isinstance(decoded, dict):
        return RowRejected("not_an_object")
    pid = decoded.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return RowRejected("bad_pid")
    if pid != file_pid:
        return RowRejected("pid_mismatch")
    session_id = _str(decoded, "sessionId")
    if session_id is None:
        return RowRejected("bad_session_id")
    cwd = _str(decoded, "cwd")
    if cwd is None:
        return RowRejected("bad_cwd")
    proc_start_text = _str(decoded, "procStart")
    return RegistryRow(
        pid=pid,
        session_id=session_id,
        cwd=cwd,
        started_at_ms=_int(decoded, "startedAt"),
        proc_start=None if proc_start_text is None else parse_proc_start(proc_start_text),
        version=_str(decoded, "version"),
        kind=_str(decoded, "kind"),
        entrypoint=_str(decoded, "entrypoint"),
        name=_str(decoded, "name"),
        status=_str(decoded, "status"),
        waiting_for=_str(decoded, "waitingFor"),
        bridge_session_id=_str(decoded, "bridgeSessionId"),
        peer_protocol=_int(decoded, "peerProtocol"),
        messaging_socket_path=_str(decoded, "messagingSocketPath"),
    )


# --------------------------------------------------------------------------------------------
# Liveness
# --------------------------------------------------------------------------------------------


def parse_ps_starts(text: str) -> dict[int, datetime]:
    """Parse ``ps -o pid=,lstart=`` output into ``{pid: naive local start time}``.

    Lines that do not parse are skipped.
    """
    starts: dict[int, datetime] = {}
    for line in text.splitlines():
        pid_text, _, rest = line.strip().partition(" ")
        if not pid_text.isdigit():
            continue
        started = parse_proc_start(rest)
        if started is not None:
            starts[int(pid_text)] = started
    return starts


def parse_ps_table(text: str) -> dict[int, tuple[int, str]]:
    """Parse ``ps -A -o pid=,ppid=,comm=`` output into ``{pid: (ppid, comm)}``.

    ``comm`` may hold spaces (an application path). Lines that do not parse are skipped.
    """
    table: dict[int, tuple[int, str]] = {}
    for line in text.splitlines():
        match = _PS_TABLE_LINE.fullmatch(line)
        if match is not None:
            table[int(match[1])] = (int(match[2]), match[3])
    return table


def ancestor_chain(table: Mapping[int, tuple[int, str]], pid: int) -> tuple[ProcessRef, ...]:
    """``pid`` and its ancestors, nearest first, up to (not including) pid 1.

    The walk stops at a pid the table does not list, at a loop and after :data:`MAX_CHAIN` steps.
    A ``pid`` that the table does not list gives an empty chain.
    """
    chain: list[ProcessRef] = []
    seen: set[int] = set()
    current = pid
    while current > 1 and current in table and current not in seen and len(chain) < MAX_CHAIN:
        seen.add(current)
        ppid, comm = table[current]
        chain.append(ProcessRef(pid=current, ppid=ppid, comm=comm))
        current = ppid
    return tuple(chain)


class Liveness(StrEnum):
    LIVE = "live"
    DEAD = "dead"
    """No process has the pid."""
    REUSED = "reused"
    """A process has the pid, but it is not the one that wrote the row."""


def check_liveness(row: RegistryRow, started_at: float | None) -> Liveness:
    """Is the row's process alive?

    ``started_at`` is the start of the process that now owns ``row.pid`` as epoch seconds, or
    ``None`` when no process has it. A row with ``procStart`` is live only when the process start
    equals it (within a second). A row without one is live unless the process started after the
    row's own ``startedAt``, which a pid reused later must have.
    """
    if started_at is None:
        return Liveness.DEAD
    if row.proc_start is not None:
        written = row.proc_start.replace(tzinfo=UTC).timestamp()
        if abs(written - started_at) <= PROC_START_TOLERANCE_SECONDS:
            return Liveness.LIVE
        return Liveness.REUSED
    if (
        row.started_at_ms is not None
        and started_at > row.started_at_ms / 1000 + REUSE_SLACK_SECONDS
    ):
        return Liveness.REUSED
    return Liveness.LIVE


def process_key(pid: int, started_at: float) -> str:
    """The A11 process key: the pid plus the process start time (epoch seconds)."""
    return f"{pid}-{int(started_at)}"


def session_key(session_id: str) -> str:
    """The key records use for a Claude session (``kind:native_id``)."""
    return f"{KIND}:{session_id}"


# --------------------------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------------------------


def cwd_ignored(cwd: str, patterns: Iterable[str]) -> bool:
    """Does ``cwd`` match an ``ignore_cwd`` entry?

    An absolute entry matches that directory and everything below it. A relative entry such as
    ``CodexBar/ClaudeProbe`` matches when its path components appear in a row inside ``cwd``.
    """
    parts = PurePosixPath(cwd).parts
    for pattern in patterns:
        wanted = PurePosixPath(pattern).parts
        if not wanted:
            continue
        if pattern.startswith("/"):
            if parts[: len(wanted)] == wanted:
                return True
            continue
        width = len(wanted)
        if any(parts[i : i + width] == wanted for i in range(len(parts) - width + 1)):
            return True
    return False


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------


def map_state(status: str | None) -> str | None:
    """The activity state of a registry ``status``; an unknown status gives ``None``."""
    return None if status is None else STATUS_TO_STATE.get(status)


def remote_url(bridge_session_id: str | None) -> str | None:
    """The Remote Control link of a ``bridgeSessionId`` (absent unless Remote Control is on)."""
    if bridge_session_id is None or _BRIDGE_ID.fullmatch(bridge_session_id) is None:
        return None
    return f"{BRIDGE_URL_PREFIX}{bridge_session_id}"


def session_payload(
    row: RegistryRow,
    *,
    process_key: str | None,
    predecessor_key: str | None,
    facts: Mapping[str, Any] | None = None,
    ancestry: tuple[ProcessRef, ...] | None = None,
) -> SessionPayload:
    started = (
        None if row.started_at_ms is None else datetime.fromtimestamp(row.started_at_ms / 1000, UTC)
    )
    return SessionPayload(
        kind=KIND,
        native_id=row.session_id,
        pid=row.pid,
        cwd=row.cwd,
        name=row.name,
        model=_fact(facts, "model"),
        branch=_fact(facts, "branch"),
        remote_url=remote_url(row.bridge_session_id),
        started_at=started,
        process_key=process_key,
        predecessor_key=predecessor_key,
        entrypoint=row.entrypoint,
        version=row.version,
        peer_protocol=row.peer_protocol,
        ancestry=ancestry or None,
    )


def _fact(facts: Mapping[str, Any] | None, name: str) -> str | None:
    """A non-empty string of the facts block, else ``None``."""
    value = None if facts is None else facts.get(name)
    return value if isinstance(value, str) and value else None


def activity_of(
    row: RegistryRow,
    facts: Mapping[str, Any] | None = None,
    pending: PendingTool | None = None,
) -> SessionActivity:
    """The Claude ``SessionActivity`` (§4.6): the registry state plus the transcript's facts.

    ``current_tool`` and ``current_target`` come from the newest tool call without a result, and
    only while the session is working or waiting. The title falls back to the registry name until
    the transcript has an ``ai-title``.
    """
    state = map_state(row.status)
    tool = pending if state in ("working", "waiting") else None
    tasks = facts.get("tasks") if facts is not None else None
    prompts = facts.get("prompts") if facts is not None else None
    last_typed = prompts[-1] if isinstance(prompts, list) and prompts else None
    return SessionActivity(
        state=state,
        waiting_reason=row.waiting_for if state == "waiting" else None,
        current_tool=None if tool is None else tool.name,
        current_target=None if tool is None else tool.target,
        open_task=open_task(tasks) if isinstance(tasks, list) else None,
        last_prompt=_fact(facts, "prompt") or (last_typed if isinstance(last_typed, str) else None),
        last_reply=_fact(facts, "said"),
        title=_fact(facts, "title") or row.name,
        branch=_fact(facts, "branch"),
        facts=None if facts is None else dict(facts),
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
