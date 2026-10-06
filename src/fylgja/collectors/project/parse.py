"""Pure parsers of the ``project`` collector: repo-root ``TODO.md`` (``PLAN-final-plan.md`` WP4.3).

Everything here is a function over bytes or text the adapter already read: no I/O and no clock.

**What a task is.** A line that is a list item with a checkbox, at any indentation (so nested lists
count): ``-``, ``*`` or ``+``, at least one space, ``[ ]`` (open) or ``[x]``/``[X]`` (done), at
least one space and the text. Everything else is not a task: headings, plain bullets, prose,
``[ ]`` without text or without the space after it, and every line inside a fenced code block
(`````` ``` `````` or ``~~~``).

**The ``next`` marker.** An open task whose text starts with ``next:`` or carries a standalone
``#next`` tag (any case) has status ``next``. A done task stays ``done``. The marker is not part
of the text.

**Keys** (WP4.3). A line has no id, so its key is the normalized text (checkbox and ``next`` marker
stripped, whitespace collapsed) plus an ordinal among identical lines of the file, counted from 0
top to bottom. The key holds a hash of the text, never the text itself (the stored ``source_ref``
is not redacted, the text is), as ``<16 hex digits of SHA-256>:<ordinal>``. So ticking,
unticking or marking a line ``next`` keeps its key (an update), while editing its text gives a new
key (the old item is removed and a new one appears), and moving a line keeps its key.

**A failed read.** :class:`TodoMdError` is raised for bytes that are not UTF-8 text (or hold NUL
bytes), a file larger than :data:`MAX_BYTES` and more than :data:`MAX_ITEMS` tasks. The collector
then emits nothing for the file, so a broken read never wipes its list.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Final

from fylgja.collectors.todo_lists import TodoStatus, todo_list_snapshot
from fylgja.models import TodoPayload, TodoSnapshot

__all__ = [
    "FILE_NAME",
    "IMPORT_SOURCE",
    "MAX_BYTES",
    "MAX_ITEMS",
    "SCOPE_KIND",
    "SOURCE",
    "TodoMdError",
    "TodoMdItem",
    "decode",
    "item_key",
    "normalize",
    "parse_todo_md",
    "split_next",
    "todo_md_path",
    "todo_md_snapshot",
]

SOURCE: Final = "project"
"""The collector's name: the ``source`` of its records."""
IMPORT_SOURCE: Final = "project-todo-md"
"""The ``todos.source`` of an imported ``TODO.md`` line."""
FILE_NAME: Final = "TODO.md"
SCOPE_KIND: Final = "project"
MAX_BYTES: Final = 1 << 20
"""A ``TODO.md`` larger than 1 MiB is not read (a partial read would wipe its tail)."""
MAX_ITEMS: Final = 1000

_TASK: Final = re.compile(r"[ \t]*[-*+][ \t]+\[(?P<box>[ xX])\][ \t]+(?P<text>.*)")
_FENCE: Final = re.compile(r"[ \t]{0,3}(?P<fence>`{3,}|~{3,})")
_NEXT_PREFIX: Final = re.compile(r"(?i)next:")
_NEXT_TAG: Final = re.compile(r"(?i)(?<!\S)#next(?!\S)")
_KEY_DIGITS: Final = 16


class TodoMdError(ValueError):
    """The file cannot be taken as a whole. ``kind`` is a short code, never file content."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class TodoMdItem:
    """One task line of a ``TODO.md``."""

    key: str
    text: str
    """The normalized text: no checkbox, no ``next`` marker, whitespace collapsed."""
    status: TodoStatus
    line: int = field(compare=False)
    """1-based line number. Informational: not part of the key, nor of equality (a moved line is
    the same task)."""


def normalize(text: str) -> str:
    """Whitespace collapsed to single spaces, ends trimmed."""
    return " ".join(text.split())


def split_next(text: str) -> tuple[str, bool]:
    """``(normalized text without the next marker, whether it had one)``."""
    stripped = text.strip()
    marked = False
    prefix = _NEXT_PREFIX.match(stripped)
    if prefix is not None:
        stripped = stripped[prefix.end() :]
        marked = True
    without, tags = _NEXT_TAG.subn("", stripped)
    return normalize(without), marked or tags > 0


def item_key(text: str, ordinal: int) -> str:
    """The key of the ``ordinal``-th line with normalized text ``text``."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:_KEY_DIGITS]
    return f"{digest}:{ordinal}"


def decode(data: bytes) -> str:
    """The text of a ``TODO.md``. Raises :class:`TodoMdError` for anything that is not text."""
    if len(data) > MAX_BYTES:
        raise TodoMdError("todo_md_too_large")
    if b"\x00" in data:
        raise TodoMdError("todo_md_binary")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise TodoMdError("todo_md_not_utf8") from None


def _closes(line: str, fence: str) -> bool:
    """Does ``line`` close a code block opened by ``fence`` (same character, at least as long)?"""
    match = _FENCE.match(line)
    if match is None:
        return False
    run = match["fence"]
    return run[0] == fence[0] and len(run) >= len(fence) and not line[match.end() :].strip()


def _lines_outside_fences(text: str) -> Iterable[tuple[int, str]]:
    """``(line number, line)`` of every line that is not inside a fenced code block."""
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if fence is not None:
            if _closes(line, fence):
                fence = None
            continue
        opened = _FENCE.match(line)
        if opened is not None:
            fence = opened["fence"]
            continue
        yield number, line


def parse_todo_md(text: str) -> list[TodoMdItem]:
    """The task lines of a ``TODO.md``, top to bottom (see the module docstring)."""
    items: list[TodoMdItem] = []
    seen: Counter[str] = Counter()
    for number, line in _lines_outside_fences(text):
        match = _TASK.fullmatch(line)
        if match is None:
            continue
        normalized, marked = split_next(match["text"])
        if not normalized:
            continue
        done = match["box"] in "xX"
        status: TodoStatus = "done" if done else ("next" if marked else "open")
        ordinal = seen[normalized]
        seen[normalized] += 1
        items.append(TodoMdItem(item_key(normalized, ordinal), normalized, status, number))
        if len(items) > MAX_ITEMS:
            raise TodoMdError("todo_md_too_many_items")
    return items


def todo_md_path(root: str) -> str:
    """The repo-root ``TODO.md`` of the project whose repository root is ``root``."""
    return str(PurePosixPath(root) / FILE_NAME)


def todo_md_snapshot(
    path: str, project: str, items: Sequence[TodoMdItem], observed_at: datetime
) -> TodoSnapshot:
    """The complete snapshot of one ``TODO.md``, scoped to ``project`` (its project path).

    The list is the file (``project-todo-md:<path>``) and each item links back to it.
    """
    return todo_list_snapshot(
        SOURCE,
        IMPORT_SOURCE,
        path,
        (
            (
                item.key,
                TodoPayload(
                    text=item.text,
                    status=item.status,
                    scope_kind=SCOPE_KIND,
                    scope_ref=project,
                    link=path,
                ),
            )
            for item in items
        ),
        observed_at,
    )
