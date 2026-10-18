"""The journal service in a running daemon (J5–J7, J13–J15, J22, J23; docs/decisions/0057).

A real daemon on a ``FakeClock`` with three synthetic collectors: ``wm`` (windows and focus),
``agents`` (sessions, activity and hooks) and ``proj`` (project state). Session ``claude:a`` works
in the checkout ``/w/repo`` and ``claude:b`` in ``/w/other``, both on ``main``; window 100 hosts the
first and window 200 the second. The model is a :class:`FakeRunner` whose default reply is a valid
entry, and ``git log`` is scripted on a ``FakeProcessRunner``. Nothing calls a real model or a
real ``git``. Session keys and kinds are test data only: the journal reads states and
``parent_key``.

Monotonic time starts at 0 once the picture is set up, so ``until(t)`` is "t seconds after set-up".
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from api_support import (
    T0,
    Synthetic,
    Win,
    activity_snapshot,
    focus_event,
    make_daemon,
    session,
    session_snapshot,
    sessions_collector,
    window_snapshot,
    windows_collector,
    workspace_snapshot,
)
from summary_support import FakeRunner, Stream, wait_until

from fylgja import paths
from fylgja.api.app import create_app
from fylgja.collectors.base import Capability
from fylgja.core.daemon import Daemon
from fylgja.core.journal import ThreadKey, thread_id
from fylgja.core.journal_service import JournalService
from fylgja.core.summaries import GUARD_ENV, RunOutput, SummaryInputs
from fylgja.models import (
    HookEvent,
    HookEventPayload,
    ProjectEvent,
    ProjectPayload,
    SessionActivity,
    SessionActivityEvent,
)
from fylgja.store import db, queries
from fylgja.store.rows import JournalEntryRow, JournalItemRow, JournalThreadRow
from fylgja.testing.fakes import FakeClock, FakeFS, FakeProcessRunner, settle

REPO = "/w/repo"
OTHER = "/w/other"
A = "claude:a"
B = "claude:b"
MAIN = ThreadKey(REPO, "main")
MAIN_ID = thread_id(MAIN)
OTHER_ID = thread_id(ThreadKey(OTHER, "main"))
FEATURE_ID = thread_id(ThreadKey(REPO, "feature"))
QUILL_ID = thread_id(ThreadKey(REPO, "quill-branch"))
THREAD_IDS = (MAIN_ID, OTHER_ID, FEATURE_ID, QUILL_ID)
GIT = "/usr/bin/git"

TITLE = "Fix the login form"
OLD_PROMPT = "make the email field validate"
NEW_PROMPT = "now cover the validator with a test"
HEADLINE = "You made the email field validate."
WHERE = "You are about to test the validator."
SECOND_WHERE = "You have a tested validator now."
SUBJECT = "Validate the email field"
COMMIT = "c0ffee" + "0" * 34
ITEM = "Write the validator test"
FACTS: dict[str, Any] = {
    "files": [f"{REPO}/src/form.py", f"{REPO}/src/validate.py"],
    "tasks": [
        {"status": "in_progress", "content": "validate the email"},
        {"status": "completed", "content": "read the form"},
    ],
}


def reply(**over: Any) -> str:
    """A valid entry (J11); keys set to ``None`` are left out."""
    body: dict[str, Any] = {
        "skip": False,
        "where": WHERE,
        "headline": HEADLINE,
        "did": ["You changed the validator."],
        "sources": ["S1", "S2"],
        "add": [{"kind": "next", "text": ITEM, "source": "S2"}],
    }
    body.update(over)
    return json.dumps({key: value for key, value in body.items() if value is not None})


def journal_on(**sections: Mapping[str, Any]) -> dict[str, Any]:
    return {"journal": {"enabled": True}, **sections}


# --------------------------------------------------------------------------------------------
# The world
# --------------------------------------------------------------------------------------------


@dataclass
class World:
    daemon: Daemon
    clock: FakeClock
    runner: FakeRunner
    processes: FakeProcessRunner
    changed: list[str] = field(default_factory=list)
    held: bool = False

    def hold(self) -> None:
        """Every model call waits until :meth:`release`; meanwhile :meth:`quiet` does not wait
        for the journal's attempts, which may be waiting for the runner or for a slot."""
        self.runner.hold()
        self.held = True

    def release(self) -> None:
        self.runner.release()
        self.held = False

    @property
    def journal(self) -> JournalService:
        assert self.daemon.journal is not None
        return self.daemon.journal

    async def quiet(self) -> None:
        """Let every write land and the journal evaluate, then (unless the runner is held) let
        every attempt finish: attempts read the store and write in threads."""
        await self.daemon.settle()
        await settle()  # the journal's own task evaluates and starts its attempts
        journal = self.daemon.journal
        if journal is not None and not self.held:
            await wait_until(lambda: not any(journal.pending(t) for t in THREAD_IDS))
        await self.daemon.settle()

    async def until(self, moment: float) -> None:
        """Advance monotonic time to ``moment`` and let the journal act."""
        await self.clock.advance_to(moment)
        await self.quiet()

    async def advance(self, seconds: float) -> None:
        await self.until(self.clock.monotonic() + seconds)

    async def until_call(self, moment: float, calls: int) -> None:
        """Advance to ``moment`` while the runner is held, until it has had ``calls`` calls."""
        await self.clock.advance_to(moment)
        await wait_until(lambda: len(self.runner.calls) == calls)

    async def hook(self, key: str, event: str) -> None:
        await self.daemon.sink("agents").emit_committed(
            HookEvent(
                source="agents",
                entity_id=key,
                observed_at=self.clock.now(),
                payload=HookEventPayload(session_id=key.partition(":")[2], event=event),
            )
        )
        await self.quiet()

    async def turn(self, key: str = A) -> None:
        """A new prompt is submitted, and one second later the turn ends."""
        await self.hook(key, "UserPromptSubmit")
        await self.advance(1)
        await self.hook(key, "Stop")

    async def prompt(self, key: str, text: str, *, state: str = "working") -> None:
        """The agent's registry reports a new last prompt."""
        self.daemon.sink("agents").emit(
            SessionActivityEvent(
                source="agents",
                entity_id=key,
                observed_at=self.clock.now(),
                payload=SessionActivity(state=state, title=TITLE, last_prompt=text),
            )
        )
        await self.quiet()

    async def project(self, path: str, branch: str | None) -> None:
        self.daemon.sink("proj").emit(
            ProjectEvent(
                source="proj",
                entity_id=path,
                observed_at=self.clock.now(),
                payload=ProjectPayload(path=path, repo_root=path, branch=branch),
            )
        )
        await self.quiet()

    async def focus(self, window_id: str, workspace: str) -> None:
        self.daemon.sink("wm").emit(focus_event("wm", window_id, workspace, self.clock.now()))
        await self.quiet()


