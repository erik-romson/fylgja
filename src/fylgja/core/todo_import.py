"""Read-only todo importers: reconciliation (``PLAN-final-plan.md`` WP4.3, §4.1 ``todos``).

Importers (repo-root ``TODO.md`` files, agent task lists) read a source *as a whole* and emit one
complete TODOS snapshot per list; :mod:`fylgja.collectors.todo_lists` says how. The writer hands
each one to :func:`reconcile` inside its transaction, and every change reaches the live view.

**Keys.** Each item of a list has a key, and the stored ``source_ref`` is
``<list ref>#<item key>``, unique per import source (``UNIQUE(source, source_ref)``):

* an agent task is keyed by its native task or todo id, so a task renamed in place (same id) is
  updated, never replaced;
* a ``TODO.md`` line has no id. It is keyed by the file path (the list ref), the normalized text
  (checkbox and ``next`` marker stripped, whitespace collapsed) and an ordinal among identical
  lines. **A checkbox or ``next`` change is therefore an update of the same row, and a text edit
  is a removal (the old key goes ``source_absent``) plus a new item (a new row).**

**The rules**, applied to all the rows of one list at once:

* **Upsert.** An item present in the read updates its row's ``text``, ``status``, ``link`` (and
  scope) and ``updated_at`` in place. A row that already says the same is not written.
* **Absent.** A row of the list that a complete read does not name gets ``deleted_at`` and
  ``deleted_reason = 'source_absent'``. A missing file or an ended session's removed task list is
  a complete, empty read, so all its rows go.
* **Restore.** A key that reappears restores its row (``deleted_at`` cleared). It never gets a
  second row.
* **Failed reads never reconcile.** A read that failed emits no snapshot, so nothing changes. A
  snapshot the core cannot take as a whole is refused as a whole, and nothing changes either: an
  item without text or scope, a key with ``#`` or control characters, two items with one key, more
  than :data:`MAX_ITEMS` items, or a read older than what the list already holds (``stale``). An
  incomplete snapshot (``complete=False``) only upserts and restores; it never removes.
* **Ownership.** The import source is the collector's own name or starts with
  ``<collector>-``, and it is never ``fylgja``: a collector reconciles only its own lists and
  never touches Fylgja's own todos. A snapshot that names no list at all (its ``entity_id`` is not
  ``<source>:<ref>``) is not an import; the writer keeps it as a raw event.
* **Sessions.** A session's own task list ends with the session: :func:`close_session_lists` marks
  the imported todos scoped to a session that ended ``source_absent``. That also covers a session
  that ended while the daemon was down (the first complete session read after a restart ends it).
  A later read of the same list restores them.
* **Read-only.** Imported rows change only here: the todo service refuses ``PATCH`` and
  ``DELETE`` on them (:class:`fylgja.core.todos.TodoReadOnly`).

Status follows the todo service: moving to ``done`` stamps ``done_at`` once (the source's own
time when it gives one) and leaving ``done`` clears it. Text is collapsed to one line and cut to
:data:`MAX_TEXT_CHARS`; the ingest gate has already redacted it. Nothing here logs todo text.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Protocol

from fylgja.collectors.todo_lists import (
    ITEM_KEY_PATTERN,
    REF_SEPARATOR,
    owns,
    split_list_entity_id,
)
from fylgja.core.live_view import Change, Collection
from fylgja.models import Snapshot, TodoPayload
from fylgja.store import db, queries
from fylgja.store.rows import ImportedTodoRow, TodoRow

__all__ = [
    "ABSENT_REASON",
    "ACTIVE_STATUSES",
    "MAX_ITEMS",
    "MAX_REF_CHARS",
    "MAX_TEXT_CHARS",
    "NATIVE_SOURCE",
    "SCOPE_KINDS",
    "STATUSES",
    "ImportList",
    "ImportedItem",
    "Reconciled",
    "Refusal",
    "TxLike",
    "clean_text",
    "close_session_lists",
    "is_imported",
    "item_ref",
    "list_of",
    "plan",
    "reconcile",
]

NATIVE_SOURCE: Final = "fylgja"
"""The ``source`` of Fylgja's own todos; every other source is an import."""
SCOPE_KINDS: Final = ("workspace", "project", "session")
STATUSES: Final = ("open", "next", "done")
ACTIVE_STATUSES: Final = frozenset({"open", "next"})
MAX_TEXT_CHARS: Final = 1000
MAX_REF_CHARS: Final = 1024
MAX_ITEMS: Final = 5000
"""More items than this in one list is not a list anyone keeps: the read is refused."""
ABSENT_REASON: Final = "source_absent"

