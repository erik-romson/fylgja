"""The pure parsers and the read-only database adapter of the OpenCode collector (WP3.2).

Databases are built under ``target/tests/`` from the sanitized DDL and rows in
``tests/fixtures/opencode/``; nothing here needs OpenCode.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from opencode_support import (
    CANARY,
    FIXTURES,
    build_db,
    now,
    rows_fixture,
    session_id,
)

from fylgja.collectors.opencode import adapter, parse
from fylgja.models import SessionActivity

NOW_MS = int(now().timestamp() * 1000)
STALE_MS = 1_800_000


def _message(
    role: str | None = "assistant",
    *,
    completed: int | None = None,
    errored: bool = False,
    created: int = NOW_MS - 10_000,
    updated: int | None = None,
    message_id: str = "msg_1",
) -> parse.MessageRow:
    row = parse.message_row(
        {
            "id": message_id,
            "role": role,
            "time_created": created,
            "time_updated": updated,
            "completed": completed,
            "errored": int(errored),
        }
    )
    assert row is not None
    return row


# --------------------------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------------------------


def test_missing_columns_lists_table_and_column_in_the_required_order() -> None:
    complete = {table: list(columns) for table, columns in parse.REQUIRED_COLUMNS.items()}
    assert parse.missing_columns(complete) == []
    broken = {**complete, "session": ["id", "directory"]}
    del broken["todo"]
    missing = parse.missing_columns(broken)
    assert missing[0] == "session.parent_id"
    assert "session.time_archived" in missing
    assert missing[-4:] == ["todo.session_id", "todo.content", "todo.status", "todo.position"]


# --------------------------------------------------------------------------------------------
# Small parsers
# --------------------------------------------------------------------------------------------


def test_norm_dir_drops_trailing_slashes() -> None:
    assert parse.norm_dir("/work/example/") == "/work/example"
    assert parse.norm_dir("/") == "/"
    assert parse.norm_dir("//") == "/"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"id": "example-model", "providerID": "x", "variant": "high"}', "example-model"),
        ("plain-model-name", "plain-model-name"),
        ('{"providerID": "x"}', None),
        ("[1, 2]", None),
        ("", None),
        (None, None),
        (7, None),
    ],
)
def test_model_name(raw: object, expected: str | None) -> None:
    assert parse.model_name(raw) == expected


def _session_values(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": "ses_a",
        "directory": "/work/example/app/",
        "title": "  A   title\nwith breaks ",
        "time_created": 1000,
        "time_updated": 2000,
        "model": '{"id": "m"}',
        "agent": "build",
        "version": "1.18.26",
    }
    values.update(changes)
    return values


def test_session_row_normalizes_what_it_keeps() -> None:
    row = parse.session_row(_session_values())
    assert row == parse.SessionRow(
        id="ses_a",
        directory="/work/example/app",
        title="A title with breaks",
        created_ms=1000,
        updated_ms=2000,
        model="m",
        agent="build",
        version="1.18.26",
    )


def test_session_row_drops_the_placeholder_title_and_cuts_long_ones() -> None:
    placeholder = parse.session_row(_session_values(title="New session - 2026-10-06T11:00:00.000Z"))
    assert placeholder is not None
    assert placeholder.title is None
    child = parse.session_row(_session_values(title="Child session - 2026-10-06T11:00:00.000Z"))
    assert child is not None
    assert child.title is None
    long = parse.session_row(_session_values(title="x" * 500))
    assert long is not None
    assert long.title is not None
    assert len(long.title) == parse.MAX_TITLE_CHARS
    assert long.title.endswith("…")


@pytest.mark.parametrize("missing", ["id", "directory", "time_created", "time_updated"])
def test_session_row_without_a_required_value_is_none(missing: str) -> None:
    assert parse.session_row(_session_values(**{missing: None})) is None


def test_session_row_ignores_a_boolean_time() -> None:
    assert parse.session_row(_session_values(time_created=True)) is None


def test_message_row_reads_role_completion_and_error() -> None:
    row = _message("assistant", completed=NOW_MS, errored=True, updated=NOW_MS - 5)
    assert (row.role, row.completed_ms, row.errored, row.updated_ms) == (
        "assistant",
        NOW_MS,
        True,
        NOW_MS - 5,
    )
    assert _message(updated=None).updated_ms == NOW_MS - 10_000
    assert parse.message_row({"id": None, "time_created": 1}) is None
    assert parse.message_row({"id": "m", "time_created": None}) is None


@pytest.mark.parametrize(
    ("tool", "fields", "expected"),
    [
        ("bash", {"command": "uv run pytest -x\nsecond line"}, "uv run pytest -x"),
        ("bash", {"command": "\n\n  ", "description": "Run it"}, "Run it"),
        ("bash", {"description": "Run the tests"}, "Run the tests"),
        ("bash", {}, None),
        ("read", {"filePath": "/work/example/app/src/models.py"}, "models.py"),
        ("edit", {"filePath": "/work/example/app/src/orders.py"}, "orders.py"),
        ("write", {"filePath": None}, None),
        ("glob", {"pattern": "**/*.py"}, "**/*.py"),
        ("grep", {"pattern": "def main", "path": "/work"}, "def main"),
        ("webfetch", {"url": "https://example.com/docs"}, "https://example.com/docs"),
        ("task", {"description": "Explore the code"}, "Explore the code"),
        ("list", {"path": "/work/example"}, "/work/example"),
        ("todowrite", {"command": "ignored"}, None),
        ("apply_patch", {}, None),
    ],
)
def test_tool_target(tool: str, fields: dict[str, Any], expected: str | None) -> None:
    assert parse.tool_target(tool, fields) == expected


def test_tool_target_is_cut_to_eighty_characters() -> None:
    target = parse.tool_target("bash", {"command": "echo " + "x" * 200})
    assert target is not None
    assert len(target) == parse.MAX_TARGET_CHARS
    assert target.endswith("…")


def test_part_row_keeps_tool_parts_only() -> None:
    row = parse.part_row(
        {
            "type": "tool",
            "tool": "bash",
            "status": "running",
            "time_updated": 5,
            "command": "ls",
            "description": None,
        }
    )
    assert row == parse.PartRow("bash", "running", 5, "ls")
    assert parse.part_row({"type": "text", "tool": "bash"}) is None
    assert parse.part_row({"type": "tool", "tool": None}) is None
    assert parse.part_row({"type": None}) is None


def test_todo_row_cuts_the_text_and_defaults_the_status() -> None:
    row = parse.todo_row({"content": "  Do   it  ", "status": None, "position": None})
    assert row == parse.TodoRow("Do it", "pending", 0)
    long = parse.todo_row({"content": "x" * 200, "status": "pending", "position": 3})
    assert long is not None
    assert len(long.content) == parse.MAX_TASK_CHARS
    assert parse.todo_row({"content": "  ", "status": "pending", "position": 1}) is None


# --------------------------------------------------------------------------------------------
# Busy or idle
# --------------------------------------------------------------------------------------------


def test_an_incomplete_assistant_message_is_a_turn_in_flight() -> None:
    facts = parse.turn_facts([_message()], now_ms=NOW_MS, stale_ms=STALE_MS)
    assert facts == parse.TurnFacts(True, "msg_1")
    assert facts.state == parse.BUSY


def test_a_completed_assistant_message_is_idle() -> None:
    facts = parse.turn_facts([_message(completed=NOW_MS - 1)], now_ms=NOW_MS, stale_ms=STALE_MS)
    assert facts == parse.TurnFacts(False)
    assert facts.state == parse.IDLE


def test_the_newest_assistant_message_decides_and_user_messages_are_skipped() -> None:
    messages = [
        _message("user", message_id="u2"),
        _message(message_id="a2"),
        _message(completed=NOW_MS - 50_000, message_id="a1"),
    ]
    assert parse.turn_facts(messages, now_ms=NOW_MS, stale_ms=STALE_MS) == parse.TurnFacts(
        True, "a2"
    )
    older_open = [_message(completed=NOW_MS - 1, message_id="a2"), _message(message_id="a1")]
    assert not parse.turn_facts(older_open, now_ms=NOW_MS, stale_ms=STALE_MS).busy


def test_no_assistant_message_means_idle() -> None:
    assert not parse.turn_facts([], now_ms=NOW_MS, stale_ms=STALE_MS).busy
    assert not parse.turn_facts([_message("user")], now_ms=NOW_MS, stale_ms=STALE_MS).busy
    assert not parse.turn_facts([_message(None)], now_ms=NOW_MS, stale_ms=STALE_MS).busy


def test_an_errored_incomplete_message_is_not_busy() -> None:
    assert not parse.turn_facts([_message(errored=True)], now_ms=NOW_MS, stale_ms=STALE_MS).busy
    assert parse.open_turn([_message(errored=True)]) is None


def test_a_turn_without_a_sign_of_life_for_the_stale_limit_is_not_busy() -> None:
    old = NOW_MS - STALE_MS - 1
    message = _message(created=old, updated=old)
    assert not parse.turn_facts([message], now_ms=NOW_MS, stale_ms=STALE_MS).busy
    on_the_limit = _message(created=NOW_MS - STALE_MS, updated=NOW_MS - STALE_MS)
    assert parse.turn_facts([on_the_limit], now_ms=NOW_MS, stale_ms=STALE_MS).busy


def test_a_fresh_session_or_part_keeps_an_old_message_busy() -> None:
    old = NOW_MS - 10 * STALE_MS
    message = _message(created=old, updated=old)
    assert not parse.turn_facts([message], now_ms=NOW_MS, stale_ms=STALE_MS).busy
    by_session = parse.turn_facts(
        [message], now_ms=NOW_MS, stale_ms=STALE_MS, session_updated_ms=NOW_MS - 5
    )
    by_part = parse.turn_facts(
        [message], now_ms=NOW_MS, stale_ms=STALE_MS, parts_updated_ms=NOW_MS - 5
    )
    assert by_session.busy
    assert by_part.busy


def test_open_turn_is_the_newest_assistant_message_when_it_is_incomplete() -> None:
    open_message = _message(message_id="a2")
    assert parse.open_turn([_message("user"), open_message]) == open_message
    assert parse.open_turn([_message(completed=1), _message()]) is None
    assert parse.open_turn([]) is None


def test_current_tool_is_the_newest_pending_or_running_part() -> None:
    parts = [
        parse.PartRow("read", "completed", 3, "a.py"),
        parse.PartRow("bash", "running", 2, "ls"),
        parse.PartRow("grep", "pending", 1, "x"),
    ]
    assert parse.current_tool(parts) == ("bash", "ls")
    assert parse.current_tool(parts[:1]) is None
    assert parse.current_tool([]) is None
    assert parse.current_tool([parse.PartRow("grep", "pending", None, None)]) == ("grep", None)


# --------------------------------------------------------------------------------------------
# The task list
# --------------------------------------------------------------------------------------------


def test_task_entries_follow_position_and_are_limited() -> None:
    todos = [parse.TodoRow(f"task {n}", "pending", n) for n in reversed(range(30))]
    tasks = parse.task_entries(todos)
    assert len(tasks) == parse.MAX_TASKS
    assert tasks[0] == {"status": "pending", "content": "task 0"}
    assert tasks[-1]["content"] == "task 19"


def test_the_open_task_is_the_first_in_progress_one() -> None:
    tasks = [
        {"status": "completed", "content": "a"},
        {"status": "in_progress", "content": "b"},
        {"status": "in_progress", "content": "c"},
    ]
    assert parse.open_task(tasks) == "b"
    assert parse.open_task(tasks[:1]) is None
    assert parse.open_task([]) is None
    assert parse.open_task([{"status": "in_progress", "content": ""}]) is None


# --------------------------------------------------------------------------------------------
# Process to session
# --------------------------------------------------------------------------------------------


def _row(number: int, directory: str, created: int, updated: int) -> parse.SessionRow:
    return parse.SessionRow(
        id=f"ses_{number}", directory=directory, title=None, created_ms=created, updated_ms=updated
    )


def test_a_process_gets_the_newest_session_of_its_directory() -> None:
    rows = {"/work/a": [_row(2, "/work/a", 5, 90), _row(1, "/work/a", 1, 10)]}
    chosen = parse.assign_sessions([parse.ProcInfo(10, "/work/a/", 1.0)], rows)
    assert {pid: row.id for pid, row in chosen.items()} == {10: "ses_2"}


def test_processes_in_one_directory_take_the_newest_sessions_by_age() -> None:
    rows = {
        "/work/a": [
            _row(3, "/work/a", 30, 300),
            _row(2, "/work/a", 20, 200),
            _row(1, "/work/a", 10, 100),
        ]
    }
    procs = [parse.ProcInfo(22, "/work/a", 200.0), parse.ProcInfo(11, "/work/a", 100.0)]
    chosen = parse.assign_sessions(procs, rows)
    # The older process (pid 11) gets the older of the two newest sessions.
    assert {pid: row.id for pid, row in chosen.items()} == {11: "ses_2", 22: "ses_3"}


def test_more_processes_than_sessions_leave_the_youngest_without() -> None:
    rows = {"/work/a": [_row(1, "/work/a", 10, 100)]}
    procs = [parse.ProcInfo(2, "/work/a", 300.0), parse.ProcInfo(1, "/work/a", 100.0)]
    chosen = parse.assign_sessions(procs, rows)
    assert {pid: row.id for pid, row in chosen.items()} == {1: "ses_1"}


def test_processes_without_a_start_time_are_ordered_by_pid() -> None:
    rows = {"/work/a": [_row(2, "/work/a", 20, 200), _row(1, "/work/a", 10, 100)]}
    procs = [parse.ProcInfo(9, "/work/a"), parse.ProcInfo(4, "/work/a")]
    chosen = parse.assign_sessions(procs, rows)
    assert {pid: row.id for pid, row in chosen.items()} == {4: "ses_1", 9: "ses_2"}


def test_a_directory_without_a_session_gives_its_process_none() -> None:
    assert parse.assign_sessions([parse.ProcInfo(1, "/work/none", 1.0)], {}) == {}


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------


def test_the_session_payload_carries_identity_and_process() -> None:
    row = _row(1, "/work/a", 1_791_288_000_000, 2)
    payload = parse.session_payload(row, pid=7, key="7-100", ancestry=None)
    assert (payload.kind, payload.native_id, payload.pid, payload.cwd) == (
        "opencode",
        "ses_1",
        7,
        "/work/a",
    )
    assert payload.process_key == "7-100"
    assert payload.started_at == datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    assert payload.ancestry is None
    assert parse.session_key("ses_1") == "opencode:ses_1"


def test_a_busy_activity_has_the_tool_the_open_task_and_the_list() -> None:
    row = parse.SessionRow("ses_1", "/w", "Title", 1, 2)
    todos = [parse.TodoRow("first", "completed", 0), parse.TodoRow("second", "in_progress", 1)]
    activity = parse.activity_of(row, parse.TurnFacts(True, "m"), ("bash", "ls"), todos)
    assert activity == SessionActivity(
        state="busy",
        current_tool="bash",
        current_target="ls",
        open_task="second",
        title="Title",
        facts={
            "tasks": [
                {"status": "completed", "content": "first"},
                {"status": "in_progress", "content": "second"},
            ]
        },
    )


def test_an_idle_activity_hides_the_tool_and_has_no_facts_without_todos() -> None:
    row = parse.SessionRow("ses_1", "/w", None, 1, 2)
    activity = parse.activity_of(row, parse.TurnFacts(False), ("bash", "ls"), [])
    assert activity == SessionActivity(state="idle")
    assert activity.facts is None


def test_snapshots_are_complete() -> None:
    moment = now()
    sessions = parse.session_snapshot([], moment)
    activities = parse.activity_snapshot(
        [("opencode:ses_1", SessionActivity(state="idle"))], moment
    )
    assert sessions.complete
    assert sessions.items == ()
    assert activities.complete
    assert [item.entity_id for item in activities.items] == ["opencode:ses_1"]
    assert (sessions.source, sessions.entity_id) == ("opencode", "all")


# --------------------------------------------------------------------------------------------
# The database fixtures
# --------------------------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return build_db(tmp_path / "opencode.db")


def test_the_committed_ddl_is_the_five_tables_and_holds_no_path() -> None:
    ddl = (FIXTURES / "opencode.schema.sql").read_text(encoding="utf-8")
    for table in ("project", "session", "message", "part", "todo"):
        assert f"CREATE TABLE `{table}`" in ddl
    assert "/Users/" not in ddl


def test_the_fixture_database_has_every_column_the_collector_needs(db_path: Path) -> None:
    with adapter.OpenCodeDb.open(db_path) as db:
        columns = db.check_schema()
    assert parse.missing_columns(columns) == []
    assert "model" in columns["session"]


def test_sessions_are_top_level_not_archived_and_newest_first(db_path: Path) -> None:
    directories = {"/work/example/api-service", "/work/example/web-app", "/work/example/docs"}
    with adapter.OpenCodeDb.open(db_path) as db:
        found = db.sessions(directories, db.check_schema())
    ids = {directory: [row.id for row in rows] for directory, rows in found.items()}
    # Session 4 is archived and newer, session 5 is a child: neither is a candidate.
    assert ids["/work/example/api-service"] == [session_id(1), session_id(3)]
    assert ids["/work/example/web-app"] == [session_id(2)]
    assert ids["/work/example/docs"] == [session_id(6)]
    first = found["/work/example/api-service"][0]
    assert first.model == "example-model"
    assert first.title == "Add pagination to the orders endpoint"
    assert first.version == "1.18.26"


def test_sessions_of_no_directory_are_none(db_path: Path) -> None:
    with adapter.OpenCodeDb.open(db_path) as db:
        assert db.sessions(set(), db.check_schema()) == {}
        assert db.sessions({"/work/nowhere"}, db.check_schema()) == {}


def test_the_busy_session_has_an_incomplete_message_and_a_running_tool(db_path: Path) -> None:
    with adapter.OpenCodeDb.open(db_path) as db:
        messages = db.messages(session_id(1))
        assert [m.role for m in messages] == ["assistant", "user", "assistant", "user"]
        assert messages[0].completed_ms is None
        assert messages[1].completed_ms is None
        assert messages[2].completed_ms is not None
        open_message = parse.open_turn(messages)
        assert open_message is not None
        parts = db.tool_parts(open_message.id)
    assert [(p.tool, p.status) for p in parts] == [("bash", "running"), ("read", "completed")]
    assert parse.current_tool(parts) == ("bash", "uv run pytest tests/unit -x")


def test_a_message_read_holds_no_text(db_path: Path) -> None:
    with adapter.OpenCodeDb.open(db_path) as db:
        rows = [*db.messages(session_id(1)), *db.tool_parts("msg_0000000104")]
    assert CANARY not in repr(rows)


def test_the_todo_table_is_the_sessions_own_list(db_path: Path) -> None:
    with adapter.OpenCodeDb.open(db_path) as db:
        todos = db.todos(session_id(1))
        assert db.todos(session_id(2)) == []
        assert [t.content for t in db.todos(session_id(3))] == ["A task of the older session"]
    assert [(t.position, t.status) for t in todos] == [
        (0, "completed"),
        (1, "in_progress"),
        (2, "pending"),
    ]
    assert parse.open_task(parse.task_entries(todos)) == "Write the pagination tests"


def test_the_fixture_rows_cover_every_case_of_the_readme() -> None:
    rows = rows_fixture()
    assert len(rows["session"]) == 8
    archived = [s for s in rows["session"] if s["time_archived"] is not None]
    children = [s for s in rows["session"] if s["parent_id"] is not None]
    assert [s["id"] for s in archived] == [session_id(4)]
    assert [s["id"] for s in children] == [session_id(5)]


def test_a_message_whose_json_is_invalid_is_read_as_a_row_without_a_role(tmp_path: Path) -> None:
    path = build_db(tmp_path / "opencode.db")
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data)"
        " VALUES ('msg_bad', ?, ?, ?, 'not json')",
        [session_id(2), NOW_MS + 5, NOW_MS + 5],
    )
    connection.commit()
    connection.close()
    with adapter.OpenCodeDb.open(path) as db:
        newest = db.messages(session_id(2))[0]
    assert (newest.id, newest.role, newest.completed_ms, newest.errored) == (
        "msg_bad",
        None,
        None,
        False,
    )


def test_a_part_whose_json_is_invalid_is_skipped(tmp_path: Path) -> None:
    path = build_db(
        tmp_path / "opencode.db",
        extra_sql=(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)"
            " VALUES ('prt_zz', 'msg_0000000104', 'ses_0000000001', 1, 1, '{broken');"
        ),
    )
    with adapter.OpenCodeDb.open(path) as db:
        parts = db.tool_parts("msg_0000000104")
    assert [p.tool for p in parts] == ["bash", "read"]


def test_a_database_missing_a_column_is_a_schema_mismatch(tmp_path: Path) -> None:
    ddl = (FIXTURES / "opencode.schema.sql").read_text(encoding="utf-8")
    path = tmp_path / "opencode.db"
    connection = sqlite3.connect(path)
    connection.executescript(ddl.replace("`time_archived` integer,", ""))
    connection.close()
    with adapter.OpenCodeDb.open(path) as db, pytest.raises(adapter.OpenCodeDbError) as caught:
        db.check_schema()
    assert caught.value.kind == "schema_mismatch"
    assert "session.time_archived" in caught.value.detail


def test_an_empty_database_lacks_every_table(tmp_path: Path) -> None:
    path = tmp_path / "opencode.db"
    sqlite3.connect(path).close()
    with adapter.OpenCodeDb.open(path) as db, pytest.raises(adapter.OpenCodeDbError) as caught:
        db.check_schema()
    assert caught.value.kind == "schema_mismatch"
    assert "todo.content" in caught.value.detail


def test_a_file_that_is_not_a_database_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "opencode.db"
    path.write_bytes(b"corrupt" * 200)
    with adapter.OpenCodeDb.open(path) as db, pytest.raises(adapter.OpenCodeDbError) as caught:
        db.check_schema()
    assert caught.value.kind == "db_unreadable"
    assert "corrupt" not in caught.value.detail


def test_a_missing_database_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "opencode.db"
    with pytest.raises(adapter.OpenCodeDbError) as caught:
        adapter.OpenCodeDb.open(path)
    assert caught.value.kind == "db_unreadable"
    assert not path.exists()
    assert not path.parent.exists()


def test_the_adapter_cannot_write(db_path: Path) -> None:
    before = db_path.read_bytes()
    with adapter.OpenCodeDb.open(db_path) as db:
        db.check_schema()
        with pytest.raises(adapter.OpenCodeDbError):
            db._query("DELETE FROM todo")  # noqa: SLF001
    assert db_path.read_bytes() == before
    assert sorted(p.name for p in db_path.parent.iterdir()) == ["opencode.db"]


def test_the_default_database_path_follows_xdg_data_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert adapter.default_db_path() == Path.home() / ".local/share/opencode/opencode.db"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert adapter.default_db_path() == tmp_path / "xdg/opencode/opencode.db"
    monkeypatch.setenv("XDG_DATA_HOME", "relative/dir")
    assert adapter.default_db_path() == Path.home() / ".local/share/opencode/opencode.db"
