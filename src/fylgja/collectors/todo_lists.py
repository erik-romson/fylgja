"""How a collector reports a read-only todo list (``PLAN-final-plan.md`` WP4.3).

An importer reads a todo source *as a whole* and emits one complete
:class:`~fylgja.models.TodoSnapshot` per list it read. The core reconciles the stored rows of
that list with it (:mod:`fylgja.core.todo_import`).

* **The list.** The snapshot's ``entity_id`` is ``<import source>:<list ref>``, for example
  ``project-todo-md:/work/app/TODO.md`` or ``claude-tasks:claude:<session id>``. The import
  source becomes the ``todos.source`` column. It is the collector's own name or starts with
  ``<collector>-``, so a collector only ever reconciles its own lists, and it is never
  ``fylgja`` (Fylgja's own todos).
* **The items.** Each item's ``entity_id`` is its key within the list: a native task id, or
  whatever the importer documents as the item's identity. A key never holds ``#``. The stored
  ``source_ref`` is ``<list ref>#<item key>``, unique per import source.
* **Complete.** ``complete=True`` says every item of the list is in ``items``: an item the
  stored list has and the snapshot lacks is gone from the source. A read that failed (a parse
  error, a permission error, a partial read) is never emitted, so it changes nothing.

Agent task lists share one status vocabulary (``pending``, ``in_progress``, ``completed``,
``cancelled``); :func:`agent_status` maps it to Fylgja's ``open``, ``next`` and ``done``.

This module imports only the standard library and :mod:`fylgja.models`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from typing import Final, Literal

from fylgja.models import SnapshotItem, TodoPayload, TodoSnapshot

__all__ = [
    "IMPORT_SOURCE_PATTERN",
    "ITEM_KEY_PATTERN",
    "LIST_SEPARATOR",
    "MAX_LIST_REF_CHARS",
    "REF_SEPARATOR",
    "TodoStatus",
    "agent_status",
    "list_entity_id",
    "owns",
    "split_list_entity_id",
    "todo_list_snapshot",
]

TodoStatus = Literal["open", "next", "done"]

LIST_SEPARATOR: Final = ":"
"""Between the import source and the list ref in a snapshot's ``entity_id``."""
REF_SEPARATOR: Final = "#"
"""Between the list ref and the item key in a stored ``source_ref``."""

IMPORT_SOURCE_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
ITEM_KEY_PATTERN: Final = re.compile(r"[^#\x00-\x1f\x7f]{1,200}")
MAX_LIST_REF_CHARS: Final = 800
"""A list ref plus ``#`` plus the longest item key stays within the 1024 characters of a ref."""

_NATIVE_SOURCE: Final = "fylgja"
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")

_AGENT_STATUSES: Final[dict[str, TodoStatus]] = {
    "pending": "open",
    "in_progress": "next",
    "completed": "done",
    "cancelled": "done",
}


def list_entity_id(source: str, ref: str) -> str:
    """The ``entity_id`` of the snapshot of list ``ref`` of import source ``source``."""
    return f"{source}{LIST_SEPARATOR}{ref}"


def split_list_entity_id(entity_id: str) -> tuple[str, str] | None:
    """``(import source, list ref)`` of a snapshot ``entity_id``; ``None`` when it names no list.

    The import source must match :data:`IMPORT_SOURCE_PATTERN` and the list ref must be
    non-empty, at most :data:`MAX_LIST_REF_CHARS` long and free of control characters.
    """
    source, separator, ref = entity_id.partition(LIST_SEPARATOR)
    if not separator or IMPORT_SOURCE_PATTERN.fullmatch(source) is None:
        return None
    if not ref.strip() or len(ref) > MAX_LIST_REF_CHARS or _CONTROL.search(ref):
        return None
    return source, ref


def owns(collector: str, source: str) -> bool:
    """May ``collector`` reconcile the lists of import source ``source``?"""
    if source == _NATIVE_SOURCE:
        return False
    return source == collector or source.startswith(f"{collector}-")


def agent_status(status: str | None) -> TodoStatus:
    """An agent task status as a todo status; an unknown status is ``open``.

    ``in_progress`` is ``next`` (the agent works on it now) and ``cancelled`` is ``done`` (it is
    no longer open work).
    """
    return _AGENT_STATUSES.get(status or "", "open")


def todo_list_snapshot(
    collector: str,
    source: str,
    ref: str,
    items: Iterable[tuple[str, TodoPayload]],
    observed_at: datetime,
) -> TodoSnapshot:
    """The complete snapshot of one list: ``items`` are ``(item key, payload)`` pairs."""
    return TodoSnapshot(
        source=collector,
        entity_id=list_entity_id(source, ref),
        observed_at=observed_at,
        items=tuple(SnapshotItem(entity_id=key, payload=payload) for key, payload in items),
        complete=True,
    )
