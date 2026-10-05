"""The ``facts`` block of a Claude session: a pure port of Lidskjalv's ``_SUMMARY_JQ`` (WP1.7).

:class:`FactsAccumulator` folds transcript lines (already decoded objects, oldest first) into the
fields Lidskjalv's program produces, one line at a time, so the transcript tail never rereads a
file. :func:`facts_of` is the one-shot form over a list of lines.

Parity with the jq program (``tests/fixtures/lidskjalv/facts/*.json`` are its recorded output):

- ``title``, ``prompt`` (cut to 300), ``said`` (whitespace collapsed, cut to 800), ``mode``,
  ``branch``, ``version``, ``model``, ``turns``, ``started``, ``last_at``, ``prompts`` (the last
  30 typed prompts, collapsed, no slash commands, each cut to 160), ``tools``, ``files`` (the
  first 20 distinct paths written, sorted);
- like ``last // ""`` in jq, a last value that is ``null`` gives ``""``;
- a user message is a typed prompt unless it is meta, a sidechain, or carries a ``tool_result``.

**Deliberate difference: ``tasks``.** The jq program reports ``todos`` from the last ``TodoWrite``.
``tasks`` has the same ``{status, content}`` shape (at most 20, ``content`` cut to 80) and is the
same list when a transcript has only ``TodoWrite``. It also follows the task tools
(``TaskCreate``/``TaskUpdate``), which is the R7 source: ``~/.claude/tasks/`` no longer exists, so
the transcript's ``tool_use`` records are the only place a task list can be read. A ``TodoWrite``
replaces the list; ``TaskCreate`` appends a ``pending`` task; ``TaskUpdate`` changes the status or
subject of the task with that id (ids are counted from 1 in creation order, as Claude numbers
them) and ``deleted`` removes it.

Where jq would abort on a malformed value (a string where it expects an array, say), the port
ignores the value instead of failing: a live transcript is never trusted.

Nothing here does I/O or logging.
"""

from __future__ import annotations

import json
import re
from collections import Counter, deque
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlsplit

__all__ = [
    "FILE_TOOLS",
    "MAX_OPEN_TOOLS",
    "FactsAccumulator",
    "PendingTool",
    "decode_line",
    "epoch",
    "facts_of",
    "open_task",
    "tool_target",
]

PROMPT_CHARS: Final = 300
SAID_CHARS: Final = 800
PROMPT_ITEM_CHARS: Final = 160
PROMPT_COUNT: Final = 30
FILE_COUNT: Final = 20
TASK_COUNT: Final = 20
TASK_CHARS: Final = 80
MAX_OPEN_TOOLS: Final = 64
"""Tool calls awaiting a result that are remembered; the oldest is forgotten first."""

FILE_TOOLS: Final = frozenset({"Edit", "Write", "NotebookEdit"})
"""Tools whose ``input.file_path`` counts as a file written (as in Lidskjalv)."""

_WHITESPACE: Final = re.compile(r"\s+")
_FRACTION: Final = re.compile(r"\.[0-9]+Z$")
_CREATE_TOOLS: Final = frozenset({"TaskCreate"})
_UPDATE_TOOLS: Final = frozenset({"TaskUpdate"})
_PATH_KEYS: Final = ("file_path", "notebook_path", "path")


def decode_line(line: str | bytes) -> dict[str, Any] | None:
    """One transcript line as an object, or ``None`` (jq: ``fromjson? | select(object)``)."""
    try:
        value = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def epoch(value: object) -> int:
    """jq's ``epoch``: an ISO-8601 UTC instant as epoch seconds, ``0`` when absent or unparsable."""
    if not isinstance(value, str) or value == "":
        return 0
    try:
        parsed = datetime.strptime(_FRACTION.sub("Z", value), "%Y-%m-%dT%H:%M:%SZ")  # noqa: DTZ007
    except ValueError:
        return 0
    return int(parsed.replace(tzinfo=UTC).timestamp())


def _truthy(value: object) -> bool:
    """jq truthiness: only ``null`` and ``false`` are false."""
    return value is not None and value is not False


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _blocks(entry: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """``.message.content`` as a list of block objects (``[]`` when it is anything else)."""
    message = entry.get("message")
    if not isinstance(message, Mapping):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, Mapping)]