def projects_collector() -> Synthetic:
    return Synthetic("proj", frozenset({Capability.PROJECT_STATE}))


def scripted_git() -> FakeProcessRunner:
    """``git log`` of /w/repo prints one commit 30 s after ``T0``; /w/other has none."""
    processes = FakeProcessRunner()
    committed = int((T0 + timedelta(seconds=30)).timestamp())
    processes.script(
        GIT,
        "--no-optional-locks",
        "-C",
        REPO,
        "log",
        stdout=f"{COMMIT}\x00{committed}\x00{SUBJECT}\n",
    )
    processes.script(GIT, "--no-optional-locks", "-C", OTHER, "log", stdout=b"")
    return processes


def checkouts() -> FakeFS:
    fs = FakeFS()
    fs.add_dir(f"{REPO}/.git")
    fs.add_dir(f"{OTHER}/.git")
    return fs


async def set_up(daemon: Daemon, branch: str = "main") -> None:
    """The picture of the module docstring, stamped now; /w/repo is on ``branch``."""
    now = daemon.clock.now()
    wm, agents, proj = daemon.sink("wm"), daemon.sink("agents"), daemon.sink("proj")
    wm.emit(workspace_snapshot("wm", ["1", "2"], now, focused="1"))
    wm.emit(
        window_snapshot(
            "wm", [Win("100", "1", "Term", 500, "zsh"), Win("200", "2", "Term", 600)], now
        )
    )
    for path in (REPO, OTHER):
        proj.emit(
            ProjectEvent(
                source="proj",
                entity_id=path,
                observed_at=now,
                payload=ProjectPayload(
                    path=path, repo_root=path, branch=branch if path == REPO else "main"
                ),
            )
        )
    agents.emit(
        session_snapshot(
            "agents",
            [
                session("claude", "a", pid=501, cwd=REPO, host_pid=500),
                session("claude", "b", pid=601, cwd=OTHER, host_pid=600),
            ],
            now,
        )
    )
    agents.emit(
        activity_snapshot(
            "agents",
            {
                A: SessionActivity(
                    state="working", title=TITLE, last_prompt=OLD_PROMPT, facts=FACTS
                ),
                B: SessionActivity(state="working", title="Tidy up", last_prompt="tidy the docs"),
            },
            now,
        )
    )
    await daemon.settle()


