"""Agent task lists as read-only todos (WP4.3): the Claude task list from the transcript
(``claude-tasks``) and OpenCode's ``todo`` table (``opencode-todo``), each the session's own list.

The real collectors poll their fixtures and their records go into a real writer and store: a task
renamed in place (same id) is updated, not replaced; a session that ends empties its list (also
when it ended while nobody watched); a list that cannot be read leaves the stored tasks alone; and
OpenCode ``todo`` rows map to the session scope. Through the daemon, the gate accepts the lists of
a plugin with the ``TODOS`` capability, ``/now`` shows them under their session, and ``PATCH`` and
``DELETE`` on them are refused with 409.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from api_support import (
    BASE_URL,
    Synthetic,
    activity_snapshot,
    make_daemon,
    session,
    session_snapshot,
)
from api_support import T0 as DAEMON_T0
from claude_support import World
from opencode_support import PGREP, Rig, build_db, script_processes
from todo_import_support import Store, open_store
from transcript_support import append, assistant, line, tool_use, user

from fylgja.api.app import create_app
from fylgja.collectors.base import Capability
from fylgja.collectors.claude import transcript
from fylgja.collectors.claude.collector import ClaudeSettings
from fylgja.collectors.claude.facts import FactsAccumulator
from fylgja.collectors.opencode import adapter
from fylgja.collectors.todo_lists import agent_status, list_entity_id
from fylgja.models import (
    Record,
    SessionActivity,
    SnapshotItem,
    TodoPayload,
    TodoSnapshot,
)
from fylgja.testing.fakes import FakeProcessRunner

CWD = "/work/agent-app"
SID = "00000000-0000-4000-8000-000000004201"
KEY = f"claude:{SID}"
CLAUDE = "claude-tasks"
OPENCODE = "opencode-todo"


@pytest.fixture
async def store(isolated_env: Path) -> AsyncIterator[Store]:
    async with open_store(isolated_env) as opened:
        yield opened


def result(call_id: str, text: str) -> str:
    block = {"type": "tool_result", "tool_use_id": call_id, "content": text}
    return line(type="user", message={"role": "user", "content": [block]})


def todos_of(records: list[Record]) -> list[TodoSnapshot]:
    return [r for r in records if isinstance(r, TodoSnapshot)]


class ClaudeSession:
    """One live Claude session with a transcript, polled into a store.

    The collector keeps its own fake clock; the store only stamps when it received a record.
    """

    def __init__(self, world: World, store: Store) -> None:
        self.world = world
        self.store = store
        self.seen = 0
        directory = world.registry.parent / "projects" / transcript.project_slug(CWD)
        directory.mkdir(parents=True, exist_ok=True)
        self.transcript = directory / f"{SID}.jsonl"

    async def poll(self) -> list[Record]:
        await self.world.clock.advance(2.0)
        await self.world.collector.poll()
        fresh = self.world.sink.records[self.seen :]
        self.seen = len(self.world.sink.records)
        await self.store.apply(*fresh)
        return fresh


@pytest.fixture
async def claude(tmp_path: Path, store: Store) -> ClaudeSession:
    world = World(tmp_path / "claude" / "sessions")
    world.add(4201, SID, cwd=CWD, status="busy")
    return ClaudeSession(world, store)


# --- Claude ------------------------------------------------------------------------------------


async def test_a_claude_task_renamed_in_place_is_updated_not_replaced(
    claude: ClaudeSession,
) -> None:
    append(claude.transcript, user("plan the migration"))
    append(
        claude.transcript,
        assistant(
            tool_use("tu-1", "TaskCreate", subject="write the schema", description="d"),
            tool_use("tu-2", "TaskCreate", subject="backfill the table", description="d"),
        ),
    )
    append(claude.transcript, result("tu-1", "Task #1 created successfully: write the schema"))
    append(claude.transcript, result("tu-2", "Task #2 created successfully: backfill the table"))
    records = await claude.poll()
    (snapshot,) = todos_of(records)
    assert snapshot.entity_id == list_entity_id(CLAUDE, KEY)
    assert snapshot.complete is True
    assert [i.entity_id for i in snapshot.items] == ["task-1", "task-2"]
    first = {row["text"]: row for row in claude.store.present(CLAUDE)}
    assert sorted(first) == ["backfill the table", "write the schema"]
    for row in first.values():
        assert (row["scope_kind"], row["scope_ref"], row["link"]) == ("session", KEY, KEY)
        assert row["status"] == "open"
    assert first["write the schema"]["source_ref"] == f"{KEY}#task-1"

    append(
        claude.transcript,
        assistant(
            tool_use("tu-3", "TaskUpdate", taskId="1", subject="write the new schema"),
            tool_use("tu-4", "TaskUpdate", taskId="2", status="in_progress"),
        ),
    )
    records = await claude.poll()
    assert len(todos_of(records)) == 1
    rows = claude.store.rows(CLAUDE)
    assert len(rows) == 2  # renamed in place: no second row
    renamed = {row["source_ref"]: row for row in rows}
    assert renamed[f"{KEY}#task-1"]["id"] == first["write the schema"]["id"]
    assert renamed[f"{KEY}#task-1"]["text"] == "write the new schema"
    assert renamed[f"{KEY}#task-1"]["deleted_at"] is None
    assert renamed[f"{KEY}#task-2"]["status"] == "next"
    assert claude.store.shown()[renamed[f"{KEY}#task-2"]["id"]] == ("backfill the table", "next")

    assert todos_of(await claude.poll()) == []  # unchanged: no list is emitted again

    append(
        claude.transcript, assistant(tool_use("tu-5", "TaskUpdate", taskId="2", status="deleted"))
    )
    await claude.poll()
    gone = {row["source_ref"]: row for row in claude.store.rows(CLAUDE)}[f"{KEY}#task-2"]
    assert gone["deleted_reason"] == "source_absent"


async def test_a_todowrite_list_is_keyed_by_position(claude: ClaudeSession) -> None:
    todos = [
        {"content": "read the code", "status": "completed", "activeForm": "Reading"},
        {"content": "fix the bug", "status": "in_progress", "activeForm": "Fixing"},
        {"content": "", "status": "pending", "activeForm": "Nothing"},
    ]
    append(claude.transcript, assistant(tool_use("tu-1", "TodoWrite", todos=todos)))
    (snapshot,) = todos_of(await claude.poll())
    assert [(i.entity_id, i.payload.status) for i in snapshot.items] == [
        ("todo-0", "done"),
        ("todo-1", "next"),
    ]  # a task without text is not a todo
    fix = {row["text"]: row["id"] for row in claude.store.present(CLAUDE)}["fix the bug"]

    todos[1] = {"content": "fix the bug properly", "status": "in_progress", "activeForm": "F"}
    append(claude.transcript, assistant(tool_use("tu-2", "TodoWrite", todos=todos[:2])))
    await claude.poll()
    rows = {row["source_ref"]: row for row in claude.store.rows(CLAUDE)}
    assert rows[f"{KEY}#todo-1"]["id"] == fix
    assert rows[f"{KEY}#todo-1"]["text"] == "fix the bug properly"
    assert len(rows) == 2


async def test_a_claude_session_that_ends_empties_its_list(claude: ClaudeSession) -> None:
    append(claude.transcript, assistant(tool_use("tu-1", "TaskCreate", subject="ship it")))
    await claude.poll()
    (row,) = claude.store.present(CLAUDE)
    assert row["id"] in claude.store.shown()

    (claude.world.registry / "4201.json").unlink()  # the session ended
    records = await claude.poll()
    (empty,) = todos_of(records)
    assert empty.entity_id == list_entity_id(CLAUDE, KEY)
    assert (empty.complete, empty.items) == (True, ())
    (ended,) = claude.store.rows(CLAUDE)
    assert (ended["id"], ended["deleted_reason"]) == (row["id"], "source_absent")
    assert claude.store.shown() == {}
    assert todos_of(await claude.poll()) == []  # said once


async def test_a_session_that_ended_unwatched_still_loses_its_list(
    claude: ClaudeSession, tmp_path: Path
) -> None:
    append(claude.transcript, assistant(tool_use("tu-1", "TaskCreate", subject="ship it")))
    await claude.poll()
    assert len(claude.store.present(CLAUDE)) == 1

    # The daemon restarts and the session is gone: the new collector never emitted its list, but
    # the first complete session read ends the stored session, and its list ends with it.
    restarted = World(tmp_path / "restarted" / "sessions")
    await restarted.clock.advance(60.0)  # later than anything the first run stored
    sink_records = restarted.sink.records
    await restarted.collector.poll()
    assert todos_of(sink_records) == []
    await claude.store.apply(*sink_records)
    (row,) = claude.store.rows(CLAUDE)
    assert row["deleted_reason"] == "source_absent"
    assert claude.store.shown() == {}


async def test_a_transcript_that_cannot_be_read_leaves_the_tasks_alone(
    claude: ClaudeSession,
) -> None:
    append(claude.transcript, assistant(tool_use("tu-1", "TaskCreate", subject="ship it")))
    await claude.poll()
    before = claude.store.rows(CLAUDE)
    claude.transcript.unlink()
    claude.transcript.mkdir()  # still there, but not a file any more
    records = await claude.poll()
    assert todos_of(records) == []
    assert claude.store.rows(CLAUDE) == before


async def test_a_transcript_read_over_several_polls_emits_its_list_only_when_whole(
    tmp_path: Path, store: Store
) -> None:
    world = World(
        tmp_path / "claude" / "sessions",
        settings=ClaudeSettings(transcript_max_read_bytes=4096),
    )
    world.add(4201, SID, cwd=CWD, status="busy")
    session = ClaudeSession(world, store)
    append(session.transcript, assistant(tool_use("tu-1", "TaskCreate", subject="early task")))
    for n in range(40):  # about 10 KiB of chatter: three reads of 4 KiB
        append(session.transcript, user(f"line {n} " + "x" * 200))
    todos = [{"content": "the final list", "status": "pending", "activeForm": "Listing"}]
    append(session.transcript, assistant(tool_use("tu-2", "TodoWrite", todos=todos)))

    emitted: list[TodoSnapshot] = []
    for _ in range(5):
        emitted += todos_of(await session.poll())
    (whole,) = emitted  # nothing while the backlog lasted, then the whole list once
    assert [(i.entity_id, i.payload.text) for i in whole.items] == [("todo-0", "the final list")]
    assert [row["text"] for row in store.rows(CLAUDE)] == ["the final list"]


def test_the_task_create_result_names_the_id() -> None:
    facts = FactsAccumulator()
    facts.feed_all(
        [
            {
                "type": "assistant",
                "message": {"content": [tool_use("c1", "TaskCreate", subject="a")]},
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "c1",
                            "content": [{"type": "text", "text": "Task #7 created successfully"}],
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        tool_use("c2", "TaskCreate", subject="b"),
                        tool_use("c3", "TaskUpdate", taskId=7, status="completed"),
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "c2", "content": "Task #7 created"}
                    ]
                },
            },
        ]
    )
    tasks = facts.task_list()
    # #7 came back for the second task too: never two tasks with one id, so it keeps its count
    assert [(t.key, t.status, t.content) for t in tasks] == [
        ("task-7", "completed", "a"),
        ("task-8", "pending", "b"),
    ]
    assert facts.facts()["tasks"] == [
        {"status": "completed", "content": "a"},
        {"status": "pending", "content": "b"},
    ]


def test_the_whole_task_text_is_kept_for_the_import() -> None:
    long = "x" * 300
    facts = FactsAccumulator()
    facts.feed(
        {"type": "assistant", "message": {"content": [tool_use("c1", "TaskCreate", subject=long)]}}
    )
    assert facts.task_list()[0].content == long
    assert facts.facts()["tasks"][0]["content"] == "x" * 80  # the facts block is unchanged


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("pending", "open"),
        ("in_progress", "next"),
        ("completed", "done"),
        ("cancelled", "done"),
        ("weird", "open"),
        (None, "open"),
    ],
)
def test_agent_statuses(status: str | None, expected: str) -> None:
    assert agent_status(status) == expected


# --- OpenCode ------------------------------------------------------------------------------------

OC1 = "opencode:ses_0000000001"


async def _opencode_poll(rig: Rig, store: Store, start: int) -> tuple[list[Record], int]:
    await rig.collector.poll()
    fresh = rig.sink.records[start:]
    await store.apply(*fresh)
    return fresh, len(rig.sink.records)


async def test_opencode_todo_rows_map_to_the_session_scope(tmp_path: Path, store: Store) -> None:
    db_path = build_db(tmp_path / "opencode.db")
    rig = Rig(db_path)
    script_processes(rig.runner)
    records, seen = await _opencode_poll(rig, store, 0)
    lists = {s.entity_id: s for s in todos_of(records)}
    assert set(lists) == {
        list_entity_id(OPENCODE, OC1),
        list_entity_id(OPENCODE, "opencode:ses_0000000002"),
    }
    rows = {row["source_ref"]: row for row in store.present(OPENCODE)}
    assert {ref: (row["text"], row["status"]) for ref, row in rows.items()} == {
        f"{OC1}#0": ("Add the page and limit parameters", "done"),
        f"{OC1}#1": ("Write the pagination tests", "next"),
        f"{OC1}#2": ("Update the API documentation", "open"),
    }
    for row in rows.values():
        assert (row["scope_kind"], row["scope_ref"], row["link"]) == ("session", OC1, OC1)
    renamed_id = rows[f"{OC1}#1"]["id"]

    # the agent renames the task at position 1 (todowrite rewrites the list)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            "UPDATE todo SET content = ?, status = ? WHERE session_id = ? AND position = 1",
            ("Write the pagination and sorting tests", "completed", "ses_0000000001"),
        )
        connection.commit()
    records, seen = await _opencode_poll(rig, store, seen)
    assert [s.entity_id for s in todos_of(records)] == [list_entity_id(OPENCODE, OC1)]
    after = {row["source_ref"]: row for row in store.rows(OPENCODE)}
    assert len(after) == 3
    assert after[f"{OC1}#1"]["id"] == renamed_id
    assert (after[f"{OC1}#1"]["text"], after[f"{OC1}#1"]["status"]) == (
        "Write the pagination and sorting tests",
        "done",
    )

    # a database that cannot be read changes nothing
    before = store.rows(OPENCODE)
    db_path.write_bytes(b"corrupt" * 100)
    records, seen = await _opencode_poll(rig, store, seen)
    assert todos_of(records) == []
    assert store.rows(OPENCODE) == before

    # the processes end: the sessions end and their lists with them
    ended = FakeProcessRunner()
    ended.script(PGREP, returncode=1)
    rig.collector._seams = adapter.Seams(ended, adapter.LocalFileSystem())  # noqa: SLF001
    records, seen = await _opencode_poll(rig, store, seen)
    emptied = todos_of(records)
    assert sorted(s.entity_id for s in emptied) == sorted(lists)
    assert all(s.complete and s.items == () for s in emptied)
    assert store.present(OPENCODE) == []
    assert {row["deleted_reason"] for row in store.rows(OPENCODE)} == {"source_absent"}
    assert rig.collector.task_emissions == 5


# --- through the daemon: gate, /now and the read-only API ------------------------------------

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"
AUTH = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}


def _agent_list(items: list[tuple[str, str, str]]) -> TodoSnapshot:
    return TodoSnapshot(
        source="agents",
        entity_id=list_entity_id("agents-tasks", "claude:s-1"),
        observed_at=DAEMON_T0,
        complete=True,
        items=tuple(
            SnapshotItem(
                entity_id=key,
                payload=TodoPayload(
                    text=text,
                    status=status,  # type: ignore[arg-type]
                    scope_kind="session",
                    scope_ref="claude:s-1",
                    link="claude:s-1",
                ),
            )
            for key, text, status in items
        ),
    )


async def test_imported_tasks_show_under_their_session_and_are_read_only() -> None:
    agents = Synthetic("agents", frozenset({Capability.SESSIONS, Capability.TODOS}))
    daemon = make_daemon(
        agents, user_config={"privacy": {"redact_patterns": ["sk-[A-Za-z0-9]{8,}"]}}
    )
    await daemon.start()
    try:
        sink = daemon.sink("agents")
        live = [session("claude", "s-1", cwd="/work/alpha")]
        sink.emit(session_snapshot("agents", live, DAEMON_T0))
        working = {"claude:s-1": SessionActivity(state="working")}
        sink.emit(activity_snapshot("agents", working, DAEMON_T0))
        sink.emit(
            _agent_list(
                [("task-1", "rotate sk-abcdefghijkl", "next"), ("task-2", "done one", "done")]
            )
        )
        await daemon.settle()
        transport = httpx.ASGITransport(app=create_app(daemon, secret=SECRET))
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            now = (await http.get("/now")).json()
            (item,) = [s for s in now["sessions"] if s["key"] == "claude:s-1"]
            (todo,) = item["todos"]  # a done task is not on the picture
            assert todo["text"] == "rotate [redacted]"  # the gate redacted it
            assert (todo["status"], todo["source"], todo["source_ref"]) == (
                "next",
                "agents-tasks",
                "claude:s-1#task-1",
            )
            assert todo["link"] == "claude:s-1"
            listed = (await http.get("/todos?session=claude:s-1")).json()
            assert sorted(t["status"] for t in listed) == ["done", "next"]

            patched = await http.patch(
                f"/todos/{todo['id']}", headers=AUTH, json={"status": "done"}
            )
            assert patched.status_code == 409
            assert "imported from agents-tasks" in patched.json()["detail"]
            deleted = await http.delete(f"/todos/{todo['id']}", headers=AUTH)
            assert deleted.status_code == 409
            assert "read-only" in deleted.json()["detail"]
            missing = await http.delete("/todos/nope", headers=AUTH)
            assert missing.status_code == 404
            again = (await http.get("/todos?session=claude:s-1")).json()
            assert again == listed

            sink.emit(session_snapshot("agents", [], DAEMON_T0))  # the session ends
            await daemon.settle()
            assert (await http.get("/todos?session=claude:s-1")).json() == []
    finally:
        await daemon.stop()
