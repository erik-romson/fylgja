"""The pure Codex parsers over the sanitized fixtures, plus the state database they describe.

The ``state_5.sqlite`` of every database test is built from the committed DDL and rows under
``target/tests/``; no database file is committed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from codex_support import (
    FIXTURES,
    PLACEHOLDER_HOME,
    PROCESS_FIXTURES,
    ROLLOUT_FIXTURES,
    make_home,
    thread_id,
)

from fylgja.collectors.codex import adapter, parse
from fylgja.collectors.codex.adapter import StateDb, StateDbError
from fylgja.collectors.seams import RangeReader
from fylgja.models import ProcessRef, SessionActivity

# --------------------------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------------------------


def test_pgrep_output_is_sorted_and_deduplicated() -> None:
    assert parse.parse_pgrep("41002\n41001\n\nnot a pid\n41001\n") == [41001, 41002]
    assert parse.parse_pgrep("") == []
    assert parse.parse_pgrep((PROCESS_FIXTURES / "pgrep.txt").read_text()) == [41001, 41002]


def test_rollout_thread_id_comes_from_the_file_name() -> None:
    name = f"rollout-2026-10-06T09-00-00-{thread_id(1)}.jsonl"
    assert parse.rollout_thread_id(f"{PLACEHOLDER_HOME}/sessions/2026/10/06/{name}") == thread_id(1)
    assert parse.rollout_thread_id(name) == thread_id(1)
    for other in (
        "state_5.sqlite",
        "rollout-latest.jsonl",
        f"rollout-2026-10-06T09-00-00-{thread_id(1)}.jsonl.bak",
        f"rollout-2026-10-06T09-00-00-{thread_id(1).upper()}.jsonl",
        "",
    ):
        assert parse.rollout_thread_id(other) is None, other


def test_lsof_fields_give_the_cwd_and_the_open_rollout_of_each_process() -> None:
    found = parse.parse_lsof_fields((PROCESS_FIXTURES / "lsof-fields.txt").read_text())
    assert sorted(found) == [41001, 41002]
    first = found[41001]
    assert first.cwd == "/work/example/api-service"
    assert [parse.rollout_thread_id(p) for p in first.rollouts] == [thread_id(1)]
    assert found[41002].cwd == "/work/example/web-app"
    assert [parse.rollout_thread_id(p) for p in found[41002].rollouts] == [thread_id(2)]


def test_lsof_fields_tolerate_noise_and_repeats() -> None:
    rollout = f"/h/sessions/rollout-2026-10-06T09-00-00-{thread_id(1)}.jsonl"
    text = (
        "n/orphan/before/any/process\n"
        f"p7\nfcwd\nn/work\nf3\nn{rollout}\nf4\nn{rollout}\nf5\nn/h/state_5.sqlite\n"
        "pnot-a-pid\nf9\nn/ignored\n"
        "p8\n"
    )
    found = parse.parse_lsof_fields(text)
    assert found == {
        7: parse.OpenFiles(7, "/work", (rollout,)),
        8: parse.OpenFiles(8, None, ()),
    }
    assert parse.parse_lsof_fields("") == {}


def test_process_table_gives_parent_start_and_name() -> None:
    table = parse.parse_process_table((PROCESS_FIXTURES / "ps-table.txt").read_text())
    assert sorted(table) == [1, 7000, 7010, 41001, 41002]
    assert table[41001].ppid == 7010
    assert table[41001].comm == "codex"
    assert table[7000].comm == "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea"
    assert table[41001].started == datetime(2026, 10, 6, 9, 0, 0)  # noqa: DTZ001 - local on purpose
    # Garbage and a malformed date are skipped.
    assert parse.parse_process_table("nonsense\n  5 1 Xyz Foo 99 99:99:99 2026 cmd\n") == {}


def test_ancestor_chain_stops_at_launchd_unknown_pids_and_loops() -> None:
    table = parse.parse_process_table((PROCESS_FIXTURES / "ps-table.txt").read_text())
    chain = parse.ancestor_chain(table, 41001)
    assert chain == (
        ProcessRef(pid=41001, ppid=7010, comm="codex"),
        ProcessRef(pid=7010, ppid=7000, comm="/bin/zsh"),
        ProcessRef(pid=7000, ppid=1, comm="/Applications/IntelliJ IDEA.app/Contents/MacOS/idea"),
    )
    assert parse.ancestor_chain(table, 999) == ()
    now = datetime(2026, 10, 6)  # noqa: DTZ001
    loop = {10: parse.ProcessEntry(11, now, "a"), 11: parse.ProcessEntry(10, now, "b")}
    assert [r.pid for r in parse.ancestor_chain(loop, 10)] == [10, 11]
    long = {i: parse.ProcessEntry(i + 1, now, "x") for i in range(2, 400)}
    assert len(parse.ancestor_chain(long, 2)) == parse.MAX_CHAIN


def test_process_key_and_session_key() -> None:
    assert parse.process_key(41001, 1_790_000_000.9) == "41001-1790000000"
    assert parse.session_key(thread_id(1)) == f"codex:{thread_id(1)}"


def test_local_epoch_reads_naive_time_as_local() -> None:
    naive = datetime(2026, 10, 6, 9, 0, 0)  # noqa: DTZ001
    assert parse.local_epoch(naive) == naive.astimezone().timestamp()


# --------------------------------------------------------------------------------------------
# State database names and schema detection
# --------------------------------------------------------------------------------------------


def test_the_newest_state_database_is_chosen_and_side_files_are_ignored() -> None:
    names = ["state_5.sqlite", "state_5.sqlite-wal", "state_5.sqlite-shm", "logs_2.sqlite"]
    assert parse.choose_state_db(names) == parse.StateDbChoice("state_5.sqlite", 5)
    assert parse.choose_state_db(names + ["state_10.sqlite"]) == parse.StateDbChoice(
        "state_10.sqlite", 10
    )
    assert parse.choose_state_db(["logs_2.sqlite", "state.sqlite"]) is None
    assert parse.choose_state_db([]) is None


def test_only_known_versions_are_supported() -> None:
    assert parse.StateDbChoice("state_5.sqlite", 5).supported
    assert not parse.StateDbChoice("state_6.sqlite", 6).supported
    assert parse.SUPPORTED_STATE_VERSIONS == {5}


def test_missing_required_columns_are_listed_in_order() -> None:
    assert parse.missing_columns(["id", "cwd", "title", "git_branch", "model", "x"]) == []
    assert parse.missing_columns(["id", "title"]) == ["cwd", "git_branch", "model"]


def test_thread_row_keeps_what_is_useful_and_drops_what_is_empty() -> None:
    row = parse.thread_row(
        {
            "id": "t",
            "cwd": "/w",
            "title": "  A   title ",
            "git_branch": "",
            "model": None,
            "name": "named",
            "cli_version": "0.154.0",
            "created_at_ms": 5,
        }
    )
    assert row == parse.ThreadRow(
        "t", "/w", "A title", None, None, name="named", cli_version="0.154.0", created_at_ms=5
    )
    assert row is not None
    assert row.label == "named"
    assert parse.thread_row({"id": ""}) is None
    assert parse.thread_row({}) is None
    odd = parse.thread_row({"id": "t", "created_at_ms": True, "title": "x" * 500})
    assert odd is not None
    assert odd.created_at_ms is None
    assert odd.label is not None
    assert len(odd.label) == parse.MAX_TITLE_CHARS


def test_the_ddl_fixture_is_schema_5_and_has_the_required_columns(isolated_env: Path) -> None:
    path = isolated_env / "ddl.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript((FIXTURES / "state_5.schema.sql").read_text())
        columns = [row[1] for row in connection.execute("PRAGMA table_info(threads)")]
    finally:
        connection.close()
    assert parse.missing_columns(columns) == []
    assert set(parse.OPTIONAL_THREAD_COLUMNS) <= set(columns)


# --------------------------------------------------------------------------------------------
# Reading the database
# --------------------------------------------------------------------------------------------


def test_state_db_reads_threads_read_only(isolated_env: Path) -> None:
    codex = make_home(isolated_env)
    with StateDb.open(codex.home / "state_5.sqlite") as db:
        found = db.threads({thread_id(1), thread_id(3), "missing"})
        assert sorted(found) == [thread_id(1), thread_id(3)]
        row = parse.thread_row(found[thread_id(1)])
        assert row == parse.ThreadRow(
            thread_id(1),
            "/work/example/api-service",
            "Add pagination to the orders endpoint",
            "main",
            "gpt-5-codex",
            name=None,
            cli_version="0.154.0",
            created_at_ms=1791277200000,
        )
        renamed = parse.thread_row(found[thread_id(3)])
        assert renamed is not None
        assert (renamed.name, renamed.branch, renamed.model) == ("helper rename", None, None)
        assert db.threads(set()) == {}
        with pytest.raises(StateDbError) as refused:
            db._query("INSERT INTO threads (id) VALUES ('x')")  # a write must fail
        assert refused.value.kind == "state_db_unreadable"
    assert not (codex.home / "state_5.sqlite-journal").exists()


def test_state_db_finds_sub_agent_threads(isolated_env: Path) -> None:
    codex = make_home(isolated_env)
    with StateDb.open(codex.home / "state_5.sqlite") as db:
        assert db.spawn_children({thread_id(4), thread_id(6)}) == {thread_id(6)}
        assert db.spawn_children({thread_id(4)}) == set()
        assert db.spawn_children(set()) == set()


def test_state_db_without_the_edge_table_has_no_sub_agents(isolated_env: Path) -> None:
    codex = make_home(isolated_env, db=False)
    path = codex.home / "state_5.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE threads (id TEXT)")
    connection.commit()
    connection.close()
    with StateDb.open(path) as db:
        assert db.spawn_children({"a"}) == set()


def test_a_missing_database_is_not_created(isolated_env: Path) -> None:
    path = isolated_env / "nowhere" / "state_5.sqlite"
    with pytest.raises(StateDbError) as raised:
        StateDb.open(path)
    assert raised.value.kind == "state_db_unreadable"
    assert not path.exists()
    assert not path.parent.exists()


def test_a_file_that_is_not_a_database_is_unreadable(isolated_env: Path) -> None:
    path = isolated_env / "state_5.sqlite"
    path.write_text("this is not sqlite" * 50)
    with StateDb.open(path) as db, pytest.raises(StateDbError) as raised:
        db.threads({"a"})
    assert raised.value.kind == "state_db_unreadable"


def test_a_threads_table_without_required_columns_is_a_schema_mismatch(isolated_env: Path) -> None:
    path = isolated_env / "state_5.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE threads (id TEXT, title TEXT)")
    connection.commit()
    connection.close()
    with StateDb.open(path) as db, pytest.raises(StateDbError) as raised:
        db.threads({"a"})
    assert raised.value.kind == "state_schema_mismatch"
    assert "cwd" in raised.value.detail
    assert "git_branch" in raised.value.detail


def test_optional_columns_may_be_absent(isolated_env: Path) -> None:
    path = isolated_env / "state_5.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE threads (id TEXT, cwd TEXT, title TEXT, git_branch TEXT, model TEXT)"
    )
    connection.execute("INSERT INTO threads VALUES ('a', '/w', 't', 'b', 'm')")
    connection.commit()
    connection.close()
    with StateDb.open(path) as db:
        row = parse.thread_row(db.threads({"a"})["a"])
    assert row == parse.ThreadRow("a", "/w", "t", "b", "m")


def test_build_state_db_places_the_rollouts_where_the_rows_point(isolated_env: Path) -> None:
    codex = make_home(isolated_env)
    with StateDb.open(codex.home / "state_5.sqlite") as db:
        paths = [r["rollout_path"] for r in db._query("SELECT rollout_path FROM threads")]
    assert all(Path(p).parent == codex.rollout_dir for p in paths)
    assert codex.rollout(1).is_file()
    assert not codex.rollout(5).exists()  # the archived thread has no rollout in the fixture


# --------------------------------------------------------------------------------------------
# Rollouts: busy or idle
# --------------------------------------------------------------------------------------------


def facts_of(name: str) -> parse.RolloutFacts:
    return parse.analyze_rollout((ROLLOUT_FIXTURES / name).read_bytes().split(b"\n"))


def test_a_closed_turn_is_idle() -> None:
    facts = facts_of("idle.jsonl")
    assert facts.busy is False
    assert facts.state == "idle"
    assert facts.pending is None
    assert facts.last_user == "Add pagination to the orders endpoint"
    assert facts.last_reply == "I added a page parameter and a limit. Tests pass."
    assert facts.model == "gpt-5-codex"
    assert facts.cwd == "/work/example/api-service"


def test_an_open_turn_is_busy_and_names_the_call_without_output() -> None:
    facts = facts_of("busy-tool.jsonl")
    assert facts.busy is True
    assert facts.state == "busy"
    # call_a has its output, call_b does not; turn 1's end does not close turn 2.
    assert facts.pending == parse.PendingCall("exec_command", "pytest tests/test_login.py -x --lf")
    assert facts.last_user == "Fix the failing login test"


def test_a_pending_apply_patch_names_the_file_and_legacy_user_messages_count() -> None:
    facts = facts_of("busy-patch.jsonl")
    assert facts.busy is True
    assert facts.pending == parse.PendingCall("apply_patch", "src/helpers.py")
    assert facts.last_user == "Rename the helper"


def test_an_aborted_turn_is_idle_and_has_no_current_call() -> None:
    facts = facts_of("aborted.jsonl")
    assert facts.busy is False
    assert facts.pending is None
    assert facts.last_user == "Summarize the changelog"


def test_a_rollout_without_a_turn_event_says_nothing() -> None:
    facts = facts_of("fresh.jsonl")
    assert facts.busy is None
    assert facts.state is None
    assert facts.pending is None
    assert facts.model == "gpt-5-codex"
    assert facts.cwd == "/work/example/notes"


def test_torn_and_foreign_lines_are_skipped() -> None:
    facts = facts_of("torn.jsonl")
    assert facts.busy is True  # task_started is the last turn event that parsed
    assert facts.lines == 2  # session_meta and task_started
    assert parse.analyze_rollout([]) == parse.RolloutFacts()
    assert parse.analyze_rollout([b"", b"{", b"null", b'{"type": 1, "payload": {}}']) == (
        parse.RolloutFacts()
    )


def line(kind: str, payload: dict[str, Any]) -> str:
    return json.dumps({"type": kind, "payload": payload})


def ev(kind: str, **payload: Any) -> str:
    return line("event_msg", {"type": kind, **payload})


def resp(kind: str, **payload: Any) -> str:
    return line("response_item", {"type": kind, **payload})


def test_the_newest_turn_event_decides() -> None:
    started, done, aborted = ev("task_started"), ev("task_complete"), ev("turn_aborted")
    assert parse.analyze_rollout([started]).busy is True
    assert parse.analyze_rollout([started, done]).busy is False
    assert parse.analyze_rollout([started, done, started]).busy is True
    assert parse.analyze_rollout([started, aborted]).busy is False
    assert parse.analyze_rollout([done]).busy is False
    assert parse.analyze_rollout([started, aborted, started, done]).busy is False


def test_a_call_from_an_earlier_turn_is_not_current() -> None:
    lines = [
        ev("task_started"),
        resp("function_call", name="exec_command", arguments="{}", call_id="c1"),
        ev("turn_aborted"),
        ev("task_started"),
    ]
    facts = parse.analyze_rollout(lines)
    assert facts.busy is True
    assert facts.pending is None


def test_the_newest_unanswered_call_is_current_and_an_answer_clears_it() -> None:
    first = resp("function_call", name="shell", arguments='{"command": ["ls"]}', call_id="1")
    second = resp("custom_tool_call", name="exec", input="1+1", call_id="2")
    out_first = resp("function_call_output", call_id="1", output="x")
    out_second = resp("custom_tool_call_output", call_id="2", output="x")
    started = ev("task_started")
    assert parse.analyze_rollout([started, first, second]).pending == parse.PendingCall("exec")
    assert parse.analyze_rollout([started, first, second, out_second]).pending == parse.PendingCall(
        "shell", "ls"
    )
    assert parse.analyze_rollout([started, first, second, out_first, out_second]).pending is None


def test_a_call_in_an_idle_session_is_not_reported() -> None:
    lines = [ev("task_complete"), resp("function_call", name="shell", arguments="{}", call_id="1")]
    assert parse.analyze_rollout(lines).pending is None


def test_calls_with_missing_parts_are_ignored() -> None:
    lines = [
        ev("task_started"),
        resp("function_call", name="", arguments="{}", call_id="1"),
        resp("function_call", name="shell", arguments="{}"),
        resp("function_call_output"),
    ]
    assert parse.analyze_rollout(lines).pending is None


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("exec_command", '{"cmd": "pytest -x\\nsecond"}', "pytest -x"),
        ("exec_command", '{"cmd": "\\n\\n  ls  -la "}', "ls -la"),
        ("shell", '{"command": ["bash", "-lc", "cat CHANGELOG.md"]}', "cat CHANGELOG.md"),
        ("shell", '{"command": ["ls", "-la"]}', "ls -la"),
        ("shell_command", '{"command": "make test"}', "make test"),
        ("view_image", '{"path": "img.png"}', "img.png"),
        ("shell", '{"command": [1, 2]}', None),
        ("shell", '{"command": ""}', None),
        ("update_plan", '{"plan": []}', None),
        ("shell", "[1, 2]", None),
        ("shell", "not json", None),
        ("shell", None, None),
        (
            "apply_patch",
            "*** Begin Patch\n*** Add File: new/file.py\n+x\n*** End Patch",
            "new/file.py",
        ),
        ("apply_patch", "*** Begin Patch\n*** Delete File: gone.txt\n", "gone.txt"),
        ("apply_patch", "no files here", None),
    ],
)
def test_call_target(name: str, raw: object, expected: str | None) -> None:
    assert parse.call_target(name, raw) == expected


def test_a_long_command_is_cut() -> None:
    target = parse.call_target("shell", json.dumps({"cmd": "x" * 500}))
    assert target is not None
    assert len(target) == parse.MAX_TARGET_CHARS
    assert target.endswith("…")


def test_user_messages_come_from_three_places_and_codex_injections_are_skipped() -> None:
    injected = resp(
        "message", role="user", content=[{"type": "input_text", "text": "<environment_context>"}]
    )
    agents = resp(
        "message", role="user", content=[{"type": "input_text", "text": "# AGENTS.md instructions"}]
    )
    typed = resp("message", role="user", content=[{"type": "input_text", "text": "  Do   it "}])
    item = ev(
        "item_completed",
        item={"type": "UserMessage", "content": [{"type": "text", "text": "From the item"}]},
    )
    legacy = ev("user_message", message="From the event")
    assert parse.analyze_rollout([injected, agents]).last_user is None
    assert parse.analyze_rollout([injected, typed]).last_user == "Do it"
    assert parse.analyze_rollout([typed, item]).last_user == "From the item"
    assert parse.analyze_rollout([item, legacy]).last_user == "From the event"
    assert parse.analyze_rollout([ev("user_message", message="   ")]).last_user is None
    assert parse.analyze_rollout([ev("user_message", message=3)]).last_user is None
    long = parse.analyze_rollout([ev("user_message", message="word " * 200)]).last_user
    assert long is not None
    assert len(long) == parse.MAX_PROMPT_CHARS


def test_replies_come_from_events_items_messages_and_the_turn_end() -> None:
    assert parse.analyze_rollout([ev("agent_message", message="From the event")]).last_reply == (
        "From the event"
    )
    agent_item = ev(
        "item_completed",
        item={"type": "AgentMessage", "content": [{"type": "Text", "text": "From the item"}]},
    )
    assert parse.analyze_rollout([agent_item]).last_reply == "From the item"
    assistant = resp(
        "message", role="assistant", content=[{"type": "output_text", "text": "From a message"}]
    )
    assert parse.analyze_rollout([assistant]).last_reply == "From a message"
    assert parse.analyze_rollout([assistant, ev("task_complete", last_agent_message="Done")]) == (
        parse.RolloutFacts(busy=False, last_reply="Done", lines=2)
    )
    developer = resp("message", role="developer", content=[{"type": "input_text", "text": "x"}])
    assert parse.analyze_rollout([developer]).last_reply is None
    unknown_item = ev("item_completed", item={"type": "Reasoning"})
    assert parse.analyze_rollout([unknown_item, ev("item_completed", item=5)]) == (
        parse.RolloutFacts(lines=2)
    )
    assert parse.analyze_rollout([ev("token_count")]).lines == 1
    assert parse.analyze_rollout([line("world_state", {})]).lines == 1


def test_content_that_is_plain_text_is_accepted() -> None:
    item = ev("item_completed", item={"type": "UserMessage", "content": "plain"})
    assert parse.analyze_rollout([item]).last_user == "plain"
    odd = ev("item_completed", item={"type": "UserMessage", "content": 3})
    assert parse.analyze_rollout([odd]).last_user is None


def test_turn_context_and_session_meta_give_model_and_cwd() -> None:
    lines = [
        line("session_meta", {"cwd": "/a"}),
        line("turn_context", {"cwd": "/b", "model": "m1"}),
        line("turn_context", {"model": "m2"}),
        line("session_meta", {"cwd": "", "model": "ignored"}),
    ]
    facts = parse.analyze_rollout(lines)
    assert (facts.cwd, facts.model) == ("/b", "m2")


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------

ROW = parse.ThreadRow(
    "t1",
    "/work/db",
    "Thread title",
    "main",
    "gpt-5-codex",
    name=None,
    cli_version="0.154.0",
    created_at_ms=1_791_277_200_000,
)


def test_the_session_payload_prefers_the_database_and_falls_back_to_rollout_and_process() -> None:
    facts = parse.RolloutFacts(model="rollout-model", cwd="/work/rollout")
    chain = (ProcessRef(pid=5, ppid=1, comm="codex"),)
    full = parse.session_payload(
        "t1", pid=5, thread=ROW, facts=facts, process_cwd="/work/proc", key="5-1", ancestry=chain
    )
    assert full.kind == "codex"
    assert full.native_id == "t1"
    assert (full.pid, full.cwd, full.model, full.branch) == (5, "/work/db", "gpt-5-codex", "main")
    assert full.version == "0.154.0"
    assert full.started_at == datetime.fromtimestamp(1_791_277_200, UTC)
    assert full.process_key == "5-1"
    assert full.ancestry == chain

    bare = parse.session_payload(
        "t1", pid=5, thread=None, facts=facts, process_cwd="/work/proc", key=None, ancestry=()
    )
    assert (bare.cwd, bare.model, bare.branch, bare.name, bare.started_at) == (
        "/work/rollout",
        "rollout-model",
        None,
        None,
        None,
    )
    assert bare.ancestry is None
    proc_only = parse.session_payload(
        "t1", pid=5, thread=None, facts=None, process_cwd="/work/proc", key=None, ancestry=None
    )
    assert proc_only.cwd == "/work/proc"
    assert proc_only.model is None


def test_activity_carries_state_title_prompt_and_the_current_call() -> None:
    facts = parse.RolloutFacts(
        busy=True,
        last_user="Fix the login",
        last_reply="Looking.",
        pending=parse.PendingCall("exec_command", "pytest"),
    )
    assert parse.activity_of(ROW, facts) == SessionActivity(
        state="busy",
        current_tool="exec_command",
        current_target="pytest",
        last_prompt="Fix the login",
        last_reply="Looking.",
        title="Thread title",
        branch="main",
    )
    idle = parse.activity_of(ROW, parse.RolloutFacts(busy=False, last_user="x"))
    assert idle.state == "idle"
    assert idle.current_tool is None


def test_activity_without_rollout_or_row_still_says_what_it_can() -> None:
    assert parse.activity_of(ROW, None) == SessionActivity(title="Thread title", branch="main")
    assert parse.activity_of(None, parse.RolloutFacts(busy=False)) == SessionActivity(state="idle")


def test_the_user_given_name_beats_the_generated_title() -> None:
    named = parse.ThreadRow("t", None, "Generated", None, None, name="My name")
    assert parse.activity_of(named, None).title == "My name"


def test_a_title_that_is_only_the_start_of_the_prompt_is_dropped() -> None:
    row = parse.ThreadRow("t", None, "Add pagination to the orders…", None, None)
    same = parse.activity_of(
        row, parse.RolloutFacts(last_user="Add pagination to the orders endpoint")
    )
    assert same.title is None
    other = parse.activity_of(row, parse.RolloutFacts(last_user="Something else"))
    assert other.title == "Add pagination to the orders…"


def test_snapshots_are_complete_and_keyed_by_session() -> None:
    now = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    payload = parse.session_payload(
        "t1", pid=5, thread=ROW, facts=None, process_cwd=None, key=None, ancestry=None
    )
    sessions = parse.session_snapshot([("codex:t1", payload)], now)
    activities = parse.activity_snapshot([("codex:t1", SessionActivity(state="idle"))], now)
    for snapshot in (sessions, activities):
        assert snapshot.source == "codex"
        assert snapshot.complete
        assert [item.entity_id for item in snapshot.items] == ["codex:t1"]
    assert parse.session_snapshot([], now).items == ()


# --------------------------------------------------------------------------------------------
# Reading the rollout tail
# --------------------------------------------------------------------------------------------


class _WholeFileOnly:
    """A filesystem without ``read_range``: the tail is cut from a whole read."""

    def __init__(self, inner: adapter.LocalFileSystem) -> None:
        self._inner = inner

    def read_bytes(self, path: Path | str) -> bytes:
        return self._inner.read_bytes(path)

    def stat(self, path: Path | str) -> Any:
        return self._inner.stat(path)


def test_the_tail_of_a_long_rollout_drops_the_partial_first_line(isolated_env: Path) -> None:
    path = isolated_env / "r.jsonl"
    body = b"".join(b'{"n": %d}\n' % n for n in range(100))
    path.write_bytes(body)
    fs = adapter.LocalFileSystem()
    assert isinstance(fs, RangeReader)
    whole = adapter.read_rollout_tail(fs, path, 10_000)
    assert whole.complete
    assert whole.lines[:2] == [b'{"n": 0}', b'{"n": 1}']
    assert whole.size == len(body)
    tail = adapter.read_rollout_tail(fs, path, 50)
    assert not tail.complete
    assert tail.lines[-2:] == [b'{"n": 99}', b""]
    assert all(entry.startswith(b'{"n"') for entry in tail.lines[:-1])
    # A filesystem without ranges gives the same lines.
    plain = _WholeFileOnly(fs)
    assert not isinstance(plain, RangeReader)
    assert adapter.read_rollout_tail(plain, path, 50).lines == tail.lines  # type: ignore[arg-type]
    # A limit shorter than one line leaves nothing whole.
    assert adapter.read_rollout_tail(fs, path, 3).lines == [b""]