@asynccontextmanager
async def world(
    runner: FakeRunner | None = None,
    *,
    user_config: Mapping[str, Any] | None = None,
    clock: FakeClock | None = None,
    database: Path | None = None,
    fresh: bool = True,
    branch: str = "main",
) -> AsyncIterator[World]:
    """A started daemon with the journal on (unless ``user_config`` says otherwise). ``fresh``
    sets up the picture, with /w/repo on ``branch``; a restarted daemon finds it in the store
    instead."""
    clock = FakeClock(T0) if clock is None else clock
    runner = FakeRunner(default=reply()) if runner is None else runner
    processes = scripted_git()
    options: dict[str, Any] = {} if database is None else {"database": database}
    daemon = make_daemon(
        windows_collector(),
        sessions_collector(),
        projects_collector(),
        user_config=journal_on() if user_config is None else user_config,
        clock=clock,
        fs=checkouts(),
        summary_runner=runner,
        runner=processes,
        **options,
    )
    await daemon.start()
    found = World(daemon, clock, runner, processes)
    try:
        if daemon.journal is not None:
            daemon.journal.add_listener(found.changed.append)
        if fresh:
            await set_up(daemon, branch)
        yield found
    finally:
        await daemon.stop()


@dataclass(frozen=True)
class Stored:
    thread: JournalThreadRow | None
    entries: list[JournalEntryRow]
    items: list[JournalItemRow]


def stored(daemon: Daemon, tid: str = MAIN_ID) -> Stored:
    """The thread row, its entries (oldest first) and its items, as stored."""
    connection = db.connect_reader(daemon.writer.database)
    try:
        return Stored(
            queries.get_journal_thread(connection, tid),
            list(reversed(queries.list_journal_entries(connection, tid, limit=100))),
            queries.list_journal_items(connection, tid),
        )
    finally:
        connection.close()


def count_rows(daemon: Daemon, table: str) -> int:
    connection = sqlite3.connect(f"file:{daemon.writer.database}?mode=ro", uri=True)
    try:
        found: int = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return found
    finally:
        connection.close()


def task_names() -> set[str]:
    import asyncio

    return {task.get_name() for task in asyncio.all_tasks()}


# --------------------------------------------------------------------------------------------
# 1. A turn, 12. the guard environment
# --------------------------------------------------------------------------------------------


async def test_01_a_turn_gives_one_entry_after_the_quiet_time() -> None:
    async with world() as w:
        stream = await Stream(create_app(w.daemon)).open()
        try:
            await w.hook(A, "Stop")
            await w.until(119)
            assert w.runner.calls == []
            await w.until(120)
            assert len(w.runner.calls) == 1
            as_of = w.clock.now()
            found = stored(w.daemon)
            assert found.thread is not None
            assert [entry.trigger for entry in found.entries] == ["turn_done"]
            entry = found.entries[0]
            assert entry.model == "haiku"
            assert found.thread.watermark == as_of == entry.ended_at == entry.generated_at
            assert entry.started_at == as_of - timedelta(seconds=120) - timedelta(hours=1)
            assert (found.thread.where_text, found.thread.where_at) == (WHERE, as_of)
            assert found.thread.name == "repo"
            assert entry.headline == HEADLINE
            assert [source["id"] for source in entry.sources] == ["S1", "S2"]
            assert entry.sources[0]["kind"] == "session"
            assert [(item.kind, item.text, item.status) for item in found.items] == [
                ("next", ITEM, "open")
            ]
            assert w.changed == [MAIN_ID]
            while True:
                name, data = await stream.event()
                if name == "journal.changed":
                    break
            assert data == {"thread": MAIN_ID}
            prompt = w.runner.calls[0].prompt
            assert OLD_PROMPT in prompt
            assert SUBJECT in prompt
            assert "Open items: none." in prompt
            assert "  Files: form.py, validate.py" in prompt
            assert "  Open tasks: validate the email" in prompt
            assert "  Done tasks: read the form" in prompt
            brief = w.journal.brief(MAIN_ID)
            assert brief is not None
            assert brief.row == found.thread
            assert [item.text for item in brief.open_items] == [ITEM]
            assert not brief.pending
            assert w.journal.brief(OTHER_ID) is None
            assert w.journal.thread_for_path(REPO) == MAIN
            assert w.journal.thread_for_path("/w/nowhere") is None
            assert w.daemon.model_gate.calls == 1
        finally:
            await stream.close()