_ELLIPSIS: Final = "…"
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")


class TxLike(Protocol):
    """What :func:`reconcile` needs of the writer transaction (:class:`fylgja.core.writer.Tx`)."""

    connection: sqlite3.Connection

    def changed(self, change: Change) -> None: ...


class Refusal(StrEnum):
    """Why a TODOS snapshot changed nothing."""

    NOT_A_LIST = "not_a_list"
    """The ``entity_id`` is not ``<import source>:<list ref>``: not an import at all."""
    NOT_OWNED = "not_owned"
    """The import source is not the collector's own (or it is ``fylgja``)."""
    BAD_ITEM = "bad_item"
    DUPLICATE_KEY = "duplicate_key"
    TOO_MANY = "too_many_items"
    STALE = "stale"
    """The read is older than the newest change the list already holds."""


@dataclass(frozen=True)
class Reconciled:
    """What :func:`reconcile` did with one snapshot (counts only, never text)."""

    refused: Refusal | None = None
    source: str | None = None
    inserted: int = 0
    updated: int = 0
    restored: int = 0
    removed: int = 0
    unchanged: int = 0


@dataclass(frozen=True)
class ImportedItem:
    """One item of a read, ready to store."""

    ref: str
    """``<list ref>#<item key>``: the row's ``source_ref``."""
    text: str
    status: str
    scope_kind: str
    scope_ref: str
    link: str | None
    created_at: datetime | None
    done_at: datetime | None


@dataclass(frozen=True)
class ImportList:
    """A snapshot validated as one whole list of one import source."""

    source: str
    ref: str
    items: tuple[ImportedItem, ...]
    complete: bool


def is_imported(source: str) -> bool:
    """Is a row of ``source`` an imported (read-only) todo?"""
    return source != NATIVE_SOURCE


def clean_text(text: str) -> str:
    """One line, whitespace collapsed, at most :data:`MAX_TEXT_CHARS` characters."""
    cleaned = " ".join(text.split())
    if len(cleaned) <= MAX_TEXT_CHARS:
        return cleaned
    return cleaned[: MAX_TEXT_CHARS - 1].rstrip() + _ELLIPSIS


def item_ref(list_ref: str, key: str) -> str:
    """The ``source_ref`` of item ``key`` of list ``list_ref``."""
    return f"{list_ref}{REF_SEPARATOR}{key}"


def list_of(source_ref: str) -> str:
    """The list ref of a stored ``source_ref`` (item keys never hold ``#``)."""
    return source_ref.rpartition(REF_SEPARATOR)[0]


def _ref_ok(value: str) -> bool:
    return bool(value.strip()) and len(value) <= MAX_REF_CHARS and _CONTROL.search(value) is None


def _item(list_ref: str, key: str, payload: object) -> ImportedItem | None:
    if not isinstance(payload, TodoPayload) or ITEM_KEY_PATTERN.fullmatch(key) is None:
        return None
    text = clean_text(payload.text)
    kind, scope, link = payload.scope_kind, payload.scope_ref, payload.link
    if not text or kind is None or scope is None or kind not in SCOPE_KINDS:
        return None
    if not _ref_ok(scope) or (link is not None and not _ref_ok(link)):
        return None
    return ImportedItem(
        ref=item_ref(list_ref, key),
        text=text,
        status=payload.status,
        scope_kind=kind,
        scope_ref=scope,
        link=link,
        created_at=payload.created_at,
        done_at=payload.done_at,
    )


def plan(record: Snapshot[Any]) -> ImportList | Refusal:
    """Validate a TODOS snapshot as one whole import list. Pure: no store access."""
    named = split_list_entity_id(record.entity_id)
    if named is None:
        return Refusal.NOT_A_LIST
    source, ref = named
    if not owns(record.source, source):
        return Refusal.NOT_OWNED
    if len(record.items) > MAX_ITEMS:
        return Refusal.TOO_MANY
    items: list[ImportedItem] = []
    keys: set[str] = set()
    for entry in record.items:
        if entry.entity_id in keys:
            return Refusal.DUPLICATE_KEY
        keys.add(entry.entity_id)
        item = _item(ref, entry.entity_id, entry.payload)
        if item is None:
            return Refusal.BAD_ITEM
        items.append(item)
    return ImportList(source=source, ref=ref, items=tuple(items), complete=record.complete)


