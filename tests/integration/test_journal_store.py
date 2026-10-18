"""The journal's store queries (docs/decisions/0057): round trips, constraints and the two window
queries."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from fylgja import paths
from fylgja.store import db, queries

BASE = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def at(minutes: int) -> str:
    return db.to_db_time(BASE + timedelta(minutes=minutes))


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    opened = db.open_store(paths.database_file())
    try:
        yield opened
    finally:
        opened.close()


def thread_row(thread_id: str = "t1", **over: object) -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "repo_root": "/w/repo",
        "branch": "main",
        "name": "repo",
        "first_at": at(0),
        "last_at": None,
        "watermark": None,
        "where_text": None,
        "where_at": None,
        "last_attempt_at": None,
        "error_kind": None,
        "error_at": None,
        **over,
    }


def entry_row(minute: int, thread_id: str = "t1", **over: object) -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "started_at": at(minute - 5),
        "ended_at": at(minute),
        "trigger": "turn_done",
        "headline": f"entry {minute}",
        "did": json.dumps(["did a thing"]),
        "sources": json.dumps([{"id": "S1", "kind": "thread", "ref": thread_id, "label": "x"}]),
        "input_digest": "d" * 64,
        "model": "haiku",
        "generated_at": at(minute),
        **over,
    }


def item_row(seq: int, thread_id: str = "t1", **over: object) -> dict[str, object]:
    return {
        "id": f"item{seq}",
        "thread_id": thread_id,
        "seq": seq,
        "kind": "next",
        "text": f"do {seq}",
        "status": "open",
        "pinned": 0,
        "edited": 0,
        "source": json.dumps({"kind": "thread", "ref": thread_id, "label": "x"}),
        "created_at": at(seq),
        "created_entry": None,
        "closed_at": None,
        "closed_by": None,
        "closed_entry": None,
        "todo_id": None,
        "how_text": None,
        "how_digest": None,
        "how_at": None,
        "how_error_kind": None,
        **over,
    }


def test_a_thread_round_trips_and_updates(connection: sqlite3.Connection) -> None:
    assert queries.get_journal_thread(connection, "t1") is None
    stored = queries.put_journal_thread(connection, thread_row())
    assert (stored.repo_root, stored.branch, stored.first_at) == ("/w/repo", "main", BASE)
    assert stored.last_at is None
    assert stored.error_kind is None
    changed = queries.put_journal_thread(
        connection,
        thread_row(
            first_at=at(99),
            last_at=at(10),
            watermark=at(10),
            where_text="You are here.",
            where_at=at(10),
            last_attempt_at=at(10),
        ),
    )
    assert changed.first_at == BASE  # an update keeps the creation time
    assert changed.where_text == "You are here."
    assert changed.watermark == BASE + timedelta(minutes=10)
    assert queries.get_journal_thread(connection, "t1") == changed


def test_a_branch_is_unique_per_root(connection: sqlite3.Connection) -> None:
    queries.put_journal_thread(connection, thread_row())
    with pytest.raises(sqlite3.IntegrityError):
        queries.put_journal_thread(connection, thread_row("t2"))


def test_threads_list_by_last_entry(connection: sqlite3.Connection) -> None:
    queries.put_journal_thread(connection, thread_row("old", branch="a", last_at=at(-100)))
    queries.put_journal_thread(connection, thread_row("new", branch="b", last_at=at(5)))
    queries.put_journal_thread(connection, thread_row("none", branch="c"))
    listed = queries.list_journal_threads(connection, since=at(-10))
    assert [row.thread_id for row in listed] == ["new"]
    both = queries.list_journal_threads(connection, since=at(-200))
    assert [row.thread_id for row in both] == ["new", "old"]


def test_entries_round_trip_newest_first(connection: sqlite3.Connection) -> None:
    queries.put_journal_thread(connection, thread_row())
    ids = [queries.insert_journal_entry(connection, entry_row(m)) for m in (10, 30, 20)]
    assert len(set(ids)) == 3
    listed = queries.list_journal_entries(connection, "t1", limit=2)
    assert [row.headline for row in listed] == ["entry 30", "entry 20"]
    assert listed[0].did == ["did a thing"]
    assert listed[0].sources[0]["id"] == "S1"
    assert listed[0].ended_at == BASE + timedelta(minutes=30)
    between = queries.entries_between(connection, "t1", at(10), at(30))
    assert [row.headline for row in between] == ["entry 20", "entry 10"]
    assert queries.list_journal_entries(connection, "other", limit=5) == []


def test_the_trigger_is_checked(connection: sqlite3.Connection) -> None:
    queries.put_journal_thread(connection, thread_row())
    with pytest.raises(sqlite3.IntegrityError):
        queries.insert_journal_entry(connection, entry_row(10, trigger="bogus"))
    with pytest.raises(sqlite3.IntegrityError):
        queries.insert_journal_entry(connection, entry_row(10, did="not json"))


def test_an_entry_needs_its_thread(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        queries.insert_journal_entry(connection, entry_row(10, "missing"))


def test_items_round_trip_and_update(connection: sqlite3.Connection) -> None:
    queries.put_journal_thread(connection, thread_row())
    entry = queries.insert_journal_entry(connection, entry_row(10))
    stored = queries.put_journal_item(connection, item_row(1, created_entry=entry))
    assert stored.status == "open"
    assert stored.pinned is False
    assert stored.edited is False
    assert stored.source == {"kind": "thread", "ref": "t1", "label": "x"}
    assert stored.created_entry == entry
    closed = queries.put_journal_item(
        connection,
        item_row(
            1,
            text="renamed",
            status="done",
            pinned=1,
            edited=1,
            closed_at=at(20),
            closed_by="user",
            closed_entry=entry,
            todo_id="todo-1",
            how_text="a\nb",
            how_digest="h",
            how_at=at(21),
        ),
    )
    assert (closed.text, closed.status, closed.pinned, closed.edited) == (
        "renamed",
        "done",
        True,
        True,
    )
    assert closed.closed_by == "user"
    assert closed.closed_at == BASE + timedelta(minutes=20)
    assert closed.how_text == "a\nb"
    assert queries.get_journal_item(connection, "item1") == closed
    assert queries.get_journal_item(connection, "nope") is None


def test_items_list_by_status_and_link(connection: sqlite3.Connection) -> None:
    queries.put_journal_thread(connection, thread_row())
    queries.put_journal_item(connection, item_row(1))
    queries.put_journal_item(connection, item_row(2, status="dropped", closed_by="user"))
    queries.put_journal_item(connection, item_row(3, todo_id="todo-3"))
    assert [i.seq for i in queries.list_journal_items(connection, "t1")] == [1, 2, 3]
    open_items = queries.list_journal_items(connection, "t1", statuses={"open"})
    assert [i.seq for i in open_items] == [1, 3]
    assert [i.id for i in queries.linked_journal_items(connection)] == ["item3"]


def test_an_item_sequence_is_unique_per_thread(connection: sqlite3.Connection) -> None:
    queries.put_journal_thread(connection, thread_row())
    queries.put_journal_item(connection, item_row(1))
    with pytest.raises(sqlite3.IntegrityError):
        queries.put_journal_item(connection, item_row(1, id="other"))


def test_item_constraints(connection: sqlite3.Connection) -> None:
    queries.put_journal_thread(connection, thread_row())
    for bad in ({"kind": "idea"}, {"status": "gone"}, {"pinned": 2}, {"closed_by": "model2"}):
        with pytest.raises(sqlite3.IntegrityError):
            queries.put_journal_item(connection, item_row(9, **bad))
    with pytest.raises(sqlite3.IntegrityError):
        queries.put_journal_item(connection, item_row(9, source="not json"))


def seed_session(
    connection: sqlite3.Connection, key: str, project_path: str, parent: str | None = None
) -> None:
    connection.execute(
        "INSERT INTO sessions (session_key, kind, native_id, source, project_path, title, "
        "parent_key, first_seen, last_seen) VALUES (?, 'agent', ?, 'src', ?, ?, ?, ?, ?)",
        (key, key, project_path, f"title {key}", parent, at(0), at(0)),
    )


def seed_interval(
    connection: sqlite3.Connection, key: str, minute: int, prompt: str | None = None
) -> None:
    connection.execute(
        "INSERT INTO session_snapshots (session_key, source, valid_from, valid_to, state, "
        "last_prompt, stage) VALUES (?, 'src', ?, ?, 'busy', ?, ?)",
        (key, at(minute), at(minute + 1), prompt, json.dumps({"name": "build"})),
    )


def test_activity_between_returns_the_checkouts_intervals_in_order(
    connection: sqlite3.Connection,
) -> None:
    seed_session(connection, "a", "/w/repo")
    seed_session(connection, "b", "/w/repo", parent="a")
    seed_session(connection, "c", "/w/other")
    for key, minute, prompt in (
        ("a", 5, "before"),
        ("a", 20, "first"),
        ("b", 15, "child"),
        ("c", 16, "elsewhere"),
        ("a", 30, "last"),
        ("a", 40, "after"),
    ):
        seed_interval(connection, key, minute, prompt)
    found = queries.load_activity_between(
        connection, project_path="/w/repo", since=at(5), until=at(30)
    )
    assert [(r.session_key, r.last_prompt) for r in found] == [
        ("b", "child"),
        ("a", "first"),
        ("a", "last"),
    ]
    assert found[0].parent_key == "a"
    assert found[0].kind == "agent"
    assert found[0].title == "title b"
    assert found[0].stage == {"name": "build"}
    assert found[1].valid_from == BASE + timedelta(minutes=20)


def test_todos_between_filters_the_window_deletion_and_links(
    connection: sqlite3.Connection,
) -> None:
    queries.put_journal_thread(connection, thread_row())

    def todo(todo_id: str, minute: int, scope_ref: str = "/w/repo", **over: object) -> None:
        queries.insert_todo(
            connection,
            todo_id=todo_id,
            scope_kind=str(over.get("scope_kind", "project")),
            scope_ref=scope_ref,
            text=f"todo {todo_id}",
            status=str(over.get("status", "open")),
            created_at=at(minute),
            done_at=over.get("done_at"),  # type: ignore[arg-type]
            source="user",
            source_ref=todo_id,
            link=None,
        )

    todo("inside", 10)
    todo("before", -10)
    todo("after", 50)
    todo("deleted", 11)
    todo("linked", 12)
    todo("other", 13, scope_ref="/w/other")
    todo("session", 14, scope_kind="session")
    todo("finished", -30, status="done", done_at=at(15))
    queries.soft_delete_todo(connection, todo_id="deleted", at=at(12))
    queries.put_journal_item(connection, item_row(1, todo_id="linked"))
    found = queries.todos_between(connection, scope_ref="/w/repo", since=at(0), until=at(30))
    assert [row.id for row in found] == ["finished", "inside"]