async def test_12_the_call_runs_with_the_guard_environment_in_the_summarizer_directory() -> None:
    async with world() as w:
        await w.hook(A, "Stop")
        await w.until(120)
        (call,) = w.runner.calls
    assert GUARD_ENV.items() <= call.env.items()
    assert call.cwd == paths.summarizer_dir()


# --------------------------------------------------------------------------------------------
# 2. min_interval, 3. session end, 4. focus_left
# --------------------------------------------------------------------------------------------


async def test_02_a_second_stop_waits_for_min_interval_and_one_entry_covers_it() -> None:
    async with world() as w:
        await w.hook(A, "Stop")
        await w.until(120)
        assert len(w.runner.calls) == 1
        await w.until(170)
        await w.hook(A, "UserPromptSubmit")
        await w.until(180)
        await w.hook(A, "Stop")
        await w.until(700)
        assert len(w.runner.calls) == 1
        await w.until(720)
        assert len(w.runner.calls) == 2
        await w.until(2000)
        assert len(w.runner.calls) == 2
        assert [entry.trigger for entry in stored(w.daemon).entries] == ["turn_done", "turn_done"]


async def test_03_a_session_end_right_after_a_stop_gives_one_session_end_entry() -> None:
    async with world() as w:
        await w.hook(A, "Stop")
        await w.until(5)
        await w.hook(A, "SessionEnd")
        assert len(w.runner.calls) == 1
        await w.until(1200)
        assert len(w.runner.calls) == 1
        assert [entry.trigger for entry in stored(w.daemon).entries] == ["session_end"]


async def test_04_leaving_after_a_long_visit_gives_focus_left() -> None:
    async with world() as w:
        await w.focus("100", "1")
        await w.until(10)
        await w.prompt(A, NEW_PROMPT)  # input, no Stop: only a hard boundary makes it due
        await w.until(300)
        assert w.runner.calls == []
        await w.focus("200", "2")
        assert len(w.runner.calls) == 1
        await w.until(1200)
        assert len(w.runner.calls) == 1
        assert [entry.trigger for entry in stored(w.daemon).entries] == ["focus_left"]
        assert "Your focus on this checkout in this window: 5 minutes." in w.runner.calls[0].prompt


async def test_04_leaving_after_a_short_visit_gives_nothing() -> None:
    async with world() as w:
        await w.focus("100", "1")
        await w.until(10)
        await w.prompt(A, NEW_PROMPT)
        await w.until(60)
        await w.focus("200", "2")
        await w.until(1200)
        assert w.runner.calls == []
        assert stored(w.daemon).thread is None


# --------------------------------------------------------------------------------------------
# 5. a failed call, 6. bad_output, 7. skip
# --------------------------------------------------------------------------------------------


async def test_05_a_timeout_writes_no_entry_keeps_the_watermark_and_retries_after_600_s() -> None:
    async with world() as w:
        await w.hook(A, "Stop")
        await w.until(120)
        before = stored(w.daemon).thread
        assert before is not None
        await w.until(729)
        w.hold()
        await w.turn()  # Stop at 730: due at 850
        await w.until_call(850, 2)
        assert w.journal.pending(MAIN_ID)
        await w.clock.advance(60)  # the gate's 60 s race on the daemon clock
        await w.quiet()
        failed_at = w.clock.now()
        found = stored(w.daemon)
        assert found.thread is not None
        assert len(found.entries) == 1
        assert found.thread.error_kind == "timeout"
        assert found.thread.error_at == failed_at
        assert found.thread.watermark == before.watermark
        assert found.thread.where_text == before.where_text
        assert found.thread.last_attempt_at == failed_at - timedelta(seconds=60)
        assert w.changed == [MAIN_ID, MAIN_ID]
        w.release()
        await w.until(910 + 599)
        assert len(w.runner.calls) == 2
        await w.until(910 + 600)
        assert len(w.runner.calls) == 3
        found = stored(w.daemon)
        assert found.thread is not None
        assert len(found.entries) == 2
        assert found.thread.error_kind is None
        assert found.thread.error_at is None
        assert found.entries[1].started_at == before.watermark


