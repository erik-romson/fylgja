"""Fylgja's own todos (WP4.1, ``PLAN-final-plan.md`` §4.1 ``todos``, §4.2, A7; ``PLAN.md`` §8).

A todo has a scope (a workspace, a project or a session), a text, and one of three statuses:
``open``, ``next`` and ``done``. Native items have ``source = "fylgja"``; importers (WP4.3) write
their own source, and the unique ``(source, source_ref)`` pair keeps an import from duplicating.

* **Imported todos are read-only** (WP4.3). Their source owns them and every read reconciles them
  (:mod:`fylgja.core.todo_import`), so ``update`` and ``delete`` refuse them with
  :class:`TodoReadOnly`; the change belongs in the source (the ``TODO.md`` line, the agent's list).

* **One writer.** Every change runs as a job in the daemon's single writer
  (:meth:`~fylgja.core.writer.Writer.execute`), reports the changed row to the live view, and is
  committed before the call returns. The live view holds the **open and next** items; ``/now``
  is built from them. Reads that need ``done`` items (``GET /todos``) open a read-only connection.
* **Scope.** ``add`` without a scope uses the focused AeroSpace workspace of the live view, and
  fails with :class:`NoFocusedWorkspace` when there is none.
* **Status.** Any status may change to any other. Moving to ``done`` stamps ``done_at`` once;
  marking a done todo done again changes nothing (``done_at`` and ``updated_at`` stay), and
  moving a done todo back to ``open`` or ``next`` clears ``done_at``.
* **Soft delete.** ``delete`` sets ``deleted_at`` and ``deleted_reason = 'user'``; the row stays
  (todos are kept forever) but no read returns it, and a second delete says "not found".
* **Privacy (A5).** The text is redacted with the configured patterns before it is stored, and
  it is never logged.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from collections.abc import Callable, Collection
from datetime import datetime

from fylgja.collectors.base import Clock
from fylgja.core.live_view import Change, LiveView
from fylgja.core.live_view import Collection as LiveCollection
from fylgja.core.todo_import import (
    ACTIVE_STATUSES,
    MAX_REF_CHARS,
    MAX_TEXT_CHARS,
    NATIVE_SOURCE,
    SCOPE_KINDS,
    STATUSES,
    is_imported,
)
from fylgja.core.writer import Tx, Writer
from fylgja.privacy import PrivacyFilter
from fylgja.store import db, queries
from fylgja.store.rows import TodoRow

__all__ = [
    "ACTIVE_STATUSES",
    "MAX_REF_CHARS",
    "MAX_TEXT_CHARS",
    "NATIVE_SOURCE",
    "SCOPE_KINDS",
    "STATUSES",
    "NoFocusedWorkspace",
    "TodoError",
    "TodoNotFound",
    "TodoReadOnly",
    "TodoService",
    "TodoValidationError",
]

logger = logging.getLogger("fylgja.todos")


class TodoError(Exception):
    """Base of what the service refuses."""


class TodoNotFound(TodoError):
    """No such todo, or it was deleted."""


class TodoReadOnly(TodoError):
    """The todo is imported: only its source changes it (WP4.3)."""

    def __init__(self, source: str) -> None:
        super().__init__(
            f"this todo is imported from {source} and is read-only in Fylgja; "
            "change it where it comes from"
        )
        self.source = source


class NoFocusedWorkspace(TodoError):
    """``add`` had no scope and the live view has no focused workspace."""

    def __init__(self) -> None:
        super().__init__("no workspace is focused; give a workspace, a project or a session")


class TodoValidationError(TodoError):
    """The text, status or scope is not acceptable (the message never repeats the text)."""


def new_todo_id() -> str:
    return str(uuid.uuid4())


def _clean_text(text: str, privacy: PrivacyFilter) -> str:
    cleaned = " ".join(text.split())  # one line: no control characters, no stray whitespace
    if not cleaned:
        raise TodoValidationError("the todo text is empty")
    if len(cleaned) > MAX_TEXT_CHARS:
        raise TodoValidationError(f"the todo text is longer than {MAX_TEXT_CHARS} characters")
    return privacy.redact(cleaned)


def _check_status(status: str) -> None:
    if status not in STATUSES:
        raise TodoValidationError(f"status must be one of {', '.join(STATUSES)}")


def _check_scope(kind: str, ref: str) -> None:
    if kind not in SCOPE_KINDS:
        raise TodoValidationError(f"scope must be one of {', '.join(SCOPE_KINDS)}")
    if not ref.strip() or len(ref) > MAX_REF_CHARS or any(ord(c) < 32 for c in ref):
        raise TodoValidationError(f"the {kind} reference is empty, too long or has control bytes")


class TodoService:
    """See the module docstring."""

    def __init__(
        self,
        writer: Writer,
        view: LiveView,
        *,
        clock: Clock,
        privacy: PrivacyFilter | None = None,
        new_id: Callable[[], str] = new_todo_id,
    ) -> None:
        self._writer = writer
        self._view = view
        self._clock = clock
        self._privacy = PrivacyFilter() if privacy is None else privacy
        self._new_id = new_id

    # -- the default scope ------------------------------------------------------------------

    def focused_workspace(self) -> str | None:
        """The focused AeroSpace workspace of the live view, or ``None``.

        A workspace row flagged focused wins; otherwise the latest focus event's workspace.
        """
        flagged = sorted(row.name for row in self._view.workspaces.values() if row.focused)
        if flagged:
            return flagged[0]
        focus = self._view.focused
        if focus is not None and focus.workspace:
            return focus.workspace
        return None

    # -- writing ----------------------------------------------------------------------------

    async def add(
        self,
        text: str,
        *,
        scope_kind: str | None = None,
        scope_ref: str | None = None,
        status: str = "open",
        link: str | None = None,
    ) -> TodoRow:
        """Create a native todo. Without a scope it belongs to the focused workspace."""
        cleaned = _clean_text(text, self._privacy)
        _check_status(status)
        if status == "done":
            raise TodoValidationError("a new todo is open or next")
        if (scope_kind is None) != (scope_ref is None):
            raise TodoValidationError("a scope needs both its kind and its reference")
        if scope_kind is None or scope_ref is None:
            workspace = self.focused_workspace()
            if workspace is None:
                raise NoFocusedWorkspace
            scope_kind, scope_ref = "workspace", workspace
        _check_scope(scope_kind, scope_ref)
        if link is not None and (len(link) > MAX_REF_CHARS or any(ord(c) < 32 for c in link)):
            raise TodoValidationError("the link is too long or has control bytes")
        todo_id = self._new_id()
        stamp = db.to_db_time(self._clock.now())

        def job(tx: Tx) -> TodoRow:
            row = queries.insert_todo(
                tx.connection,
                todo_id=todo_id,
                scope_kind=scope_kind,
                scope_ref=scope_ref,
                text=cleaned,
                status=status,
                created_at=stamp,
                done_at=None,
                source=NATIVE_SOURCE,
                source_ref=None,
                link=link,
            )
            _publish(tx, row)
            return row

        return await self._writer.execute(job)

    async def update(
        self, todo_id: str, *, text: str | None = None, status: str | None = None
    ) -> TodoRow:
        """Change the text and/or the status; a no-op change writes nothing.

        An imported todo is refused with :class:`TodoReadOnly`.
        """
        cleaned = None if text is None else _clean_text(text, self._privacy)
        if status is not None:
            _check_status(status)
        now = self._clock.now()

        def job(tx: Tx) -> TodoRow:
            current = queries.get_todo(tx.connection, todo_id)
            if current is None:
                raise TodoNotFound(todo_id)
            if is_imported(current.source):
                raise TodoReadOnly(current.source)
            new_text = current.text if cleaned is None else cleaned
            new_status = current.status if status is None else status
            if new_text == current.text and new_status == current.status:
                return current  # idempotent: `done` on a done todo keeps its done_at
            done_at = _done_at(current, new_status, now)
            row = queries.update_todo(
                tx.connection,
                todo_id=todo_id,
                text=new_text,
                status=new_status,
                done_at=None if done_at is None else db.to_db_time(done_at),
                at=db.to_db_time(now),
            )
            if row is None:  # pragma: no cover - the row was read in this transaction
                raise TodoNotFound(todo_id)
            _publish(tx, row)
            return row

        return await self._writer.execute(job)

    async def done(self, todo_id: str) -> TodoRow:
        """Mark the todo done (idempotent)."""
        return await self.update(todo_id, status="done")

    async def delete(self, todo_id: str) -> None:
        """Soft-delete the todo (``deleted_reason = 'user'``); an imported one is refused."""
        stamp = db.to_db_time(self._clock.now())

        def job(tx: Tx) -> None:
            current = queries.get_todo(tx.connection, todo_id)
            if current is not None and is_imported(current.source):
                raise TodoReadOnly(current.source)
            if not queries.soft_delete_todo(tx.connection, todo_id=todo_id, at=stamp):
                raise TodoNotFound(todo_id)
            tx.changed(Change(LiveCollection.TODOS, todo_id, None))

        await self._writer.execute(job)

    # -- reading ----------------------------------------------------------------------------

    async def get(self, todo_id: str) -> TodoRow:
        row = await asyncio.to_thread(self._read, lambda c: queries.get_todo(c, todo_id))
        if row is None:
            raise TodoNotFound(todo_id)
        return row

    async def list_todos(
        self,
        *,
        scope_kind: str | None = None,
        scope_ref: str | None = None,
        statuses: Collection[str] = (),
        order: str = "created",
        limit: int | None = None,
    ) -> list[TodoRow]:
        """Todos that are not deleted; with no statuses, every status.

        Oldest first by default; ``order="done_desc"`` puts the latest finished first. ``limit``
        caps the answer.
        """
        for status in statuses:
            _check_status(status)
        return await asyncio.to_thread(
            self._read,
            lambda c: queries.list_todos(
                c,
                scope_kind=scope_kind,
                scope_ref=scope_ref,
                statuses=statuses,
                order=order,
                limit=limit,
            ),
        )

    def _read[T](self, query: Callable[[sqlite3.Connection], T]) -> T:
        connection = db.connect_reader(self._writer.database)
        try:
            return query(connection)
        finally:
            connection.close()


def _done_at(current: TodoRow, new_status: str, now: datetime) -> datetime | None:
    if new_status != "done":
        return None
    return current.done_at or now


def _publish(tx: Tx, row: TodoRow) -> None:
    """The live view holds only open and next todos."""
    shown = row if row.status in ACTIVE_STATUSES else None
    tx.changed(Change(LiveCollection.TODOS, row.id, shown))
