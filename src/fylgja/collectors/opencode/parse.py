"""Pure parsers of the OpenCode collector (``PLAN-final-plan.md`` WP3.2, §4.6).

Everything here is a function over rows the adapter already read (mappings of column name to
value) or over process facts. None of it does I/O, and none of it reads message or part *text*: the
collector asks the database for roles, timestamps, tool names and a few short tool-input fields
only.

**Which session belongs to which process.** OpenCode keeps one SQLite database for every session.
A running ``opencode`` process has no id on its command line, so the join is the working directory:
a process whose cwd is ``D`` runs the newest top-level session (``parent_id`` null, not archived)
whose ``directory`` is ``D``. Several processes in one directory take the newest sessions of that
directory, matched by age: the oldest process gets the oldest of them (:func:`assign_sessions`).

**Busy or idle.** A turn is an assistant message. The newest assistant message of the session has
no ``time.completed`` while it streams, so that means busy. Three cases are not busy: the message
carries an ``error`` (an aborted or failed turn), it is older than ``busy_stale`` (a turn the
process died in; the database holds such rows from months ago) and there is no assistant message at
all. The stale limit looks at the newest of the message's, the session's and the newest tool
part's update time.

**The current tool** (§4.6) is the newest tool part of that incomplete message whose state is
``pending`` or ``running``. Only the tool name and a short target are kept: the first line of a
command, the file name of an edited file, a pattern, a URL or a task description.

**The own task list.** The ``todo`` table holds the session's task list (the ``todowrite`` tool
replaces it). It is exposed as ``facts.tasks`` in the shape Claude's list has
(``{status, content}``, at most 20, ``content`` cut to 80), and the first ``in_progress`` row is
the open task of the one-liner (rule 4).

It is also imported as read-only todos (WP4.3): :func:`todo_snapshot` is the session's whole list,
``opencode-todo:opencode:<session id>``. A row is keyed by its native id: the table's primary key is
``(session_id, position)``, so the key is the ``position``. ``todowrite`` rewrites the list, so a
task renamed at the same position is updated in place. The items are scoped to the session and
linked to it by its session key.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Final

from fylgja.collectors.todo_lists import agent_status, todo_list_snapshot
from fylgja.models import (
    ProcessRef,
    SessionActivity,
    SessionActivitySnapshot,
    SessionPayload,
    SessionSnapshot,
    SnapshotItem,
    TodoPayload,
    TodoSnapshot,
)

__all__ = [
    "BUSY",
    "IDLE",
    "INPUT_FIELDS",
    "KIND",
    "REQUIRED_COLUMNS",
    "SNAPSHOT_SCOPE",
    "SOURCE",
    "TODO_SOURCE",
    "MessageRow",
    "PartRow",
    "ProcInfo",
    "SessionRow",
    "TodoRow",
    "TurnFacts",
    "activity_of",
    "activity_snapshot",
    "assign_sessions",
    "current_tool",
    "message_row",
    "missing_columns",
    "model_name",
    "norm_dir",
    "open_task",
    "open_turn",
    "part_row",
    "session_key",
    "session_payload",
    "session_row",
    "session_snapshot",
    "task_entries",
    "todo_row",
    "todo_snapshot",
    "tool_target",
    "turn_facts",
]

SOURCE: Final = "opencode"
KIND: Final = "opencode"
TODO_SOURCE: Final = "opencode-todo"
"""The ``todos.source`` of an imported OpenCode todo (WP4.3)."""
SNAPSHOT_SCOPE: Final = "all"

BUSY: Final = "busy"
IDLE: Final = "idle"
"""The activity states of OpenCode: Codex's vocabulary (``fylgja.now/1`` ``SessionState``)."""

REQUIRED_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    "session": (
        "id",
        "parent_id",
        "directory",
        "title",
        "time_created",
        "time_updated",
        "time_archived",
    ),
    "message": ("id", "session_id", "time_created", "time_updated", "data"),
    "part": ("id", "message_id", "time_updated", "data"),
    "todo": ("session_id", "content", "status", "position"),
}
"""The columns the collector queries, by table (revalidated on OpenCode 1.18.26). A missing one is
a ``schema_mismatch``: the collector does not guess a layout."""

OPTIONAL_SESSION_COLUMNS: Final = ("model", "agent", "version")