async def test_06_bad_output_is_not_retried_without_new_input() -> None:
    async with world(FakeRunner(default="Here is your entry: nothing much.")) as w:
        await w.hook(A, "Stop")
        await w.until(120)
        assert len(w.runner.calls) == 1
        found = stored(w.daemon)
        assert found.thread is not None
        assert found.thread.error_kind == "bad_output"
        assert found.thread.watermark is None
        assert found.entries == []
        assert found.items == []
        await w.until(3000)
        assert len(w.runner.calls) == 1
        await w.turn()  # new input: retried once min_interval has passed
        await w.advance(120)
        assert len(w.runner.calls) == 2


async def test_07_a_skip_moves_the_watermark_and_keeps_where() -> None:
    script = [
        RunOutput(0, reply()),
        RunOutput(0, json.dumps({"skip": True, "headline": "ignored", "add": "ignored"})),
        RunOutput(0, reply(where=SECOND_WHERE, sources=["S2", "S9", "S1", "S2"], add=[])),
    ]
    async with world(FakeRunner(script)) as w:
        await w.hook(A, "Stop")
        await w.until(120)
        first = stored(w.daemon)
        assert first.thread is not None
        await w.until(719)
        await w.turn()  # Stop at 720: due at 840
        await w.until(840)
        assert len(w.runner.calls) == 2
        skipped = stored(w.daemon)
        assert skipped.thread is not None
        assert len(skipped.entries) == 1
        assert skipped.thread.watermark == w.clock.now()
        assert skipped.thread.watermark != first.thread.watermark
        assert skipped.thread.where_text == WHERE
        assert skipped.thread.where_at == first.thread.where_at
        assert skipped.thread.last_at == first.thread.last_at
        assert w.changed == [MAIN_ID, MAIN_ID]
        await w.until(1439)
        await w.turn()  # Stop at 1440: due at 1560
        await w.until(1560)
        assert len(w.runner.calls) == 3
        last = stored(w.daemon)
        assert last.thread is not None
        assert last.thread.where_text == SECOND_WHERE
        assert len(last.entries) == 2
        assert [source["id"] for source in last.entries[1].sources] == ["S1", "S2"]
        assert last.entries[1].started_at == skipped.thread.watermark
        assert [item.text for item in last.items] == [ITEM]


# --------------------------------------------------------------------------------------------
# 8. off by default
# --------------------------------------------------------------------------------------------


async def test_08_the_default_config_builds_no_journal() -> None:
    async with world(user_config={}) as w:
        assert w.daemon.journal is None
        assert "fylgja-journal" not in task_names()
        assert w.daemon.spans.listeners == [w.daemon.span_window.apply]
        disabled = list(w.daemon.lifecycle)
        await w.focus("100", "1")
        await w.hook(A, "Stop")
        await w.until(5)
        await w.hook(A, "SessionEnd")
        await w.until(3000)  # a hundred ticks
        assert w.runner.calls == []
        assert w.daemon.model_gate.calls == 0
        assert count_rows(w.daemon, "journal_threads") == 0
    assert disabled == ["start:writer", "start:live_view", "start:state_engine", "start:supervisor"]
    async with world() as enabled:
        assert enabled.daemon.journal is not None
        assert "fylgja-journal" in task_names()
        assert enabled.daemon.lifecycle == disabled
    assert enabled.daemon.lifecycle == w.daemon.lifecycle
    assert "fylgja-journal" not in task_names()


# --------------------------------------------------------------------------------------------
# 9. shared slots, 15. input while waiting for a slot
# --------------------------------------------------------------------------------------------


def workspace_inputs(ref: str) -> SummaryInputs:
    return SummaryInputs.create("workspace", ref, {"name": ref, "label": f"work {ref}"})


async def test_09_summaries_and_the_journal_share_max_concurrent_slots() -> None:
    config = journal_on(summaries={"enabled": True, "max_concurrent": 2})
    async with world(user_config=config) as w:
        await w.hook(A, "Stop")
        await w.hook(B, "Stop")
        await w.until(119)
        w.hold()
        for ref in ("1", "2"):
            assert w.daemon.summaries.request(workspace_inputs(ref)).state.value == "pending"
        await wait_until(lambda: len(w.runner.calls) == 2)
        await w.clock.advance(1)  # both journal threads are due and wait for a slot
        await w.daemon.settle()
        assert w.journal.pending(MAIN_ID)
        assert w.journal.pending(OTHER_ID)
        assert len(w.runner.calls) == 2
        assert w.daemon.model_gate.running == 2
        w.release()
        await w.quiet()
        await w.daemon.summaries.drain()
        assert len(w.runner.calls) == 4
        assert w.daemon.model_gate.peak_running == 2
        assert w.runner.peak == 2
        assert w.daemon.summaries.cached("workspace", "1") is not None
        assert w.daemon.summaries.cached("workspace", "2") is not None
        assert len(stored(w.daemon, MAIN_ID).entries) == 1
        assert len(stored(w.daemon, OTHER_ID).entries) == 1