def _new_id() -> str:
    return str(uuid.uuid4())


def _done_at(
    previous_status: str | None, previous: datetime | None, item: ImportedItem, now: datetime
) -> str | None:
    """``done_at`` after the change: stamped once on reaching ``done``, cleared on leaving it."""
    if item.status != "done":
        return None
    if previous_status == "done" and previous is not None:
        return db.to_db_time(previous)
    return db.to_db_time(item.done_at or now)


def _differs(row: ImportedTodoRow, item: ImportedItem) -> bool:
    return (row.text, row.status, row.link, row.scope_kind, row.scope_ref) != (
        item.text,
        item.status,
        item.link,
        item.scope_kind,
        item.scope_ref,
    )


def _publish(tx: TxLike, row: TodoRow | None) -> None:
    """The live view holds only open and next todos."""
    if row is None:  # pragma: no cover - the row was written in this transaction
        return
    shown = row if row.status in ACTIVE_STATUSES else None
    tx.changed(Change(Collection.TODOS, row.id, shown))


def _insert(tx: TxLike, source: str, item: ImportedItem, now: datetime, todo_id: str) -> TodoRow:
    return queries.insert_todo(
        tx.connection,
        todo_id=todo_id,
        scope_kind=item.scope_kind,
        scope_ref=item.scope_ref,
        text=item.text,
        status=item.status,
        created_at=db.to_db_time(item.created_at or now),
        done_at=_done_at(None, None, item, now),
        source=source,
        source_ref=item.ref,
        link=item.link,
        updated_at=db.to_db_time(now),
    )


def _rewrite(tx: TxLike, row: ImportedTodoRow, item: ImportedItem, now: datetime) -> TodoRow | None:
    return queries.update_imported_todo(
        tx.connection,
        todo_id=row.id,
        scope_kind=item.scope_kind,
        scope_ref=item.scope_ref,
        text=item.text,
        status=item.status,
        link=item.link,
        done_at=_done_at(row.status, row.done_at, item, now),
        at=db.to_db_time(now),
    )


def reconcile(
    tx: TxLike, record: Snapshot[Any], *, new_id: Callable[[], str] = _new_id
) -> Reconciled:
    """Apply one TODOS snapshot to the stored rows of its list (see the module docstring)."""
    planned = plan(record)
    if isinstance(planned, Refusal):
        return Reconciled(refused=planned)
    now = record.observed_at
    stored = [
        row
        for row in queries.list_import_todos(
            tx.connection, source=planned.source, prefix=planned.ref + REF_SEPARATOR
        )
        if list_of(row.source_ref) == planned.ref
    ]
    if any(row.updated_at > now for row in stored):
        return Reconciled(refused=Refusal.STALE, source=planned.source)
    by_ref = {row.source_ref: row for row in stored}
    inserted = updated = restored = unchanged = 0
    for item in planned.items:
        row = by_ref.get(item.ref)
        if row is None:
            _publish(tx, _insert(tx, planned.source, item, now, new_id()))
            inserted += 1
        elif row.deleted_at is not None:
            _publish(tx, _rewrite(tx, row, item, now))
            restored += 1
        elif _differs(row, item):
            _publish(tx, _rewrite(tx, row, item, now))
            updated += 1
        else:
            unchanged += 1
    removed: list[str] = []
    if planned.complete:
        wanted = {item.ref for item in planned.items}
        gone = [r.id for r in stored if r.deleted_at is None and r.source_ref not in wanted]
        removed = queries.mark_todos_absent(tx.connection, ids=gone, at=db.to_db_time(now))
        for todo_id in removed:
            tx.changed(Change(Collection.TODOS, todo_id, None))
    return Reconciled(
        source=planned.source,
        inserted=inserted,
        updated=updated,
        restored=restored,
        removed=len(removed),
        unchanged=unchanged,
    )


def close_session_lists(tx: TxLike, session_key: str, at: str) -> int:
    """The session ended at ``at``: its imported todos are ``source_absent``. Returns how many."""
    closed = queries.close_imported_session_todos(
        tx.connection, session_key=session_key, native_source=NATIVE_SOURCE, at=at
    )
    for todo_id in closed:
        tx.changed(Change(Collection.TODOS, todo_id, None))
    return len(closed)
