"""The todo service (WP4.1): scoping, the focused-workspace default, idempotent ``done``, soft
delete and status transitions, over a real writer and live view."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from api_support import focus_event, workspace_snapshot

from fylgja.core.live_view import LiveView
from fylgja.core.todos import (
    MAX_TEXT_CHARS,
    NATIVE_SOURCE,
    NoFocusedWorkspace,
    TodoNotFound,
    TodoService,
    TodoValidationError,
)
from fylgja.core.writer import Writer
from fylgja.privacy import PrivacyFilter
from fylgja.store.rows import TodoRow
from fylgja.testing.fakes import FakeClock

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


@dataclass
class Rig:
    service: TodoService
    writer: Writer
    view: LiveView
    clock: FakeClock
    database: Path

    async def focus(self, name: str | None) -> None:
        names = ["1", "2", "3"]
        await self.writer.submit_committed(
            workspace_snapshot("wm", names, self.clock.now(), focused=name)
        )

    def row(self, todo_id: str) -> dict[str, Any]:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            found = connection.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
        assert found is not None
        return dict(found)


@pytest.fixture
async def rig(isolated_env: Path) -> AsyncIterator[Rig]:
    clock = FakeClock(T0)
    view = LiveView()
    database = isolated_env / "todos.db"
    writer = Writer(database, clock=clock, on_commit=view.apply)
    await writer.start()
    service = TodoService(
        writer,
        view,
        clock=clock,
        privacy=PrivacyFilter(redact_patterns=[r"sk-[A-Za-z0-9]{8,}"]),
    )
    try:
        yield Rig(service, writer, view, clock, database)
    finally:
        await writer.stop()


# --- scope ------------------------------------------------------------------------------------


async def test_add_without_a_scope_uses_the_focused_workspace(rig: Rig) -> None:
    await rig.focus("2")
    todo = await rig.service.add("write the thing")
    assert (todo.scope_kind, todo.scope_ref) == ("workspace", "2")
    assert todo.status == "open"
    assert todo.source == NATIVE_SOURCE
    assert todo.source_ref is None
    assert str(uuid.UUID(todo.id)) == todo.id
    assert todo.created_at == todo.updated_at == T0
    assert todo.done_at is None
    assert rig.view.todos[todo.id] == todo


async def test_add_without_a_scope_and_without_focus_is_an_error(rig: Rig) -> None:
    assert rig.service.focused_workspace() is None
    with pytest.raises(NoFocusedWorkspace):
        await rig.service.add("nowhere")
    await rig.focus(None)  # workspaces are known, none is focused
    with pytest.raises(NoFocusedWorkspace):
        await rig.service.add("still nowhere")
    assert await rig.service.list_todos() == []


async def test_the_focus_event_is_the_fallback_for_the_default(rig: Rig) -> None:
    await rig.writer.submit_committed(focus_event("wm", "w1", "7", T0))
    assert rig.service.focused_workspace() == "7"
    todo = await rig.service.add("by focus")
    assert todo.scope_ref == "7"


async def test_the_default_follows_the_focus(rig: Rig) -> None:
    await rig.focus("1")
    first = await rig.service.add("one")
    await rig.focus("3")
    second = await rig.service.add("three")
    assert (first.scope_ref, second.scope_ref) == ("1", "3")


async def test_an_explicit_scope_wins_over_the_focus(rig: Rig) -> None:
    await rig.focus("2")
    project = await rig.service.add("p", scope_kind="project", scope_ref="/work/alpha")
    session = await rig.service.add("s", scope_kind="session", scope_ref="claude:abc")
    workspace = await rig.service.add("w", scope_kind="workspace", scope_ref="9", status="next")
    assert [(t.scope_kind, t.scope_ref) for t in (project, session, workspace)] == [
        ("project", "/work/alpha"),
        ("session", "claude:abc"),
        ("workspace", "9"),
    ]
    assert workspace.status == "next"
    assert set(rig.view.todos) == {project.id, session.id, workspace.id}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"scope_kind": "galaxy", "scope_ref": "x"}, "scope must be"),
        ({"scope_kind": "workspace", "scope_ref": "  "}, "reference"),
        ({"scope_kind": "workspace", "scope_ref": "a\nb"}, "reference"),
        ({"scope_kind": "workspace"}, "both"),
        ({"scope_ref": "2"}, "both"),
        ({"status": "done", "scope_kind": "workspace", "scope_ref": "2"}, "open or next"),
        ({"status": "later", "scope_kind": "workspace", "scope_ref": "2"}, "status must be"),
        ({"link": "x" * 2000, "scope_kind": "workspace", "scope_ref": "2"}, "link"),
    ],
)
async def test_a_bad_scope_status_or_link_is_refused(
    rig: Rig, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(TodoValidationError, match=message):
        await rig.service.add("text", **kwargs)
    assert await rig.service.list_todos() == []


# --- text -------------------------------------------------------------------------------------


async def test_the_text_is_one_redacted_line(rig: Rig) -> None:
    todo = await rig.service.add(
        "  call  the\n\tbank with sk-ABCDEFGH12345 today ", scope_kind="workspace", scope_ref="1"
    )
    assert todo.text == "call the bank with [redacted] today"
    assert rig.row(todo.id)["text"] == todo.text


@pytest.mark.parametrize("text", ["", "   \n\t", "x" * (MAX_TEXT_CHARS + 1)])
async def test_an_empty_or_huge_text_is_refused_without_repeating_it(rig: Rig, text: str) -> None:
    with pytest.raises(TodoValidationError) as raised:
        await rig.service.add(text, scope_kind="workspace", scope_ref="1")
    assert text.strip() not in str(raised.value) or not text.strip()
    todo = await rig.service.add("fine", scope_kind="workspace", scope_ref="1")
    with pytest.raises(TodoValidationError):
        await rig.service.update(todo.id, text=text)


async def test_update_changes_the_text_and_bumps_updated_at(rig: Rig) -> None:
    todo = await rig.service.add("old", scope_kind="workspace", scope_ref="1")
    await rig.clock.advance(60)
    changed = await rig.service.update(todo.id, text="new sk-ABCDEFGH12345")
    assert changed.text == "new [redacted]"
    assert changed.updated_at > todo.updated_at
    assert changed.created_at == todo.created_at
    assert rig.view.todos[todo.id].text == "new [redacted]"


# --- status -----------------------------------------------------------------------------------


async def test_done_stamps_done_at_and_leaves_the_live_view(rig: Rig) -> None:
    todo = await rig.service.add("finish", scope_kind="workspace", scope_ref="1")
    await rig.clock.advance(30)
    done = await rig.service.done(todo.id)
    assert done.status == "done"
    assert done.done_at == T0.replace(second=30)
    assert todo.id not in rig.view.todos
    assert (await rig.service.get(todo.id)).status == "done"


async def test_done_is_idempotent_and_keeps_done_at(rig: Rig) -> None:
    todo = await rig.service.add("finish", scope_kind="workspace", scope_ref="1")
    await rig.clock.advance(30)
    first = await rig.service.done(todo.id)
    version = rig.view.version
    await rig.clock.advance(3600)
    again = await rig.service.done(todo.id)
    assert again == first
    assert (again.done_at, again.updated_at) == (first.done_at, first.updated_at)
    assert rig.row(todo.id)["done_at"] == rig.row(todo.id)["updated_at"]
    assert rig.view.version == version  # nothing changed, nothing published
    same_status = await rig.service.update(todo.id, status="done")
    assert same_status.done_at == first.done_at


async def test_status_transitions_in_every_direction(rig: Rig) -> None:
    todo = await rig.service.add("move", scope_kind="workspace", scope_ref="1")
    assert (await rig.service.update(todo.id, status="next")).status == "next"
    assert rig.view.todos[todo.id].status == "next"
    await rig.clock.advance(10)
    assert (await rig.service.update(todo.id, status="done")).done_at is not None
    assert todo.id not in rig.view.todos
    await rig.clock.advance(10)
    reopened = await rig.service.update(todo.id, status="open")
    assert (reopened.status, reopened.done_at) == ("open", None)
    assert rig.view.todos[todo.id].status == "open"
    assert (await rig.service.update(todo.id, status="next")).status == "next"
    assert (await rig.service.update(todo.id, status="done")).status == "done"
    back = await rig.service.update(todo.id, status="next")
    assert (back.status, back.done_at) == ("next", None)


async def test_a_status_outside_the_set_is_refused(rig: Rig) -> None:
    todo = await rig.service.add("x", scope_kind="workspace", scope_ref="1")
    with pytest.raises(TodoValidationError):
        await rig.service.update(todo.id, status="blocked")
    with pytest.raises(TodoValidationError):
        await rig.service.list_todos(statuses=["blocked"])
    assert (await rig.service.get(todo.id)).status == "open"


async def test_an_unknown_todo_is_not_found(rig: Rig) -> None:
    for call in (
        rig.service.done("nope"),
        rig.service.update("nope", text="x"),
        rig.service.delete("nope"),
        rig.service.get("nope"),
    ):
        with pytest.raises(TodoNotFound):
            await call


# --- soft delete ------------------------------------------------------------------------------


async def test_delete_is_soft_and_final(rig: Rig) -> None:
    keep = await rig.service.add("keep", scope_kind="workspace", scope_ref="1")
    gone = await rig.service.add("gone", scope_kind="workspace", scope_ref="1")
    await rig.clock.advance(5)
    await rig.service.delete(gone.id)
    stored = rig.row(gone.id)
    assert stored["deleted_reason"] == "user"
    assert stored["deleted_at"] == "2026-10-05T12:00:05.000000Z"
    assert stored["text"] == "gone"  # kept forever, hidden everywhere
    assert set(rig.view.todos) == {keep.id}
    assert [t.id for t in await rig.service.list_todos()] == [keep.id]
    assert [t.id for t in await rig.service.list_todos(statuses=["open", "next", "done"])] == [
        keep.id
    ]
    for call in (
        rig.service.delete(gone.id),
        rig.service.done(gone.id),
        rig.service.update(gone.id, text="back"),
        rig.service.get(gone.id),
    ):
        with pytest.raises(TodoNotFound):
            await call


async def test_a_done_todo_can_be_deleted(rig: Rig) -> None:
    todo = await rig.service.add("x", scope_kind="workspace", scope_ref="1")
    await rig.service.done(todo.id)
    await rig.service.delete(todo.id)
    assert await rig.service.list_todos() == []
    assert rig.row(todo.id)["deleted_reason"] == "user"


# --- reading ----------------------------------------------------------------------------------


async def test_list_filters_by_scope_and_status_oldest_first(rig: Rig) -> None:
    a = await rig.service.add("a", scope_kind="workspace", scope_ref="1")
    await rig.clock.advance(1)
    b = await rig.service.add("b", scope_kind="workspace", scope_ref="2", status="next")
    await rig.clock.advance(1)
    c = await rig.service.add("c", scope_kind="project", scope_ref="1")
    await rig.clock.advance(1)
    d = await rig.service.add("d", scope_kind="workspace", scope_ref="1")
    await rig.service.done(d.id)

    def ids(rows: list[TodoRow]) -> list[str]:
        return [r.id for r in rows]

    assert ids(await rig.service.list_todos()) == [a.id, b.id, c.id, d.id]
    assert ids(await rig.service.list_todos(scope_kind="workspace")) == [a.id, b.id, d.id]
    assert ids(await rig.service.list_todos(scope_kind="workspace", scope_ref="1")) == [a.id, d.id]
    assert ids(await rig.service.list_todos(scope_ref="1")) == [a.id, c.id, d.id]
    assert ids(await rig.service.list_todos(statuses=["next"])) == [b.id]
    assert ids(await rig.service.list_todos(statuses=["open", "done"])) == [a.id, c.id, d.id]


async def test_the_live_view_is_rebuilt_from_the_store(rig: Rig) -> None:
    open_ = await rig.service.add("open", scope_kind="workspace", scope_ref="1")
    done = await rig.service.add("done", scope_kind="workspace", scope_ref="1")
    gone = await rig.service.add("gone", scope_kind="workspace", scope_ref="1")
    await rig.service.done(done.id)
    await rig.service.delete(gone.id)
    from fylgja.store import db

    fresh = LiveView()
    connection = db.connect_reader(rig.database)
    try:
        fresh.rebuild(connection)
    finally:
        connection.close()
    assert set(fresh.todos) == {open_.id}