async def test_15_input_while_waiting_for_a_slot_belongs_to_that_attempt() -> None:
    config = journal_on(summaries={"enabled": True, "max_concurrent": 1})
    async with world(user_config=config) as w:
        await w.hook(A, "Stop")
        await w.until(119)
        w.hold()
        w.daemon.summaries.request(workspace_inputs("1"))
        await wait_until(lambda: len(w.runner.calls) == 1)
        await w.clock.advance(1)
        await w.daemon.settle()
        assert w.journal.pending(MAIN_ID)  # reserved, waiting for the only slot
        await w.prompt(A, NEW_PROMPT)
        assert w.journal.tracker.states[MAIN].dirty
        assert len(w.runner.calls) == 1
        w.release()
        await w.quiet()
        await w.daemon.summaries.drain()
        assert len(w.runner.calls) == 2
        assert NEW_PROMPT in w.runner.calls[1].prompt
        state = w.journal.tracker.states[MAIN]
        assert not state.dirty
        assert not state.pending
        await w.until(1200)
        assert len(w.runner.calls) == 2


# --------------------------------------------------------------------------------------------
# 10. restart
# --------------------------------------------------------------------------------------------


async def test_10_a_restart_after_a_stop_gives_one_bootstrap_entry(tmp_path: Path) -> None:
    clock = FakeClock(T0)
    database = tmp_path / "fylgja.sqlite"
    async with world(clock=clock, database=database) as first:
        await first.hook(A, "Stop")
        await first.until(120)
        assert len(first.runner.calls) == 1
        await first.until(799)
        await first.turn()  # Stop at 800
        await first.until(830)  # the daemon stops 30 s later, before the entry
        assert len(first.runner.calls) == 1
    restarted = clock.monotonic()
    async with world(clock=clock, database=database, fresh=False) as second:
        await second.until(restarted + 119)
        assert second.runner.calls == []
        await second.until(restarted + 120)
        assert len(second.runner.calls) == 1
        entries = stored(second.daemon).entries
        assert [entry.trigger for entry in entries] == ["turn_done", "turn_done"]
        assert entries[1].started_at == entries[0].ended_at
    restarted = clock.monotonic()
    async with world(clock=clock, database=database, fresh=False) as third:
        await third.until(restarted + 3000)
        assert third.runner.calls == []
        assert len(stored(third.daemon).entries) == 2


async def test_10_a_finished_run_still_listed_at_restart_gives_no_second_entry(
    tmp_path: Path,
) -> None:
    clock = FakeClock(T0)
    database = tmp_path / "fylgja.sqlite"
    run = "flow:r1"
    async with world(clock=clock, database=database) as first:
        agents = first.daemon.sink("agents")
        agents.emit(
            session_snapshot(
                "agents",
                [
                    session("claude", "a", pid=501, cwd=REPO, host_pid=500),
                    session("claude", "b", pid=601, cwd=OTHER, host_pid=600),
                    session("flow", "r1", cwd=REPO),
                ],
                clock.now(),
            )
        )
        await first.quiet()
        await first.prompt(run, "", state="running")
        await first.until(30)
        await first.prompt(run, "", state="passed")
        assert len(first.runner.calls) == 1
        assert [entry.trigger for entry in stored(first.daemon).entries] == ["run_finished"]
        assert "The run finished: passed." in first.runner.calls[0].prompt
    restarted = clock.monotonic()
    async with world(clock=clock, database=database, fresh=False) as second:
        assert second.daemon.view.sessions[run].state == "passed"
        await second.until(restarted + 3000)
        assert second.runner.calls == []
        assert len(stored(second.daemon).entries) == 1


# --------------------------------------------------------------------------------------------
# 11. stop during a call
# --------------------------------------------------------------------------------------------