def _typed(entry: Mapping[str, Any]) -> str | None:
    """The text of a prompt the person typed, ``None`` for anything else (jq's ``typed``)."""
    if entry.get("type") != "user":
        return None
    if _truthy(entry.get("isMeta")) or _truthy(entry.get("isSidechain")):
        return None
    message = entry.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        blocks = [block for block in content if isinstance(block, Mapping)]
        if any(block.get("type") == "tool_result" for block in blocks):
            return None
        text = " ".join(_text(b.get("text")) for b in blocks if b.get("type") == "text")
    else:
        return None
    return text or None


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text)


def tool_target(name: str, tool_input: object) -> str | None:
    """What a tool call works on: a file path, the head of a command or a URL host.

    The command head is the first word only; arguments can carry secrets and never leave here.
    """
    if not isinstance(tool_input, Mapping):
        return None
    for key in _PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    command = tool_input.get("command")
    if isinstance(command, str) and command.split():
        return command.split()[0]
    url = tool_input.get("url")
    if isinstance(url, str) and url:
        try:
            host = urlsplit(url).hostname
        except ValueError:
            return None
        return host or None
    return None


class PendingTool:
    """A ``tool_use`` that has no ``tool_result`` yet."""

    __slots__ = ("name", "target")

    def __init__(self, name: str, target: str | None) -> None:
        self.name = name
        self.target = target

    def __repr__(self) -> str:
        return f"PendingTool({self.name!r})"


def _task_entry(status: object, content: object) -> dict[str, str]:
    return {
        "status": status if isinstance(status, str) else "",
        "content": _text(content)[:TASK_CHARS],
    }


