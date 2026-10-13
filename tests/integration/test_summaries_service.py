"""The summary service (WP8.1): the job queue, the cache gates and the runner, with a fake model.

The daemon is real (writer, live view, ``/now``); :class:`~summary_support.FakeRunner` answers in
place of ``claude -p``. Inputs are taken from ``/now`` as the API takes them. The real runner is
run against a stand-in ``claude`` script under ``sys.executable``, never against the real CLI.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from api_support import T0, client
from summary_support import (
    ARC,
    FOCUSED,
    FOLDER_ID,
    FOLDER_PARAGRAPH,
    NOW,
    OTHER_KEYS,
    PARAGRAPH,
    REPLY,
    SESSION_KEY,
    TODO_TEXT,
    FakeRunner,
    activity,
    set_activity,
    stored,
    story_world,
    wait_until,
)

from fylgja import paths
from fylgja.api.deps import item_builder
from fylgja.api.item_inputs import DEFAULT_DAYS
from fylgja.api.now import build_now
from fylgja.api.now_models import Now
from fylgja.api.summary_inputs import find_folder_inputs, find_inputs, folder_inputs
from fylgja.core.daemon import Daemon
from fylgja.core.items import Item, Place
from fylgja.core.summaries import (
    CLAUDE_ARGS,
    GUARD_ENV,
    MAX_CHARS,
    ClaudeRunner,
    RunOutput,
    ScopeKind,
    SummariesDisabled,
    SummaryInputs,
    SummaryState,
    claude_command,
    folder_prompt,
    input_digest,
    normalize,
    parse_story,
    parse_text,
    session_prompt,
    summary_environment,
    workspace_prompt,
)
from fylgja.testing.fakes import FakeClock

FAILURE_TEXT = "error: not logged in"


def inputs_of(
    daemon: Daemon, scope: ScopeKind = "session", ref: str = SESSION_KEY
) -> SummaryInputs:
    found = find_inputs(build_now(daemon.state()), scope, ref)
    assert found is not None, (scope, ref)
    return found


def picture(daemon: Daemon) -> tuple[Now, tuple[Item, ...]]:
    """``/now`` and the navigation items of it, as the summary route takes them."""
    now = build_now(daemon.state())
    data = daemon.rank_input(
        item_builder(daemon, lambda: now, days=DEFAULT_DAYS), days=DEFAULT_DAYS
    )
    return now, data.items


def folder_inputs_of(daemon: Daemon, ref: str = FOLDER_ID) -> SummaryInputs:
    now, items = picture(daemon)
    found = find_folder_inputs(items, now, ref)
    assert found is not None, ref
    return found


def clock_of(daemon: Daemon) -> FakeClock:
    assert isinstance(daemon.clock, FakeClock)
    return daemon.clock


async def generate(daemon: Daemon, scope: ScopeKind = "session", ref: str = SESSION_KEY) -> Any:
    answer = daemon.summaries.request(inputs_of(daemon, scope, ref))
    await daemon.summaries.drain()
    return answer


# --------------------------------------------------------------------------------------------
# One job, the cache and the store
# --------------------------------------------------------------------------------------------


async def test_a_first_request_queues_a_job_that_stores_arc_and_now() -> None:
    runner = FakeRunner()
    async with story_world(runner) as daemon:
        inputs = inputs_of(daemon)
        first = daemon.summaries.request(inputs)
        assert first.state is SummaryState.PENDING
        assert daemon.summaries.pending("session", SESSION_KEY)
        await daemon.summaries.drain()

        answer = daemon.summaries.answer("session", SESSION_KEY, inputs)
        assert answer is not None
        assert (answer.state, answer.arc, answer.now) == (SummaryState.FRESH, ARC, NOW)
        assert answer.generated_at == T0
        assert answer.input_digest == inputs.digest
        assert answer.error_kind is None
        assert len(runner.calls) == 1
        assert daemon.summaries.calls == 1

        rows = stored(daemon)
        assert [(r["scope_kind"], r["scope_ref"], r["kind"], r["text"]) for r in rows] == [
            ("session", SESSION_KEY, "arc", ARC),
            ("session", SESSION_KEY, "now", NOW),
        ]
        assert {r["input_digest"] for r in rows} == {inputs.digest}
        assert {r["model"] for r in rows} == {"haiku"}
        assert {r["error_kind"] for r in rows} == {None}
        assert all(r["generated_at"] for r in rows)


async def test_the_prompt_carries_the_facts_block_and_the_one_liner() -> None:
    runner = FakeRunner()
    async with story_world(runner) as daemon:
        await generate(daemon)
        prompt = runner.calls[0].prompt
        for expected in (
            "Agent: claude",
            "Project: /work/alpha",
            "Conversation title: Fix the login form",
            "Branch: fix/login",
            "State: working",
            "What it is doing now: ",
            "Turns so far: 4",
            "Tools used: Read×5 Edit×3 Bash×2",
            "Files edited: form.py, login.py",
            "Still open: validate the email",
            "Their last message: make the form validate",
            "Your last reply: I changed the validator.",
            "Everything they have asked in this session, oldest first:",
            "  1. look at the login form",
            "  2. make the form validate",
            "Previous ARC: (none yet)",
            "Previous NOW: (none yet)",
            "ARC: one sentence",
            "NOW: two short sentences",
        ):
            assert expected in prompt, expected
        assert "read the code" not in prompt  # a completed task is not "still open"

        # the next prompt shows the story it replaces
        await set_activity(
            daemon, {SESSION_KEY: activity(turns=5, last_at=T0 + timedelta(seconds=10))}
        )
        await clock_of(daemon).advance(301)
        await generate(daemon)
        assert f"Previous ARC: {ARC}" in runner.calls[1].prompt
        assert f"Previous NOW: {NOW}" in runner.calls[1].prompt


async def test_the_cache_survives_a_restart() -> None:
    runner = FakeRunner()
    async with story_world(runner) as daemon:
        await generate(daemon)
        database = daemon.writer.database
    assert database.exists()
    again = FakeRunner()
    async with story_world(again) as daemon:
        assert daemon.writer.database == database
        answer = daemon.summaries.request(inputs_of(daemon))
        assert (answer.state, answer.arc, answer.now) == (SummaryState.FRESH, ARC, NOW)
        assert again.calls == []


# --------------------------------------------------------------------------------------------
# The gates: digest, TTL, newer than the transcript
# --------------------------------------------------------------------------------------------


async def test_an_unchanged_digest_makes_no_call_even_after_the_ttl() -> None:
    runner = FakeRunner()
    async with story_world(runner) as daemon:
        await generate(daemon)
        for _ in range(3):
            assert daemon.summaries.request(inputs_of(daemon)).state is SummaryState.FRESH
        await clock_of(daemon).advance(3600)
        assert daemon.summaries.request(inputs_of(daemon)).state is SummaryState.FRESH
        assert len(runner.calls) == 1


async def test_a_changed_digest_inside_the_ttl_is_stale_and_regenerates_after_it() -> None:
    runner = FakeRunner()
    async with story_world(runner) as daemon:
        await generate(daemon)
        old = inputs_of(daemon).digest
        # the transcript moved on: a new turn, after the story
        await clock_of(daemon).advance(60)
        await set_activity(
            daemon, {SESSION_KEY: activity(turns=5, last_at=T0 + timedelta(seconds=30))}
        )
        changed = inputs_of(daemon)
        assert changed.digest != old

        inside = daemon.summaries.request(changed)
        assert (inside.state, inside.arc, inside.input_digest) == (SummaryState.STALE, ARC, old)
        stale = daemon.summaries.answer("session", SESSION_KEY, changed)
        assert stale is not None
        assert stale.state is SummaryState.STALE
        assert len(runner.calls) == 1

        await clock_of(daemon).advance(239)  # 299 s after the attempt: still inside
        assert daemon.summaries.request(inputs_of(daemon)).state is SummaryState.STALE
        await clock_of(daemon).advance(1)  # 300 s: the TTL has passed
        assert daemon.summaries.request(inputs_of(daemon)).state is SummaryState.PENDING
        await daemon.summaries.drain()
        assert len(runner.calls) == 2
        fresh = daemon.summaries.answer("session", SESSION_KEY, inputs_of(daemon))
        assert fresh is not None
        assert fresh.state is SummaryState.FRESH
        assert fresh.input_digest == changed.digest
        assert fresh.generated_at == T0 + timedelta(seconds=300)


async def test_a_story_newer_than_its_transcript_is_never_regenerated() -> None:
    runner = FakeRunner()
    async with story_world(runner) as daemon:
        await generate(daemon)  # the story describes T0; the transcript's last line is older
        # the state and so the one-liner change, the transcript does not
        await clock_of(daemon).advance(600)
        await set_activity(daemon, {SESSION_KEY: activity(state="waiting_input")})
        moved = inputs_of(daemon)
        cached = daemon.summaries.cached("session", SESSION_KEY)
        assert cached is not None
        assert moved.digest != cached.input_digest
        assert moved.activity_at is not None
        assert cached.generated_at is not None
        assert moved.activity_at <= cached.generated_at
        for _ in range(2):
            answer = daemon.summaries.request(moved)
            assert (answer.state, answer.arc) == (SummaryState.FRESH, ARC)
            await clock_of(daemon).advance(3600)
        assert len(runner.calls) == 1

        # a transcript line newer than the story: regenerated (the TTL passed long ago)
        newer = T0 + timedelta(hours=1)
        await set_activity(daemon, {SESSION_KEY: activity(last_at=newer, turns=6)})
        assert daemon.summaries.request(inputs_of(daemon)).state is SummaryState.PENDING
        await daemon.summaries.drain()
        assert len(runner.calls) == 2


async def test_a_reply_with_one_line_keeps_the_other_from_the_previous_story() -> None:
    runner = FakeRunner([RunOutput(0, REPLY), RunOutput(0, "NOW: Testing the validator.")])
    async with story_world(runner) as daemon:
        await generate(daemon)
        await set_activity(
            daemon, {SESSION_KEY: activity(turns=8, last_at=T0 + timedelta(seconds=5))}
        )
        await clock_of(daemon).advance(301)
        await generate(daemon)
        cached = daemon.summaries.cached("session", SESSION_KEY)
        assert cached is not None
        assert (cached.arc, cached.now, cached.error_kind) == (ARC, "Testing the validator.", None)


async def test_a_session_without_a_transcript_time_follows_the_digest_alone() -> None:
    runner = FakeRunner()
    async with story_world(runner) as daemon:
        base = inputs_of(daemon)
        plain = SummaryInputs.create("session", SESSION_KEY, dict(base.data))
        assert plain.activity_at is None
        assert plain.digest == base.digest
        daemon.summaries.request(plain)
        await daemon.summaries.drain()
        await clock_of(daemon).advance(301)
        moved = SummaryInputs.create("session", SESSION_KEY, {**base.data, "state": "idle"})
        assert daemon.summaries.request(moved).state is SummaryState.PENDING
        await daemon.summaries.drain()
        assert len(runner.calls) == 2


# --------------------------------------------------------------------------------------------
# The queue: joining and the concurrency cap
# --------------------------------------------------------------------------------------------


async def test_joined_concurrent_requests_make_one_runner_call() -> None:
    runner = FakeRunner()
    runner.hold()
    async with story_world(runner) as daemon:
        inputs = inputs_of(daemon)
        answers = [daemon.summaries.request(inputs) for _ in range(5)]
        await wait_until(lambda: len(runner.calls) == 1)
        # requests that arrive while the call runs join it too, also with newer inputs
        await set_activity(daemon, {SESSION_KEY: activity(turns=9, last_at=T0)})
        answers.append(daemon.summaries.request(inputs_of(daemon)))
        assert {a.state for a in answers} == {SummaryState.PENDING}
        late = daemon.summaries.answer("session", SESSION_KEY, inputs)
        assert late is not None
        assert late.state is SummaryState.PENDING
        runner.release()
        await daemon.summaries.drain()
        assert len(runner.calls) == 1
        assert daemon.summaries.calls == 1
        done = daemon.summaries.answer("session", SESSION_KEY, inputs)
        assert done is not None
        assert done.state is SummaryState.FRESH


async def test_concurrent_requests_through_tasks_share_one_job() -> None:
    runner = FakeRunner()
    runner.hold()
    async with story_world(runner) as daemon:

        async def ask() -> SummaryState:
            await asyncio.sleep(0)
            return daemon.summaries.request(inputs_of(daemon)).state

        states = await asyncio.gather(*(ask() for _ in range(8)))
        assert set(states) == {SummaryState.PENDING}
        runner.release()
        await daemon.summaries.drain()
        assert len(runner.calls) == 1


async def test_the_concurrency_cap_is_honoured() -> None:
    runner = FakeRunner()
    runner.hold()
    async with story_world(runner, max_concurrent=2, others=4) as daemon:
        assert daemon.summaries.max_concurrent == 2
        keys = (SESSION_KEY, *OTHER_KEYS[:4])
        for key in keys:
            assert daemon.summaries.request(inputs_of(daemon, "session", key)).state is (
                SummaryState.PENDING
            )
        await wait_until(lambda: len(runner.calls) == 2)
        for _ in range(20):
            await asyncio.sleep(0)
        assert (len(runner.calls), runner.in_flight, daemon.summaries.running) == (2, 2, 2)
        assert all(daemon.summaries.pending("session", key) for key in keys)
        runner.release()
        await daemon.summaries.drain()
        assert len(runner.calls) == 5
        assert runner.peak == 2
        assert daemon.summaries.peak_running == 2
        for key in keys:
            answer = daemon.summaries.answer("session", key, inputs_of(daemon, "session", key))
            assert answer is not None
            assert answer.state is SummaryState.FRESH


# --------------------------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------------------------


async def test_a_call_that_outlives_the_timeout_fails_with_an_error_kind() -> None:
    runner = FakeRunner()
    runner.hold()  # the model never answers
    async with story_world(runner) as daemon:
        assert daemon.summaries.request(inputs_of(daemon)).state is SummaryState.PENDING
        await wait_until(lambda: len(runner.calls) == 1)
        assert runner.calls[0].timeout == 60.0
        await clock_of(daemon).advance(59)
        assert daemon.summaries.pending("session", SESSION_KEY)
        await clock_of(daemon).advance(1)
        await daemon.summaries.drain()
        assert runner.in_flight == 0  # the call was cancelled
        answer = daemon.summaries.answer("session", SESSION_KEY, inputs_of(daemon))
        assert answer is not None
        assert (answer.state, answer.error_kind, answer.arc) == (
            SummaryState.FAILED,
            "timeout",
            None,
        )
        assert {(r["kind"], r["text"], r["error_kind"]) for r in stored(daemon)} == {
            ("arc", None, "timeout"),
            ("now", None, "timeout"),
        }
        # inside the TTL the failure is answered as it is; after it the next request retries
        assert daemon.summaries.request(inputs_of(daemon)).state is SummaryState.FAILED
        assert len(runner.calls) == 1
        runner.release()
        await clock_of(daemon).advance(300)
        assert daemon.summaries.request(inputs_of(daemon)).state is SummaryState.PENDING
        await daemon.summaries.drain()
        answer = daemon.summaries.answer("session", SESSION_KEY, inputs_of(daemon))
        assert answer is not None
        assert (answer.state, answer.error_kind) == (
            SummaryState.FRESH,
            None,
        )


@pytest.mark.parametrize(
    ("script", "kind"),
    [
        (TimeoutError(), "timeout"),
        (FileNotFoundError("claude"), "not_installed"),
        (PermissionError("claude"), "spawn_failed"),
        (RuntimeError("boom"), "runner_error"),
        (RunOutput(1, FAILURE_TEXT), "nonzero_exit"),
        (RunOutput(0, "  \n "), "empty_output"),
        (RunOutput(0, "ARC:\nNOW: **"), "empty_output"),
    ],
)
async def test_each_failure_has_its_error_kind_and_keeps_the_previous_story(
    script: Any, kind: str
) -> None:
    runner = FakeRunner([RunOutput(0, f"ARC: {ARC}\nNOW: {NOW}"), script])
    async with story_world(runner) as daemon:
        await generate(daemon)
        await set_activity(
            daemon, {SESSION_KEY: activity(turns=7, last_at=T0 + timedelta(seconds=1))}
        )
        await clock_of(daemon).advance(301)
        await generate(daemon)
        answer = daemon.summaries.answer("session", SESSION_KEY, inputs_of(daemon))
        assert answer is not None
        assert (answer.state, answer.error_kind) == (SummaryState.FAILED, kind)
        assert (answer.arc, answer.now, answer.generated_at) == (ARC, NOW, T0)
        assert {r["error_kind"] for r in stored(daemon)} == {kind}
        assert {r["text"] for r in stored(daemon)} == {ARC, NOW}
        assert FAILURE_TEXT not in json.dumps(stored(daemon))


async def test_a_summarizer_cwd_that_cannot_be_made_fails_without_a_call() -> None:
    runner = FakeRunner()
    async with story_world(runner) as daemon:
        paths.state_dir().mkdir(parents=True, exist_ok=True)
        paths.summarizer_dir().write_text("not a directory", encoding="utf-8")
        await generate(daemon)
        answer = daemon.summaries.answer("session", SESSION_KEY, inputs_of(daemon))
        assert answer is not None
        assert answer.error_kind == "cwd_unavailable"
        assert runner.calls == []


# --------------------------------------------------------------------------------------------
# Disabled: no model call, ever
# --------------------------------------------------------------------------------------------


async def test_with_summaries_disabled_no_claude_process_is_ever_spawned(tmp_path: Path) -> None:
    runner = FakeRunner()
    async with story_world(runner, enabled=False) as daemon:
        assert not daemon.summaries.enabled
        inputs = inputs_of(daemon)
        with pytest.raises(SummariesDisabled):
            daemon.summaries.request(inputs)
        answer = daemon.summaries.answer("session", SESSION_KEY, inputs)
        assert answer is not None
        assert answer.state is SummaryState.DISABLED
        async with client(daemon) as http:
            for scope, ref in (("session", SESSION_KEY), ("workspace", FOCUSED)):
                assert (await http.get(f"/summaries/{scope}/{ref}")).json()["state"] == "disabled"
            now = (await http.get("/now")).json()
        assert all("story" not in s for s in now["sessions"])
        assert all("story" not in w for w in now["workspaces"])
        await daemon.summaries.drain()
        assert runner.calls == []
        assert daemon.summaries.calls == 0
        assert stored(daemon) == []
        assert not paths.summarizer_dir().exists()


async def test_the_default_configuration_never_runs_the_real_runner(tmp_path: Path) -> None:
    """With the packaged defaults (summaries off) even the real runner, pointed at a stand-in
    ``claude`` that leaves a mark when it starts, is never started."""
    from api_support import make_daemon, sessions_collector, windows_collector

    mark = tmp_path / "spawned"
    script = _stand_in(tmp_path, f"open({str(mark)!r}, 'w').close()\nprint('NOW: x')\n")
    daemon = make_daemon(
        windows_collector(),
        sessions_collector(),
        summary_runner=ClaudeRunner([sys.executable, str(script)]),
    )
    await daemon.start()
    try:
        assert daemon.config.summaries.enabled is False
        inputs = SummaryInputs.create("session", SESSION_KEY, {"agent": "claude"})
        with pytest.raises(SummariesDisabled):
            daemon.summaries.request(inputs)
        await daemon.summaries.drain()
    finally:
        await daemon.stop()
    assert not mark.exists()
    assert daemon.summaries.calls == 0


# --------------------------------------------------------------------------------------------
# Workspaces
# --------------------------------------------------------------------------------------------


async def test_a_workspace_paragraph_from_its_label_sessions_context_and_todos() -> None:
    runner = FakeRunner(default=f"**{PARAGRAPH}**\n")
    async with story_world(runner) as daemon:
        inputs = inputs_of(daemon, "workspace", FOCUSED)
        assert inputs.activity_at is None
        await generate(daemon, "workspace", FOCUSED)
        prompt = runner.calls[0].prompt
        assert f"Workspace: {FOCUSED}" in prompt
        assert "At a glance: " in prompt
        assert "Projects: /work/alpha" in prompt
        assert "  - claude, working: " in prompt
        assert "Open in its windows:\n  - Term: zsh — alpha" in prompt
        assert f"Open todos:\n  - [open] {TODO_TEXT}" in prompt
        assert "one short paragraph" in prompt
        answer = daemon.summaries.answer("workspace", FOCUSED, inputs)
        assert answer is not None
        assert (answer.state, answer.text, answer.arc) == (SummaryState.FRESH, PARAGRAPH, None)
        assert [(r["kind"], r["text"]) for r in stored(daemon)] == [("text", PARAGRAPH)]


# --------------------------------------------------------------------------------------------
# Folders (docs/decisions/0053)
# --------------------------------------------------------------------------------------------


async def test_a_folder_job_runs_guarded_stores_redacted_text_and_keeps_its_digest() -> None:
    secret = "sk-" + "B" * 24
    runner = FakeRunner(default=f"**{FOLDER_PARAGRAPH}** It uses {secret} for now.\n")
    async with story_world(runner) as daemon:
        inputs = folder_inputs_of(daemon)
        assert (inputs.scope, inputs.ref, inputs.activity_at, inputs.settled) == (
            "folder",
            FOLDER_ID,
            None,
            False,
        )
        assert daemon.summaries.request(inputs).state is SummaryState.PENDING
        assert daemon.summaries.pending("folder", FOLDER_ID)
        await daemon.summaries.drain()

        call = runner.calls[0]
        assert {name: call.env.get(name) for name in GUARD_ENV} == dict(GUARD_ENV)
        assert call.env["FYLGJA_SUMMARY"] == "1"
        assert call.cwd == paths.summarizer_dir()
        for expected in (
            'folder "work/alpha"',
            "Name: work/alpha",
            "Agent sessions here:\n  - claude: ",
            "    Conversation title: Fix the login form",
            "at most three sentences",
        ):
            assert expected in call.prompt, expected
        assert "The session so far" not in call.prompt  # no session story is cached yet

        rows = stored(daemon)
        assert [(r["scope_kind"], r["scope_ref"], r["kind"]) for r in rows] == [
            ("folder", FOLDER_ID, "text")
        ]
        assert secret not in json.dumps(rows)
        assert rows[0]["text"].startswith(FOLDER_PARAGRAPH)
        assert (rows[0]["input_digest"], rows[0]["model"]) == (inputs.digest, "haiku")
        cached = daemon.summaries.cached("folder", FOLDER_ID)
        assert cached is not None
        assert cached.text is not None
        assert secret not in cached.text
        assert len(cached.text) <= MAX_CHARS

        # an hour later, nothing in the folder changed: the same digest, no call
        await clock_of(daemon).advance(3600)
        again = folder_inputs_of(daemon)
        assert again.digest == inputs.digest
        for _ in range(2):
            answer = daemon.summaries.request(again)
            assert (answer.state, answer.text) == (SummaryState.FRESH, cached.text)
        assert len(runner.calls) == 1


async def test_the_folder_inputs_carry_the_cached_session_arc() -> None:
    runner = FakeRunner()
    async with story_world(runner) as daemon:
        before = folder_inputs_of(daemon)
        assert "arc" not in before.data["sessions"][0]
        await generate(daemon)  # the session's ARC and NOW
        after = folder_inputs_of(daemon)
        assert after.data["sessions"][0]["arc"] == ARC
        assert after.digest != before.digest
        runner.default = FOLDER_PARAGRAPH
        daemon.summaries.request(after)
        await daemon.summaries.drain()
        assert f"    The session so far: {ARC}" in runner.calls[-1].prompt


async def test_an_unknown_item_id_has_no_folder_inputs() -> None:
    async with story_world() as daemon:
        now, items = picture(daemon)
        assert find_folder_inputs(items, now, "0000000000000000") is None
        assert {item.id for item in items} >= {FOLDER_ID}


def _place(ref: str, title: str, seconds: int, *, kind: str = "window") -> Place:
    return Place(
        kind=kind,  # type: ignore[arg-type]
        ref=ref,
        role="agent" if kind == "session" else "ide",
        label="claude" if kind == "session" else "PyCharm",
        title=title,
        workspace="3",
        open=seconds % 2 == 0,
        waiting=False,
        last_focus_at=T0 + timedelta(seconds=seconds),
    )


async def test_folder_inputs_hold_no_times_and_no_focus_order() -> None:
    async with story_world() as daemon:
        now, items = picture(daemon)
        item = next(i for i in items if i.id == FOLDER_ID)
        places = (_place("100", "main.py", 10), _place("102", "notes.md", 20))
        one = replace(item, places=places, last_focus_at=T0, seconds_today=60.0)
        two = replace(
            item,
            places=tuple(reversed([replace(p, last_focus_at=T0) for p in places])),
            last_focus_at=T0 + timedelta(hours=2),
            seconds_today=7200.0,
        )
        first, second = folder_inputs(one, now), folder_inputs(two, now)
        assert first.digest == second.digest
        assert first.data["places"] == [
            {"label": "PyCharm", "title": "main.py"},
            {"label": "PyCharm", "title": "notes.md"},
        ]
        assert not any(name.endswith(("_at", "seconds", "since")) for name in first.data)
        moved = replace(one, places=(_place("100", "test_main.py", 10), places[1]))
        assert folder_inputs(moved, now).digest != first.digest
        session = next(s for s in now.sessions or () if s.key == SESSION_KEY)
        with_session = replace(one, places=(*places, _place(SESSION_KEY, "x", 30, kind="session")))
        data = folder_inputs(with_session, now).data
        assert [s["agent"] for s in data["sessions"]] == [session.kind]


def test_the_folder_prompt_over_minimal_and_full_inputs() -> None:
    app = folder_prompt({"kind": "app", "name": "Term"})
    assert 'the\napp "Term"' in app
    assert "Agent sessions here" not in app
    assert "Open todos" not in app
    full = folder_prompt(
        {
            "kind": "folder",
            "name": "alpha/CBS-1",
            "group": "alpha",
            "tickets": ["CBS-1"],
            "branch": "CBS-1-login",
            "pr": "#12 open",
            "dirty": True,
            "places": [{"label": "PyCharm", "title": "login.py"}, {"label": "Term"}],
            "sessions": [{"agent": "codex"}, {"agent": "claude", "one_liner": "Edit form.py"}],
            "todos": [{"status": "next", "text": "ship it"}],
        }
    )
    for expected in (
        'folder "alpha/CBS-1"',
        "Group: alpha",
        "Tickets: CBS-1",
        "Branch: CBS-1-login",
        "Pull request: #12 open",
        "Uncommitted changes: yes",
        "Its windows:\n  - PyCharm: login.py\n  - Term: —",
        "Agent sessions here:\n  - codex: —\n  - claude: Edit form.py",
        "Open todos:\n  - [next] ship it",
        "a ticket key may be named as it is",
    ):
        assert expected in full, expected


async def test_a_scope_without_a_prompt_cannot_be_requested() -> None:
    async with story_world() as daemon:
        unknown = SummaryInputs.create("project", "x", {"spans": 3})  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="project"):
            daemon.summaries.request(unknown)
        assert daemon.summaries.answer("day", "2026-10-05", None) is None


# --------------------------------------------------------------------------------------------
# Privacy, listeners, stop
# --------------------------------------------------------------------------------------------


async def test_model_output_is_redacted_and_capped_before_it_is_stored() -> None:
    secret = "sk-" + "A" * 24
    runner = FakeRunner(default=f"ARC: uses {secret} now\nNOW: " + "word " * 400)
    async with story_world(runner) as daemon:
        await generate(daemon)
        text = json.dumps(stored(daemon))
        assert secret not in text
        cached = daemon.summaries.cached("session", SESSION_KEY)
        assert cached is not None
        assert cached.arc is not None
        assert cached.now is not None
        assert secret not in cached.arc
        assert len(cached.now) <= MAX_CHARS


async def test_listeners_hear_each_finished_job_and_the_view_is_touched() -> None:
    runner = FakeRunner([RunOutput(2, "")])
    async with story_world(runner) as daemon:
        heard: list[tuple[str, str, str]] = []
        remove = daemon.summaries.add_listener(lambda *event: heard.append(event))

        def broken(scope: str, ref: str, state: str) -> None:
            raise RuntimeError("listener")

        daemon.summaries.add_listener(broken)
        version = daemon.view.version
        await generate(daemon)
        assert heard == [("session", SESSION_KEY, "failed")]
        assert daemon.view.version > version
        remove()
        remove()  # removing twice is harmless
        await clock_of(daemon).advance(301)
        await generate(daemon)
        assert heard == [("session", SESSION_KEY, "failed")]


async def test_stopping_the_daemon_cancels_a_running_call() -> None:
    runner = FakeRunner()
    runner.hold()
    async with story_world(runner) as daemon:
        daemon.summaries.request(inputs_of(daemon))
        await wait_until(lambda: runner.in_flight == 1)
    assert runner.in_flight == 0
    assert not daemon.summaries.enabled
    assert stored(daemon) == []


# --------------------------------------------------------------------------------------------
# The pure parts
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (f"ARC: {ARC}\nNOW: {NOW}", (ARC, NOW)),
        (f"**ARC:** {ARC} **NOW:** {NOW}", (ARC, NOW)),  # one line, markdown
        (f"  ARC : {ARC}\r\n\r\n  NOW:   {NOW}  ", (ARC, NOW)),
        ("Just a sentence about it.", ("old arc", "Just a sentence about it.")),
        (f"NOW: {NOW}", ("old arc", NOW)),
        (f"ARC: {ARC}", (ARC, "old now")),
        ("   ", ("old arc", "old now")),
    ],
)
def test_parse_story_reads_the_reply_as_story_one_does(
    reply: str, expected: tuple[str, str]
) -> None:
    assert parse_story(reply, "old arc", "old now") == expected


def test_parse_story_and_text_cap_and_clean() -> None:
    arc, now = parse_story("ARC: " + "a" * 700 + "\nNOW: x")
    assert arc is not None
    assert len(arc) == MAX_CHARS
    assert now == "x"
    assert parse_story("", None, None) == (None, None)
    assert parse_text("**One  paragraph\n over two lines.**") == "One paragraph over two lines."
    assert parse_text(" * \n") is None


def test_normalize_and_digest_ignore_absent_values() -> None:
    assert normalize({"b": [None, "", " x "], "a": {}, "c": 0, "d": (1, None)}) == {
        "b": ["x"],
        "c": 0,
        "d": [1],
    }
    one = SummaryInputs.create("session", "k", {"title": "t", "branch": None, "files": []})
    two = SummaryInputs.create("session", "k", {"title": " t "})
    assert one.digest == two.digest == input_digest("session", {"title": "t"})
    assert one.digest != input_digest("workspace", {"title": "t"})
    assert one.key == ("session", "k")


def test_the_prompts_over_minimal_inputs() -> None:
    prompt = session_prompt({"agent": "codex", "stage": "build · test (2/3)"})
    assert "Agent: codex" in prompt
    assert "Pipeline stage: build · test (2/3)" in prompt
    assert "Everything they have asked" not in prompt
    assert workspace_prompt({"name": "7"}).count('"7"') == 1
    sessions = workspace_prompt({"name": "7", "sessions": [{"agent": "codex"}]})
    assert "  - codex: —" in sessions


def test_the_summary_environment_passes_only_the_allowlist_and_sets_both_guards() -> None:
    env = summary_environment(
        {
            "HOME": "/h",
            "PATH": "/bin",
            "LC_ALL": "C",
            "https_proxy": "http://p",
            "FYLGJA_STATE_DIR": "/s",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
            "CMUX_SOCKET_CAPABILITY": "secret",
        }
    )
    assert env == {
        "HOME": "/h",
        "PATH": "/bin",
        "LC_ALL": "C",
        "https_proxy": "http://p",
        "FYLGJA_STATE_DIR": "/s",
        **GUARD_ENV,
    }
    assert GUARD_ENV == {"FYLGJA_SUMMARY": "1", "LIDSKJALV_TRACK_SUMMARY": "1"}


# --------------------------------------------------------------------------------------------
# The real runner, against a stand-in claude
# --------------------------------------------------------------------------------------------


def _same_dir(one: str | Path, other: Path) -> bool:
    return Path(one).resolve() == other.resolve()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _stand_in(directory: Path, body: str) -> Path:
    script = directory / "claude-stand-in.py"
    script.write_text("import json, os, sys, time\n" + body, encoding="utf-8")
    return script


_RECORDER = """
prompt = sys.stdin.read()
print(json.dumps({
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "prompt": prompt,
    "guards": [os.environ.get("FYLGJA_SUMMARY"), os.environ.get("LIDSKJALV_TRACK_SUMMARY")],
    "marker": os.environ.get("CLAUDECODE"),
}))
"""


async def test_the_real_runner_passes_the_flags_the_prompt_the_cwd_and_the_guards(
    tmp_path: Path,
) -> None:
    script = _stand_in(tmp_path, _RECORDER)
    runner = ClaudeRunner([sys.executable, str(script)])
    assert runner.argv() == [sys.executable, str(script), *CLAUDE_ARGS]
    assert CLAUDE_ARGS == ("-p", "--model", "haiku", "--strict-mcp-config")
    cwd = tmp_path / "summarizer"
    cwd.mkdir()
    env = summary_environment({**os.environ, "CLAUDECODE": "1"})
    result = await runner.run("say hello", cwd=cwd, env=env, timeout=20)
    seen = json.loads(result.stdout)
    assert result.returncode == 0
    assert seen["argv"] == list(CLAUDE_ARGS)
    assert _same_dir(seen["cwd"], cwd)
    assert seen["prompt"] == "say hello"  # on stdin, never on the command line
    assert seen["guards"] == ["1", "1"]
    assert seen["marker"] is None


async def test_the_service_runs_the_real_runner_in_the_summarizer_cwd(tmp_path: Path) -> None:
    script = _stand_in(
        tmp_path, "sys.stdin.read()\nprint('ARC: ' + os.getcwd())\nprint('NOW: ok')\n"
    )
    from api_support import make_daemon, sessions_collector, windows_collector
    from summary_support import summaries_config

    daemon = make_daemon(
        windows_collector(),
        sessions_collector(),
        user_config=summaries_config(),
        summary_runner=ClaudeRunner([sys.executable, str(script)]),
    )
    await daemon.start()
    try:
        assert not paths.summarizer_dir().exists()
        inputs = SummaryInputs.create("session", SESSION_KEY, {"agent": "claude"})
        daemon.summaries.request(inputs)
        await daemon.summaries.drain()
        cached = daemon.summaries.cached("session", SESSION_KEY)
        assert cached is not None
        assert cached.now == "ok"
        assert cached.arc is not None
        assert _same_dir(cached.arc, paths.summarizer_dir())
        assert _mode(paths.summarizer_dir()) == 0o700
    finally:
        await daemon.stop()


async def test_the_real_runner_returns_a_non_zero_exit_and_kills_on_timeout(
    tmp_path: Path,
) -> None:
    failing = ClaudeRunner([sys.executable, str(_stand_in(tmp_path, "sys.exit(3)\n"))])
    result = await failing.run("x", cwd=tmp_path, env=dict(os.environ), timeout=20)
    assert result.returncode == 3
    sleeper = tmp_path / "sleeper"
    sleeper.mkdir()
    slow = ClaudeRunner([sys.executable, str(_stand_in(sleeper, "time.sleep(30)\n"))])
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await slow.run("x", cwd=tmp_path, env=dict(os.environ), timeout=0.5)
    assert asyncio.get_running_loop().time() - started < 10


async def test_a_missing_claude_is_not_installed(tmp_path: Path) -> None:
    runner = ClaudeRunner([str(tmp_path / "no-such-claude")])
    with pytest.raises(FileNotFoundError):
        await runner.run("x", cwd=tmp_path, env=dict(os.environ), timeout=5)


def test_claude_command_prefers_the_native_install_then_the_path(tmp_path: Path) -> None:
    native = tmp_path / ".local" / "bin" / "claude"
    assert claude_command(tmp_path, which=lambda name: f"/usr/local/bin/{name}") == (
        "/usr/local/bin/claude"
    )
    with pytest.raises(FileNotFoundError):
        claude_command(tmp_path, which=lambda name: None)
    native.parent.mkdir(parents=True)
    native.write_text("#!/bin/sh\n", encoding="utf-8")
    native.chmod(0o755)
    assert claude_command(tmp_path, which=lambda name: None) == str(native)