async def test_11_stopping_the_daemon_during_a_call_writes_nothing() -> None:
    runner = FakeRunner(default=reply())
    async with world(runner) as w:
        w.hold()
        await w.hook(A, "Stop")
        await w.until_call(120, 1)
        assert runner.in_flight == 1
        assert w.journal.pending(MAIN_ID)
    assert runner.in_flight == 0
    assert count_rows(w.daemon, "journal_threads") == 0
    assert count_rows(w.daemon, "journal_entries") == 0
    assert w.changed == []


# --------------------------------------------------------------------------------------------
# 13. log hygiene
# --------------------------------------------------------------------------------------------


async def test_13_no_log_line_carries_text_names_branches_or_paths(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with world(branch="quill-branch") as w:  # a branch name no other log line could hold
        await w.hook(A, "Stop")
        await w.until(120)
        await w.until(729)
        w.hold()
        await w.turn()
        await w.until_call(850, 2)
        await w.clock.advance(60)
        await w.quiet()
        w.release()
        assert len(w.runner.calls) == 2
    journal = [record for record in caplog.records if record.name == "fylgja.journal"]
    outcomes = [record.getMessage() for record in journal if record.levelno == logging.INFO]
    quill = thread_id(ThreadKey(REPO, "quill-branch"))
    assert outcomes == [
        f"journal attempt for thread {quill} (turn_done): written, 1 added, 0 closed",
        f"journal attempt for thread {quill} (turn_done): timeout, 0 added, 0 closed",
    ]
    assert [r.getMessage() for r in journal if r.levelno == logging.WARNING] == [
        f"journal attempt for thread {quill} failed: timeout"
    ]
    prompt = w.runner.calls[0].prompt
    secrets = (
        HEADLINE,
        WHERE,
        ITEM,
        OLD_PROMPT,
        SUBJECT,
        prompt,
        prompt[:60],
        "quill-branch",
        REPO,
    )
    for record in caplog.records:
        message = record.getMessage()
        for secret in secrets:
            assert secret not in message, (record.name, message)


# --------------------------------------------------------------------------------------------
# 14. a late branch, 16. input during the call
# --------------------------------------------------------------------------------------------


async def test_14_a_branch_switch_while_main_is_dirty_writes_one_branch_changed_entry() -> None:
    async with world() as w:
        await w.hook(A, "Stop")
        await w.until(10)
        await w.project(REPO, "feature")
        assert len(w.runner.calls) == 1
        await w.until(1500)
        assert len(w.runner.calls) == 1
        found = stored(w.daemon)
        assert [entry.trigger for entry in found.entries] == ["branch_changed"]
        assert found.thread is not None
        assert found.thread.branch == "main"
        assert stored(w.daemon, FEATURE_ID).thread is None
        assert w.journal.thread_for_path(REPO) == ThreadKey(REPO, "feature")
        assert found.entries[0].sources[0]["kind"] == "thread"
        switched = found.entries[0].ended_at  # the switch was seen at the same instant
        await w.until(1519)
        await w.turn()  # the new thread starts clean; its first window starts at the switch
        await w.advance(120)
        feature = stored(w.daemon, FEATURE_ID)
        assert [entry.trigger for entry in feature.entries] == ["turn_done"]
        assert feature.entries[0].started_at == switched
        assert len(stored(w.daemon).entries) == 1


async def test_16_input_during_the_call_leaves_the_thread_dirty_for_the_next_window() -> None:
    async with world() as w:
        await w.hook(A, "Stop")
        w.hold()
        await w.until_call(120, 1)
        first_as_of = w.clock.now()
        await w.clock.advance(5)
        await w.prompt(A, NEW_PROMPT)
        w.release()
        await w.quiet()
        assert len(stored(w.daemon).entries) == 1
        state = w.journal.tracker.states[MAIN]
        assert state.dirty
        assert not state.pending
        await w.until(1000)
        assert len(w.runner.calls) == 1  # dirty, but no Stop
        await w.turn()  # Stop at 1001: due at 1121
        await w.until(1121)
        assert len(w.runner.calls) == 2
        entries = stored(w.daemon).entries
        assert entries[1].started_at == first_as_of
        second = w.runner.calls[1].prompt
        assert NEW_PROMPT in second
        assert OLD_PROMPT not in second
        assert SUBJECT not in second  # the commit lies in the first window only


# --------------------------------------------------------------------------------------------
# The daemon's focused project, and failures inside the service
# --------------------------------------------------------------------------------------------


async def test_the_focused_project_is_that_of_the_focused_window() -> None:
    async with world() as w:
        assert w.daemon.focused_project() is None
        await w.focus("100", "1")
        assert w.daemon.focused_project() == REPO
        await w.focus("200", "2")
        assert w.daemon.focused_project() == OTHER


async def test_a_failing_listener_is_logged_and_the_others_are_still_told(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with world() as w:

        def broken(thread: str) -> None:
            raise RuntimeError("listener")

        w.journal.add_listener(broken)
        told: list[str] = []
        remove = w.journal.add_listener(told.append)
        await w.hook(A, "Stop")
        await w.until(120)
        assert told == [MAIN_ID]
        remove()
        remove()
        assert "a journal listener failed" in caplog.text


async def test_an_unexpected_error_in_an_attempt_keeps_the_input_and_is_logged_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world() as w:

        def broken(*args: Any, **kwargs: Any) -> Any:
            raise LookupError("broken input")

        # A scoped patch: ``monkeypatch.undo()`` would also undo the scratch HOME of the test.
        with monkeypatch.context() as patch:
            patch.setattr("fylgja.core.journal_service.JournalInputs.create", broken)
            await w.hook(A, "Stop")
            await w.until(120)
            state = w.journal.tracker.states[MAIN]
            assert state.dirty
            assert state.failed_kind == "runner_error"
            assert w.runner.calls == []
        await w.until(720)
        assert len(w.runner.calls) == 1
        assert len(stored(w.daemon).entries) == 1
    errors = [r.getMessage() for r in caplog.records if r.name == "fylgja.journal"]
    assert [m for m in errors if "LookupError" in m] == [
        "a journal attempt failed with LookupError; later failures of this type are not logged"
    ]


async def test_a_window_with_nothing_new_makes_no_call_and_writes_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """J7: a newer commit time dirties the thread, but ``git log`` has no commit in the window
    and no session opened an interval in it."""
    caplog.set_level(logging.INFO, logger="fylgja.journal")
    async with world() as w:
        await w.focus("100", "1")
        await w.until(10)
        await w.prompt(A, NEW_PROMPT)
        await w.until(300)
        await w.focus("200", "2")
        assert len(w.runner.calls) == 1
        before = stored(w.daemon)
        await w.until(310)
        w.daemon.sink("proj").emit(
            ProjectEvent(
                source="proj",
                entity_id=REPO,
                observed_at=w.clock.now(),
                payload=ProjectPayload(path=REPO, last_commit_at=w.clock.now()),
            )
        )
        await w.quiet()
        assert w.journal.tracker.states[MAIN].dirty
        await w.until(320)
        await w.focus("100", "1")
        await w.until(630)
        await w.focus("200", "2")  # focus_left after a 310 s visit
        assert len(w.runner.calls) == 1
        assert stored(w.daemon) == before
        assert w.changed == [MAIN_ID]
        state = w.journal.tracker.states[MAIN]
        assert not state.dirty
        assert not state.running
    outcomes = [r.getMessage() for r in caplog.records if r.name == "fylgja.journal"]
    assert outcomes[-1] == (
        f"journal attempt for thread {MAIN_ID} (focus_left): nothing_new, 0 added, 0 closed"
    )


async def test_a_failing_evaluation_is_logged_once_and_the_next_wake_evaluates_again(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world() as w:

        def broken(*args: Any, **kwargs: Any) -> Any:
            raise ArithmeticError("broken due")

        with monkeypatch.context() as patch:  # scoped: keeps the scratch HOME of the test
            patch.setattr("fylgja.core.journal_service.due", broken)
            await w.hook(A, "Stop")
            await w.until(200)
            assert w.runner.calls == []
        await w.until(230)  # the next tick wakes the loop
        assert len(w.runner.calls) == 1
    errors = [r.getMessage() for r in caplog.records if "ArithmeticError" in r.getMessage()]
    assert errors == [
        "evaluating the journal failed with ArithmeticError; later failures of this type are "
        "not logged"
    ]


async def test_a_project_todo_is_input_and_a_source_of_its_window() -> None:
    async with world() as w:
        added = await w.daemon.todos.add("Ship the form", scope_kind="project", scope_ref=REPO)
        await w.quiet()
        assert w.journal.tracker.states[MAIN].dirty
        await w.hook(A, "Stop")
        await w.advance(120)
        assert "todo opened: Ship the form" in w.runner.calls[0].prompt
        await w.until(700)
        await w.daemon.todos.done(added.id)
        await w.quiet()
        await w.turn()
        await w.advance(120)
        assert len(w.runner.calls) == 2
        assert "todo done: Ship the form" in w.runner.calls[1].prompt