class FactsAccumulator:
    """Folds transcript lines into the facts block. Feed lines oldest first."""

    def __init__(self) -> None:
        self._title: object = None
        self._prompt: object = None
        self._said: str | None = None
        self._mode: object = None
        self._branch: object = None
        self._version: object = None
        self._model: object = None
        self._turns = 0
        self._first_ts: object = None
        self._last_ts: object = None
        self._prompts: deque[str] = deque(maxlen=PROMPT_COUNT)
        self._tools: Counter[str] = Counter()
        self._files: set[str] = set()
        self._tasks: list[dict[str, str]] = []
        self._task_ids: list[str | None] = []
        self._next_task_id = 1
        self._pending: dict[str, PendingTool] = {}
        self.lines = 0
        """Objects folded in so far."""

    # -- folding ------------------------------------------------------------------------------

    def feed(self, entry: Mapping[str, Any]) -> None:
        self.lines += 1
        kind = entry.get("type")
        if kind == "ai-title":
            self._title = entry.get("aiTitle")
        elif kind == "last-prompt":
            self._prompt = entry.get("lastPrompt")
        elif kind == "mode":
            self._mode = entry.get("mode")
        if entry.get("gitBranch") is not None:
            self._branch = entry.get("gitBranch")
        if entry.get("version") is not None:
            self._version = entry.get("version")
        stamp = entry.get("timestamp")
        if stamp is not None:
            if self._first_ts is None:
                self._first_ts = stamp
            self._last_ts = stamp
        if kind == "assistant":
            self._assistant(entry)
        elif kind == "user":
            self._user(entry)

    def feed_all(self, entries: Iterable[Mapping[str, Any]]) -> None:
        for entry in entries:
            self.feed(entry)

    def _assistant(self, entry: Mapping[str, Any]) -> None:
        message = entry.get("message")
        self._model = message.get("model") if isinstance(message, Mapping) else None
        blocks = _blocks(entry)
        sidechain = _truthy(entry.get("isSidechain"))
        if not sidechain:
            said = " ".join(_text(b.get("text")) for b in blocks if b.get("type") == "text")
            if said != "":
                self._said = said
        for block in blocks:
            if block.get("type") == "tool_use":
                self._tool_use(block, sidechain=sidechain)

    def _tool_use(self, block: Mapping[str, Any], *, sidechain: bool) -> None:
        name = block.get("name")
        tool_input = block.get("input")
        if isinstance(name, str):
            self._tools[name] += 1
            if name in FILE_TOOLS and isinstance(tool_input, Mapping):
                path = tool_input.get("file_path")
                if isinstance(path, str):
                    self._files.add(path)
            self._task_tool(name, tool_input)
        call_id = block.get("id")
        if isinstance(name, str) and isinstance(call_id, str) and not sidechain:
            self._pending[call_id] = PendingTool(name, tool_target(name, tool_input))
            while len(self._pending) > MAX_OPEN_TOOLS:
                del self._pending[next(iter(self._pending))]

    def _user(self, entry: Mapping[str, Any]) -> None:
        for block in _blocks(entry):
            if block.get("type") == "tool_result":
                call_id = block.get("tool_use_id")
                if isinstance(call_id, str):
                    self._pending.pop(call_id, None)
        text = _typed(entry)
        if text is None:
            return
        self._turns += 1
        self._pending.clear()  # a new prompt means whatever was running ended or was interrupted
        collapsed = _collapse(text)
        if not collapsed.startswith("<"):
            self._prompts.append(collapsed[:PROMPT_ITEM_CHARS])

    # -- tasks (R7) ---------------------------------------------------------------------------

    def _task_tool(self, name: str, tool_input: object) -> None:
        if name == "TodoWrite":
            todos = tool_input.get("todos") if isinstance(tool_input, Mapping) else None
            items = todos if isinstance(todos, list) else []
            self._tasks = [
                _task_entry(item.get("status"), item.get("content"))
                for item in items
                if isinstance(item, Mapping)
            ]
            self._task_ids = [None] * len(self._tasks)
            self._next_task_id = 1
        elif name in _CREATE_TOOLS and isinstance(tool_input, Mapping):
            subject = tool_input.get("subject", tool_input.get("content"))
            self._tasks.append(_task_entry("pending", subject))
            self._task_ids.append(str(self._next_task_id))
            self._next_task_id += 1
        elif name in _UPDATE_TOOLS and isinstance(tool_input, Mapping):
            self._update_task(tool_input)

    def _update_task(self, tool_input: Mapping[str, Any]) -> None:
        raw_id = tool_input.get("taskId", tool_input.get("id"))
        if isinstance(raw_id, bool) or not isinstance(raw_id, str | int):
            return
        try:
            index = self._task_ids.index(str(raw_id))
        except ValueError:
            return
        status = tool_input.get("status")
        if status == "deleted":
            del self._tasks[index]
            del self._task_ids[index]
            return
        task = self._tasks[index]
        if isinstance(status, str):
            task["status"] = status
        subject = tool_input.get("subject")
        if isinstance(subject, str):
            task["content"] = subject[:TASK_CHARS]

    # -- results ------------------------------------------------------------------------------

    def facts(self) -> dict[str, Any]:
        """The facts block (a new dict each call)."""
        said = "" if self._said is None else _collapse(self._said)[:SAID_CHARS]
        return {
            "title": _text(self._title),
            "prompt": _text(self._prompt)[:PROMPT_CHARS],
            "said": said,
            "mode": _text(self._mode),
            "branch": _text(self._branch),
            "version": _text(self._version),
            "model": _text(self._model),
            "turns": self._turns,
            "started": epoch(self._first_ts),
            "last_at": epoch(self._last_ts),
            "prompts": list(self._prompts),
            "tools": dict(sorted(self._tools.items())),
            "files": sorted(self._files)[:FILE_COUNT],
            "tasks": [dict(task) for task in self._tasks[:TASK_COUNT]],
        }

    def pending_tool(self) -> PendingTool | None:
        """The newest tool call that has no result yet (running, or waiting for permission)."""
        if not self._pending:
            return None
        return self._pending[next(reversed(self._pending))]


def facts_of(entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The facts block of a whole transcript given as decoded lines, oldest first."""
    accumulator = FactsAccumulator()
    accumulator.feed_all(entries)
    return accumulator.facts()


def open_task(tasks: Sequence[Mapping[str, Any]]) -> str | None:
    """The text of the task in progress (the first one), or ``None``."""
    for task in tasks:
        content = task.get("content")
        if task.get("status") == "in_progress" and isinstance(content, str) and content:
            return content
    return None
