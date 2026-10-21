"""The transcript tail, the facts accumulator and the collector's use of both (WP1.7).

Real files in a temporary directory, so offsets, inodes and torn writes are the real thing; a
:class:`FakeFS` (which has no ranged read) covers the fallback.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_support import T0, World, row_data, write_row
from transcript_support import (
    CLAUDE_TRANSCRIPTS,
    FIXTURES,
    append,
    assistant,
    line,
    text,
    tool_result,
    tool_use,
    user,
)

from fylgja.collectors.claude import facts, transcript
from fylgja.collectors.claude.adapter import LocalFileSystem
from fylgja.collectors.seams import RangeReader
from fylgja.models import SessionActivity, SessionActivitySnapshot, SessionSnapshot
from fylgja.testing.fakes import FakeFS

CLEAR = FIXTURES / "ratatosk" / "clear-model-session"
BEFORE_ID = "07c2d066-18d3-4816-a0d3-d3b83bb11d08"
AFTER_ID = "d09ef654-9c0d-4e51-85c9-65eff5f3cba5"
CWD = "/Users/user/projects/ratatosk/tmp/hook-probe3"


def make_tail(path: Path, **kwargs: int) -> transcript.TranscriptTail:
    return transcript.TranscriptTail(LocalFileSystem(), path, **kwargs)


# --------------------------------------------------------------------------------------------
# The tail
# --------------------------------------------------------------------------------------------


def test_the_local_filesystem_can_read_a_range() -> None:
    assert isinstance(LocalFileSystem(), RangeReader)
    assert not isinstance(FakeFS(), RangeReader)


def test_polls_return_only_what_was_appended(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(user("one") + user("two"), encoding="utf-8")
    tail = make_tail(path)
    first = tail.poll()
    assert [e["message"]["content"] for e in first.entries] == ["one", "two"]
    assert tail.offset == path.stat().st_size
    assert not first.reset
    assert tail.poll().entries == []
    append(path, user("three"))
    second = tail.poll()
    assert [e["message"]["content"] for e in second.entries] == ["three"]
    assert tail.offset == path.stat().st_size


def test_a_torn_last_line_is_completed_by_the_next_poll(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    whole = user("whole")
    torn = user("torn one")
    path.write_text(whole + torn[:20], encoding="utf-8")
    tail = make_tail(path)
    assert [e["message"]["content"] for e in tail.poll().entries] == ["whole"]
    assert tail.carried == 20
    assert tail.poll().entries == []  # nothing new on disk
    append(path, torn[20:-3])
    assert tail.poll().entries == []  # still not a whole object
    assert tail.carried == len(torn) - 3
    append(path, torn[-3:])
    done = tail.poll()
    assert [e["message"]["content"] for e in done.entries] == ["torn one"]
    assert tail.carried == 0


def test_a_last_line_without_a_newline_is_delivered_once(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(user("a") + user("b").rstrip("\n"), encoding="utf-8")
    tail = make_tail(path)
    assert len(tail.poll().entries) == 2
    append(path, "\n")
    assert tail.poll().entries == []
    append(path, user("c"))
    assert [e["message"]["content"] for e in tail.poll().entries] == ["c"]


def test_a_garbage_line_is_skipped_and_the_rest_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(user("a") + "not json\n\n[1,2]\n" + user("b"), encoding="utf-8")
    entries = make_tail(path).poll().entries
    assert [e["message"]["content"] for e in entries] == ["a", "b"]


def test_a_shrunk_file_resets_the_tail(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(user("a") + user("b") + user("c"), encoding="utf-8")
    tail = make_tail(path)
    tail.poll()
    with path.open("r+b") as handle:  # truncated in place, then rewritten shorter
        handle.truncate(0)
        handle.write(user("z").encode())
    update = tail.poll()
    assert update.reset
    assert [e["message"]["content"] for e in update.entries] == ["z"]
    assert tail.offset == path.stat().st_size


def test_an_atomic_replace_resets_the_tail_even_when_the_file_grew(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(user("a"), encoding="utf-8")
    tail = make_tail(path)
    tail.poll()
    replacement = tmp_path / "new.jsonl"
    replacement.write_text(user("b") + user("c") + user("d"), encoding="utf-8")
    os.replace(replacement, path)
    update = tail.poll()
    assert update.reset
    assert [e["message"]["content"] for e in update.entries] == ["b", "c", "d"]


def test_an_in_place_rewrite_that_grew_resets_the_tail(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(user("first"), encoding="utf-8")
    tail = make_tail(path)
    tail.poll()
    inode = path.stat().st_ino
    with path.open("r+b") as handle:
        handle.truncate(0)
        handle.write((user("something else") + user("and more")).encode())
    assert path.stat().st_ino == inode
    update = tail.poll()
    assert update.reset
    assert [e["message"]["content"] for e in update.entries] == ["something else", "and more"]


def test_a_missing_file_resets_once_and_a_reappearing_one_starts_over(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    tail = make_tail(path)
    never = tail.poll()
    assert (never.exists, never.reset) == (False, False)
    path.write_text(user("a"), encoding="utf-8")
    assert len(tail.poll().entries) == 1
    path.unlink()
    gone = tail.poll()
    assert (gone.exists, gone.reset) == (False, True)
    assert tail.poll().reset is False
    path.write_text(user("b"), encoding="utf-8")
    back = tail.poll()
    assert [e["message"]["content"] for e in back.entries] == ["b"]


def test_a_large_file_is_read_over_several_polls(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    lines = [user(f"prompt {i}") for i in range(20)]
    path.write_text("".join(lines), encoding="utf-8")
    tail = make_tail(path, max_read=len(lines[0]) * 3 + 5)
    seen: list[str] = []
    polls = 0
    while True:
        update = tail.poll()
        seen += [e["message"]["content"] for e in update.entries]
        polls += 1
        if not update.more:
            break
    assert polls > 3
    assert seen == [f"prompt {i}" for i in range(20)]


def test_an_over_long_line_is_skipped_not_buffered(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    huge = user("x" * 5000)
    path.write_text(user("a") + huge + user("b"), encoding="utf-8")
    tail = make_tail(path, max_line=1000, max_read=700)
    seen: list[str] = []
    while True:
        update = tail.poll()
        seen += [e["message"]["content"] for e in update.entries]
        assert tail.carried <= 1000
        if not update.more:
            break
    assert seen == ["a", "b"]


def test_a_filesystem_without_ranged_reads_is_tailed_by_slicing() -> None:
    fs = FakeFS({"/p/t.jsonl": user("a")})
    tail = transcript.TranscriptTail(fs, Path("/p/t.jsonl"))
    assert len(tail.poll().entries) == 1
    fs.add_file("/p/t.jsonl", user("a") + user("b"))
    assert [e["message"]["content"] for e in tail.poll().entries] == ["b"]
    fs.replace_file("/p/t.jsonl", user("c"))
    update = tail.poll()
    assert update.reset
    assert [e["message"]["content"] for e in update.entries] == ["c"]


# --------------------------------------------------------------------------------------------
# Locating the file
# --------------------------------------------------------------------------------------------


def test_project_slug_replaces_every_non_alphanumeric_character() -> None:
    assert transcript.project_slug("/a/b.c/.d_e") == "-a-b-c--d-e"


def test_locate_tries_the_cwd_slug_then_searches(tmp_path: Path) -> None:
    fs = LocalFileSystem()
    slugged = tmp_path / transcript.project_slug("/work/proj")
    slugged.mkdir()
    (slugged / "s-1.jsonl").write_text("", encoding="utf-8")
    other = tmp_path / "-elsewhere"
    other.mkdir()
    (other / "s-2.jsonl").write_text("", encoding="utf-8")
    assert transcript.locate(fs, tmp_path, "s-1", "/work/proj") == slugged / "s-1.jsonl"
    assert transcript.locate(fs, tmp_path, "s-2", "/work/proj") == other / "s-2.jsonl"
    assert transcript.locate(fs, tmp_path, "s-2", None) == other / "s-2.jsonl"
    assert transcript.locate(fs, tmp_path, "s-3", "/work/proj") is None


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a b", "x" * 201, "s.jsonl"])
def test_locate_never_turns_a_strange_session_id_into_a_path(tmp_path: Path, bad: str) -> None:
    assert transcript.locate(LocalFileSystem(), tmp_path, bad, None) is None


def test_locate_without_a_projects_directory_finds_nothing(tmp_path: Path) -> None:
    assert transcript.locate(LocalFileSystem(), tmp_path / "none", "s-1", None) is None


# --------------------------------------------------------------------------------------------
# /clear rotation (ratatosk clear-model-session)
# --------------------------------------------------------------------------------------------


def _install_clear_fixture(projects: Path) -> Path:
    directory = projects / transcript.project_slug(CWD)
    directory.mkdir(parents=True)
    shutil.copy(CLEAR / "transcript-before-clear.jsonl", directory / f"{BEFORE_ID}.jsonl")
    shutil.copy(CLEAR / "transcript-after-clear.jsonl", directory / f"{AFTER_ID}.jsonl")
    return directory


def test_clear_rotation_gives_each_session_its_own_transcript_and_facts(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    _install_clear_fixture(projects)
    fs = LocalFileSystem()
    before = transcript.locate(fs, projects, BEFORE_ID, CWD)
    after = transcript.locate(fs, projects, AFTER_ID, CWD)
    assert before is not None
    assert after is not None
    assert before != after
    old = transcript.SessionTranscript(transcript.TranscriptTail(fs, before))
    new = transcript.SessionTranscript(transcript.TranscriptTail(fs, after))
    assert old.update() is True
    assert new.update() is True
    # Before the /clear: one typed prompt and its reply. After it: only the slash commands.
    assert old.facts()["prompts"] == ["Say: one"]
    assert old.facts()["said"] == "one"
    assert new.facts()["prompts"] == []
    assert new.facts()["said"] == ""
    assert new.facts()["turns"] == 3  # the harness's own command lines count as typed turns
    assert old.facts()["model"] == "claude-opus-4-8"


def test_the_old_transcript_stays_put_after_clear(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    directory = _install_clear_fixture(projects)
    old = transcript.SessionTranscript(
        transcript.TranscriptTail(LocalFileSystem(), directory / f"{BEFORE_ID}.jsonl")
    )
    old.update()
    snapshot = old.facts()
    assert old.update() is False  # a finished transcript yields nothing new
    assert old.facts() == snapshot


def test_session_transcript_rebuilds_its_facts_when_the_file_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(user("first prompt") + assistant(text("hello")), encoding="utf-8")
    session = transcript.SessionTranscript(make_tail(path))
    assert session.update() is True
    assert session.facts()["prompts"] == ["first prompt"]
    path.write_text(user("other") + user("another") + user("more"), encoding="utf-8")
    assert session.update() is True
    assert session.facts()["prompts"] == ["other", "another", "more"]
    assert session.facts()["said"] == ""
    path.unlink()
    assert session.update() is True
    assert session.present is False
    assert session.facts()["turns"] == 0


# --------------------------------------------------------------------------------------------
# Facts details that the parity goldens do not reach
# --------------------------------------------------------------------------------------------


def _entries(*lines: str) -> list[dict[str, Any]]:
    return [e for e in (facts.decode_line(item) for item in lines) if e is not None]


def test_the_task_tools_make_the_task_list() -> None:
    path = CLAUDE_TRANSCRIPTS / "task-tools-synthetic.jsonl"
    result = facts.facts_of(_entries(*path.read_text(encoding="utf-8").splitlines()))
    assert result["tasks"] == [
        {"status": "in_progress", "content": "write the schema"},
        {"status": "pending", "content": "backfill the users table"},
    ]
    assert result["tools"] == {"TaskCreate": 3, "TaskUpdate": 4}
    assert facts.open_task(result["tasks"]) == "write the schema"
    assert result["branch"] == "feature/x"
    assert result["turns"] == 1


def test_todowrite_replaces_the_task_list_and_later_task_tools_extend_it() -> None:
    todos = [{"status": "in_progress", "content": "x" * 100, "activeForm": "Doing"}, "junk"]
    result = facts.facts_of(
        _entries(
            assistant(tool_use("1", "TaskCreate", subject="old")),
            assistant(tool_use("2", "TodoWrite", todos=todos)),
            assistant(tool_use("3", "TaskCreate", subject="new")),
            assistant(tool_use("4", "TaskUpdate", taskId=1, status="completed")),
        )
    )
    assert result["tasks"] == [
        {"status": "in_progress", "content": "x" * 80},
        {"status": "completed", "content": "new"},
    ]


def test_the_task_list_is_capped_at_twenty() -> None:
    todos = [{"status": "pending", "content": f"t{i}"} for i in range(30)]
    result = facts.facts_of(_entries(assistant(tool_use("1", "TodoWrite", todos=todos))))
    assert len(result["tasks"]) == 20


@pytest.mark.parametrize(
    "update",
    [
        {"taskId": True, "status": "completed"},
        {"taskId": [1], "status": "completed"},
        {"status": "completed"},
        {"taskId": "7", "status": "completed"},
    ],
)
def test_a_task_update_that_names_no_known_task_changes_nothing(update: dict[str, Any]) -> None:
    result = facts.facts_of(
        _entries(
            assistant(tool_use("1", "TaskCreate", subject="a")),
            assistant(tool_use("2", "TaskUpdate", **update)),
        )
    )
    assert result["tasks"] == [{"status": "pending", "content": "a"}]


def test_malformed_values_are_ignored_not_fatal() -> None:
    entries = _entries(
        line(type="assistant", message="not an object"),
        line(type="assistant", message={"content": "a string", "model": 5}),
        line(type="assistant", message={"content": [1, "x", {"type": "tool_use", "name": 7}]}),
        line(type="assistant", message={"content": [tool_use("1", "TodoWrite", todos="no")]}),
        line(type="assistant", message={"content": [tool_use("1", "TaskCreate", subject=5)]}),
        line(type="user", message={"content": [1, {"type": "text", "text": None}]}),
        line(type="user", message={"content": 5}),
        line(type="user", message="x"),
        line(type="ai-title", aiTitle=None),
        line(type="mode", mode=3),
        line(timestamp=17, gitBranch=None),
        line(timestamp="not a time"),
    )
    result = facts.facts_of(entries)
    assert result["title"] == ""
    assert result["mode"] == ""
    assert result["model"] == ""
    assert result["started"] == 0
    assert result["last_at"] == 0
    assert result["tools"] == {"TaskCreate": 1, "TodoWrite": 1}
    assert result["tasks"] == [{"status": "pending", "content": ""}]
    assert result["turns"] == 0  # no user line carries any text


def test_epoch_matches_jqs_fromdateiso8601() -> None:
    assert facts.epoch("2026-06-16T12:35:47.537Z") == 1781613347
    assert facts.epoch("2026-06-16T12:35:47Z") == 1781613347
    assert facts.epoch("2026-06-16T12:35:47+02:00") == 0
    assert facts.epoch("") == 0
    assert facts.epoch(None) == 0
    assert facts.epoch(5) == 0


def test_a_user_message_is_a_typed_prompt_only_when_a_person_wrote_it() -> None:
    result = facts.facts_of(
        _entries(
            user("typed  with\n  spaces"),
            user("a meta line", isMeta=True),
            user("a sidechain line", isSidechain=True),
            user("<command-name>/clear</command-name>"),
            line(
                type="user",
                message={
                    "content": [{"type": "text", "text": "with"}, {"type": "text", "text": "image"}]
                },
            ),
            tool_result("t-1"),
        )
    )
    assert result["turns"] == 3
    assert result["prompts"] == ["typed with spaces", "with image"]


def test_only_the_last_thirty_prompts_are_kept_each_cut_to_160() -> None:
    entries = _entries(*[user(f"p{i} " + "w" * 200) for i in range(35)])
    result = facts.facts_of(entries)
    assert len(result["prompts"]) == 30
    assert result["prompts"][0].startswith("p5 ")
    assert all(len(p) == 160 for p in result["prompts"])
    assert result["turns"] == 35


def test_sidechain_replies_are_not_what_the_session_said() -> None:
    result = facts.facts_of(
        _entries(
            assistant(text("main reply")),
            assistant(text("subagent reply"), isSidechain=True),
        )
    )
    assert result["said"] == "main reply"


def test_the_files_are_the_first_twenty_distinct_paths_sorted() -> None:
    blocks = [tool_use(f"t{i}", "Edit", file_path=f"/f/{(i * 7) % 25:02d}.py") for i in range(30)]
    blocks.append(tool_use("w", "Read", file_path="/f/never.py"))
    result = facts.facts_of(_entries(assistant(*blocks)))
    assert result["files"] == [f"/f/{i:02d}.py" for i in range(20)]


def test_the_running_tool_is_the_newest_call_without_a_result() -> None:
    entries = _entries(
        *(CLAUDE_TRANSCRIPTS / "running-tool-synthetic.jsonl").read_text().splitlines()
    )
    accumulator = facts.FactsAccumulator()
    accumulator.feed_all(entries)
    pending = accumulator.pending_tool()
    assert pending is not None
    assert (pending.name, pending.target) == ("Bash", "pytest")  # the command head only
    assert "Bash" in repr(pending)
    accumulator.feed_all(_entries(tool_result("tu-b")))
    assert accumulator.pending_tool() is None


def test_a_new_prompt_ends_whatever_was_running() -> None:
    accumulator = facts.FactsAccumulator()
    accumulator.feed_all(_entries(assistant(tool_use("1", "Bash", command="sleep 9"))))
    assert accumulator.pending_tool() is not None
    accumulator.feed_all(_entries(user("stop")))
    assert accumulator.pending_tool() is None


def test_remembered_tool_calls_are_bounded() -> None:
    accumulator = facts.FactsAccumulator()
    calls = [tool_use(f"t{i}", "Bash", command=f"c{i}") for i in range(facts.MAX_OPEN_TOOLS + 10)]
    accumulator.feed_all(_entries(assistant(*calls)))
    pending = accumulator.pending_tool()
    assert pending is not None
    assert pending.target == f"c{facts.MAX_OPEN_TOOLS + 9}"
    accumulator.feed_all(_entries(tool_result("t0")))  # forgotten long ago: not an error
    assert accumulator.lines == 2


@pytest.mark.parametrize(
    ("tool_input", "target"),
    [
        ({"file_path": "/a/b.py", "command": "ls"}, "/a/b.py"),
        ({"notebook_path": "/a/n.ipynb"}, "/a/n.ipynb"),
        ({"path": "/src"}, "/src"),
        ({"command": "  git status --short"}, "git"),
        ({"command": "   "}, None),
        ({"url": "https://user:pw@example.com:8443/a?q=1"}, "example.com"),
        ({"url": "http://[::1"}, None),
        ({"url": ""}, None),
        ({"pattern": "x"}, None),
        ("not a mapping", None),
        (None, None),
    ],
)
def test_tool_target(tool_input: object, target: str | None) -> None:
    assert facts.tool_target("Any", tool_input) == target


def test_open_task_is_the_first_task_in_progress() -> None:
    tasks = [
        {"status": "pending", "content": "a"},
        {"status": "in_progress", "content": ""},
        {"status": "in_progress", "content": "b"},
        {"status": "in_progress", "content": "c"},
    ]
    assert facts.open_task(tasks) == "b"
    assert facts.open_task(tasks[:2]) is None
    assert facts.open_task([]) is None


def _away_summary(content: str, ts: str, **extra: Any) -> str:
    return line(type="system", subtype="away_summary", content=content, timestamp=ts, **extra)


def test_the_newest_away_summary_is_the_recap_without_the_config_hint() -> None:
    accumulator = facts.FactsAccumulator()
    accumulator.feed_all(
        _entries(
            user("hi"),
            _away_summary("Old recap.", "2026-10-05T10:01:00.000Z", gitBranch="main"),
            _away_summary(
                "We are  writing\nthe parser; next, run the tests. (disable recaps in /config)",
                "2026-10-05T10:30:00.500Z",
                gitBranch="topic",
            ),
        )
    )
    assert accumulator.recap() == facts.RecapLine(
        "We are writing the parser; next, run the tests.",
        facts.epoch("2026-10-05T10:30:00Z"),
        "topic",
    )
    assert "recap" not in accumulator.facts()  # the facts block keeps parity with Lidskjalv


@pytest.mark.parametrize(
    "entry",
    [
        _away_summary("", "2026-10-05T10:30:00Z"),
        _away_summary(" (disable recaps in /config)", "2026-10-05T10:30:00Z"),
        _away_summary("A recap.", "not a time"),
        _away_summary("A recap.", "2026-10-05T10:30:00Z", isSidechain=True),
        line(type="system", subtype="other", content="A recap.", timestamp="2026-10-05T10:30:00Z"),
    ],
)
def test_lines_that_are_no_recap_leave_none(entry: str) -> None:
    accumulator = facts.FactsAccumulator()
    accumulator.feed_all(_entries(entry))
    assert accumulator.recap() is None


def test_a_recap_is_cut_to_its_cap() -> None:
    accumulator = facts.FactsAccumulator()
    accumulator.feed_all(_entries(_away_summary("r" * 700, "2026-10-05T10:30:00Z")))
    recap = accumulator.recap()
    assert recap is not None
    assert (recap.text, recap.branch) == ("r" * facts.RECAP_CHARS, None)


# --------------------------------------------------------------------------------------------
# The collector attaches the facts to the session and its activity
# --------------------------------------------------------------------------------------------


def _transcript_path(world: World, session_id: str, cwd: str) -> Path:
    directory = world.registry.parent / "projects" / transcript.project_slug(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{session_id}.jsonl"


def _activity(world: World, key: str) -> SessionActivity:
    snapshots = [r for r in world.sink.records if isinstance(r, SessionActivitySnapshot)]
    return {i.entity_id: i.payload for i in snapshots[-1].items}[key]


async def test_the_collector_attaches_facts_state_and_the_running_tool(tmp_path: Path) -> None:
    world = World(tmp_path / "sessions")
    world.add(4201, "s-201", status="busy", name="the name", cwd="/work/proj")
    path = _transcript_path(world, "s-201", "/work/proj")
    path.write_text(
        line(type="ai-title", aiTitle="Fix the build")
        + user("fix the build", gitBranch="main", version="2.1.289")
        + line(type="last-prompt", lastPrompt="fix the build")
        + assistant(text("Looking."), tool_use("c1", "Edit", file_path="/work/proj/a.py")),
        encoding="utf-8",
    )
    assert await world.collector.poll() is True
    activity = _activity(world, "claude:s-201")
    assert (activity.state, activity.title, activity.branch) == ("working", "Fix the build", "main")
    assert (activity.current_tool, activity.current_target) == ("Edit", "/work/proj/a.py")
    assert (activity.last_prompt, activity.last_reply) == ("fix the build", "Looking.")
    assert activity.facts is not None
    assert activity.facts["files"] == ["/work/proj/a.py"]
    sessions = [r for r in world.sink.records if isinstance(r, SessionSnapshot)][-1]
    payload = sessions.items[0].payload
    assert (payload.model, payload.branch) == ("claude-opus-4-8", "main")

    append(
        path, tool_result("c1") + assistant(text("Done. All green."), ts="2026-10-05T10:01:00.000Z")
    )
    await world.clock.advance(2)
    assert await world.collector.poll() is True
    later = _activity(world, "claude:s-201")
    assert later.current_tool is None
    assert later.last_reply == "Done. All green."


async def test_a_session_without_a_transcript_is_listed_from_its_row(tmp_path: Path) -> None:
    world = World(tmp_path / "sessions")
    world.add(4202, "s-202", status="waiting", waitingFor="input needed", name="registry name")
    assert await world.collector.poll() is True
    activity = _activity(world, "claude:s-202")
    assert activity.title == "registry name"
    assert activity.facts is None
    assert (activity.state, activity.waiting_reason) == ("waiting", "input needed")


async def test_the_collector_attaches_the_recap_to_the_activity(tmp_path: Path) -> None:
    world = World(tmp_path / "sessions")
    world.add(4206, "s-206", status="idle", cwd="/work/proj")
    path = _transcript_path(world, "s-206", "/work/proj")
    path.write_text(user("hi"), encoding="utf-8")
    await world.collector.poll()
    assert _activity(world, "claude:s-206").recap is None
    append(path, _away_summary("Fixing the build.", "2026-10-05T10:30:00Z", gitBranch="main"))
    await world.clock.advance(2)
    await world.collector.poll()
    recap = _activity(world, "claude:s-206").recap
    assert recap is not None
    assert (recap.text, recap.at, recap.branch) == (
        "Fixing the build.",
        datetime(2026, 10, 5, 10, 30, tzinfo=UTC),
        "main",
    )


async def test_a_vanishing_transcript_is_looked_for_again(tmp_path: Path) -> None:
    world = World(tmp_path / "sessions")
    world.add(4203, "s-203", status="idle", cwd="/work/proj")
    path = _transcript_path(world, "s-203", "/work/proj")
    path.write_text(line(type="ai-title", aiTitle="First") + user("hi"), encoding="utf-8")
    await world.collector.poll()
    assert _activity(world, "claude:s-203").title == "First"
    path.unlink()
    await world.clock.advance(2)
    await world.collector.poll()
    assert _activity(world, "claude:s-203").facts is None
    path.write_text(line(type="ai-title", aiTitle="Second") + user("hi"), encoding="utf-8")
    await world.clock.advance(2)
    await world.collector.poll()
    assert _activity(world, "claude:s-203").title == "Second"


async def test_an_unreadable_transcript_does_not_stop_the_session_being_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_path / "sessions")
    world.add(4204, "s-204", status="idle", cwd="/work/proj")
    _transcript_path(world, "s-204", "/work/proj").write_text(user("hi"), encoding="utf-8")

    def broken(*_: Any, **__: Any) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(LocalFileSystem, "read_range", broken)
    assert await world.collector.poll() is True
    assert _activity(world, "claude:s-204").facts is None


async def test_clear_rotation_follows_the_new_session_and_drops_the_old_tail(
    tmp_path: Path,
) -> None:
    world = World(tmp_path / "sessions")
    projects = world.registry.parent / "projects"
    _install_clear_fixture(projects)
    row = row_data(4205, BEFORE_ID, status="idle", cwd=CWD)
    world.ps.starts[4205] = T0
    write_row(world.registry, row)
    await world.collector.poll()
    assert _activity(world, f"claude:{BEFORE_ID}").facts is not None
    assert _activity(world, f"claude:{BEFORE_ID}").facts["prompts"] == ["Say: one"]  # type: ignore[index]
    write_row(world.registry, row_data(4205, AFTER_ID, status="idle", cwd=CWD))
    await world.clock.advance(2)
    await world.collector.poll()
    after = _activity(world, f"claude:{AFTER_ID}")
    assert after.facts is not None
    assert after.facts["prompts"] == []
    assert list(world.collector._transcripts) == [AFTER_ID]  # noqa: SLF001
    sessions = [r for r in world.sink.records if isinstance(r, SessionSnapshot)][-1]
    assert sessions.items[0].payload.predecessor_key == f"claude:{BEFORE_ID}"


def test_the_fixture_is_the_shape_the_hook_events_describe() -> None:
    events = [
        json.loads(item)
        for item in (CLEAR / "hook-events.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    clear = [e for e in events if e.get("event") == "SessionEnd"]
    assert [e["reason"] for e in clear] == ["clear"]