MAX_TITLE_CHARS: Final = 200
MAX_TARGET_CHARS: Final = 80
MAX_TASKS: Final = 20
MAX_TASK_CHARS: Final = 80
MAX_IMPORT_CHARS: Final = 1000
"""The text of a todo kept for the import (``facts.tasks`` shows :data:`MAX_TASK_CHARS`)."""
MAX_CHAIN_PARTS: Final = 40
"""How many of the newest parts of the incomplete message the adapter reads."""

_WHITESPACE: Final = re.compile(r"\s+")
_PLACEHOLDER_TITLE: Final = re.compile(r"(?:New|Child) session - \d{4}-\d{2}-\d{2}T")
_OPEN_TOOL_STATES: Final = frozenset({"pending", "running"})


def missing_columns(present: Mapping[str, Iterable[str]]) -> list[str]:
    """``table.column`` for every required column ``present`` lacks, in the required order.

    ``present`` maps a table name to its column names; a table that is not a key lacks all of its
    required columns.
    """
    missing: list[str] = []
    for table, required in REQUIRED_COLUMNS.items():
        have = set(present.get(table, ()))
        missing.extend(f"{table}.{column}" for column in required if column not in have)
    return missing


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _millis(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    collapsed = _WHITESPACE.sub(" ", text).strip()
    if not collapsed:
        return None
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def _first_line(text: str, limit: int) -> str | None:
    for line in text.splitlines():
        if line.strip():
            return _clip(line, limit)
    return None


def norm_dir(path: str) -> str:
    """A directory without its trailing slashes (``/`` stays ``/``)."""
    return path.rstrip("/") or "/"


def session_key(session_id: str) -> str:
    """The key records use for an OpenCode session (``opencode:<session id>``)."""
    return f"{KIND}:{session_id}"


def model_name(raw: object) -> str | None:
    """The model id of the ``session.model`` column (JSON: ``{"id", "providerID", "variant"}``)."""
    text = _text(raw)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return _clip(text, MAX_TARGET_CHARS)  # an older layout stored the plain name
    if isinstance(value, dict):
        return _clip(_text(value.get("id")), MAX_TARGET_CHARS)
    return None


# --------------------------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRow:
    """The columns of one top-level ``session`` row the collector uses."""

    id: str
    directory: str
    title: str | None
    created_ms: int
    updated_ms: int
    model: str | None = None
    agent: str | None = None
    version: str | None = None


def session_row(row: Mapping[str, Any]) -> SessionRow | None:
    """A :class:`SessionRow`; a row without an id, a directory or its times gives ``None``."""
    session_id = _text(row.get("id"))
    directory = _text(row.get("directory"))
    created = _millis(row.get("time_created"))
    updated = _millis(row.get("time_updated"))
    if session_id is None or directory is None or created is None or updated is None:
        return None
    title = _clip(_text(row.get("title")), MAX_TITLE_CHARS)
    if title is not None and _PLACEHOLDER_TITLE.match(title):
        title = None  # OpenCode's own "New session - <timestamp>" says nothing
    return SessionRow(
        id=session_id,
        directory=norm_dir(directory),
        title=title,
        created_ms=created,
        updated_ms=updated,
        model=model_name(row.get("model")),
        agent=_clip(_text(row.get("agent")), MAX_TARGET_CHARS),
        version=_text(row.get("version")),
    )


@dataclass(frozen=True)
class MessageRow:
    """One message: its role and whether its turn finished. No text."""

    id: str
    role: str | None
    created_ms: int
    updated_ms: int
    completed_ms: int | None
    errored: bool


def message_row(row: Mapping[str, Any]) -> MessageRow | None:
    """A :class:`MessageRow` from ``id``, ``role``, ``completed``, ``errored`` and the times."""
    message_id = _text(row.get("id"))
    created = _millis(row.get("time_created"))
    if message_id is None or created is None:
        return None
    updated = _millis(row.get("time_updated"))
    return MessageRow(
        id=message_id,
        role=_text(row.get("role")),
        created_ms=created,
        updated_ms=created if updated is None else updated,
        completed_ms=_millis(row.get("completed")),
        errored=bool(row.get("errored")),
    )


@dataclass(frozen=True)
class PartRow:
    """One tool part of a message: the tool, its state and a few short input fields."""

    tool: str
    status: str | None
    updated_ms: int | None
    target: str | None


INPUT_FIELDS: Final = ("command", "filePath", "pattern", "url", "description", "path")
"""The tool-input keys the adapter extracts from a part (everything else stays in the database)."""


def tool_target(tool: str, fields: Mapping[str, Any]) -> str | None:
    """A short target for a tool call, from the extracted input ``fields``.

    ``bash``: the first line of the command (else its description); ``read``, ``edit`` and
    ``write``: the file name; ``glob`` and ``grep``: the pattern; ``webfetch``: the URL; ``task``:
    its description; ``list``: the path. Any other tool has no target.
    """
    if tool == "bash":
        command = _text(fields.get("command"))
        if command is not None:
            return _first_line(command, MAX_TARGET_CHARS)
        return _clip(_text(fields.get("description")), MAX_TARGET_CHARS)
    if tool in {"read", "edit", "write"}:
        path = _text(fields.get("filePath"))
        return None if path is None else _clip(PurePosixPath(path).name, MAX_TARGET_CHARS)
    if tool in {"glob", "grep"}:
        return _clip(_text(fields.get("pattern")), MAX_TARGET_CHARS)
    if tool == "webfetch":
        return _clip(_text(fields.get("url")), MAX_TARGET_CHARS)
    if tool == "task":
        return _clip(_text(fields.get("description")), MAX_TARGET_CHARS)
    if tool == "list":
        return _clip(_text(fields.get("path")), MAX_TARGET_CHARS)
    return None


def part_row(row: Mapping[str, Any]) -> PartRow | None:
    """A :class:`PartRow` of a ``tool`` part; any other part, or one without a tool, is ``None``."""
    if row.get("type") != "tool":
        return None
    tool = _text(row.get("tool"))
    if tool is None:
        return None
    fields = {name: row.get(name) for name in INPUT_FIELDS}
    return PartRow(
        tool=_clip(tool, MAX_TARGET_CHARS) or tool,
        status=_text(row.get("status")),
        updated_ms=_millis(row.get("time_updated")),
        target=tool_target(tool, fields),
    )


@dataclass(frozen=True)
class TodoRow:
    content: str
    """The text as ``facts.tasks`` shows it: collapsed, cut to :data:`MAX_TASK_CHARS`."""
    status: str
    position: int
    text: str = field(default="", compare=False)
    """The text for the import: collapsed, cut to :data:`MAX_IMPORT_CHARS` (``content`` when
    empty)."""


def todo_row(row: Mapping[str, Any]) -> TodoRow | None:
    """A :class:`TodoRow`; a row without content gives ``None``."""
    raw = _text(row.get("content"))
    content = _clip(raw, MAX_TASK_CHARS)
    if content is None:
        return None
    position = _millis(row.get("position"))
    return TodoRow(
        content=content,
        status=_text(row.get("status")) or "pending",
        position=0 if position is None else position,
        text=_clip(raw, MAX_IMPORT_CHARS) or content,
    )


# --------------------------------------------------------------------------------------------
# Turn state and the current tool
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnFacts:
    """Whether a turn is in flight, and which message it is."""

    busy: bool
    message_id: str | None = None
    """The incomplete assistant message of a busy session (its tool parts say what runs)."""

    @property
    def state(self) -> str:
        return BUSY if self.busy else IDLE


def open_turn(messages: Sequence[MessageRow]) -> MessageRow | None:
    """The newest assistant message when it has no ``time.completed`` and no error, else ``None``.

    ``messages`` are newest first. Its tool parts are what the adapter reads next.
    """
    for message in messages:
        if message.role == "assistant":
            return None if message.completed_ms is not None or message.errored else message
    return None


def turn_facts(
    messages: Sequence[MessageRow],
    *,
    now_ms: int,
    stale_ms: int,
    session_updated_ms: int = 0,
    parts_updated_ms: int = 0,
) -> TurnFacts:
    """Busy or idle from the newest messages of a session, newest first.

    Busy means the newest assistant message has no ``time.completed``, carries no error and its
    last sign of life (the newest of the message, the session and the newest tool part) is at most
    ``stale_ms`` old. See the module docstring.
    """
    for message in messages:
        if message.role != "assistant":
            continue
        if message.completed_ms is not None or message.errored:
            return TurnFacts(False)
        alive = max(message.updated_ms, message.created_ms, session_updated_ms, parts_updated_ms)
        if now_ms - alive > stale_ms:
            return TurnFacts(False)
        return TurnFacts(True, message.id)
    return TurnFacts(False)


def current_tool(parts: Iterable[PartRow]) -> tuple[str, str | None] | None:
    """``(tool, target)`` of the newest tool part that is still pending or running.

    ``parts`` are newest first, as the adapter reads them.
    """
    for part in parts:
        if part.status in _OPEN_TOOL_STATES:
            return part.tool, part.target
    return None


def task_entries(todos: Iterable[TodoRow]) -> list[dict[str, str]]:
    """The ``facts.tasks`` list: ``{status, content}`` in position order, at most 20."""
    ordered = sorted(todos, key=lambda todo: todo.position)
    return [{"status": todo.status, "content": todo.content} for todo in ordered[:MAX_TASKS]]


def open_task(tasks: Sequence[Mapping[str, str]]) -> str | None:
    """The first ``in_progress`` task (§4.6 rule 4), else ``None``."""
    for task in tasks:
        if task.get("status") == "in_progress":
            return task.get("content") or None
    return None


# --------------------------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcInfo:
    """A running ``opencode`` process: its pid, working directory and start time (epoch)."""

    pid: int
    cwd: str
    started: float | None = None


def assign_sessions(
    procs: Iterable[ProcInfo], sessions: Mapping[str, Sequence[SessionRow]]
) -> dict[int, SessionRow]:
    """``{pid: session}``: each process gets a session of its working directory.

    ``sessions`` maps a normalized directory to its candidate rows. The newest ``n`` rows (by
    ``time_updated``) go to the ``n`` processes in that directory; the processes ordered by start
    time (pid breaks ties) are matched to those rows ordered by creation time. A directory with no
    row gives its processes no session. No session is given to two processes.
    """
    by_dir: dict[str, list[ProcInfo]] = {}
    for proc in procs:
        by_dir.setdefault(norm_dir(proc.cwd), []).append(proc)
    chosen: dict[int, SessionRow] = {}
    for directory, group in by_dir.items():
        rows = sorted(sessions.get(directory, ()), key=lambda r: (-r.updated_ms, r.id))
        newest = sorted(rows[: len(group)], key=lambda r: (r.created_ms, r.id))
        ordered = sorted(
            group, key=lambda p: (p.started is None, p.started if p.started else 0.0, p.pid)
        )
        # With fewer sessions than processes the youngest processes are the ones left without.
        for proc, row in zip(ordered, newest, strict=False):
            chosen[proc.pid] = row
    return chosen


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------


def session_payload(
    row: SessionRow,
    *,
    pid: int,
    key: str | None,
    ancestry: tuple[ProcessRef, ...] | None,
) -> SessionPayload:
    """The ``session`` payload."""
    return SessionPayload(
        kind=KIND,
        native_id=row.id,
        pid=pid,
        cwd=row.directory,
        model=row.model,
        started_at=datetime.fromtimestamp(row.created_ms / 1000, UTC),
        process_key=key,
        version=row.version,
        ancestry=ancestry or None,
    )


def activity_of(
    row: SessionRow,
    turn: TurnFacts,
    tool: tuple[str, str | None] | None,
    todos: Sequence[TodoRow],
) -> SessionActivity:
    """The OpenCode ``SessionActivity`` (§4.6): title, open task and the running tool.

    The tool is only set while a turn is open. The task list travels as ``facts.tasks``.
    """
    tasks = task_entries(todos)
    return SessionActivity(
        state=turn.state,
        current_tool=tool[0] if turn.busy and tool is not None else None,
        current_target=tool[1] if turn.busy and tool is not None else None,
        open_task=open_task(tasks),
        title=row.title,
        facts={"tasks": [dict(task) for task in tasks]} if tasks else None,
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


def todo_snapshot(key: str, todos: Iterable[TodoRow], observed_at: datetime) -> TodoSnapshot:
    """The complete todo list of session ``key`` (``opencode:<session id>``) as read-only todos."""
    return todo_list_snapshot(
        SOURCE,
        TODO_SOURCE,
        key,
        (
            (
                str(todo.position),
                TodoPayload(
                    text=todo.text or todo.content,
                    status=agent_status(todo.status),
                    scope_kind="session",
                    scope_ref=key,
                    link=key,
                ),
            )
            for todo in sorted(todos, key=lambda todo: todo.position)
        ),
        observed_at,
    )
