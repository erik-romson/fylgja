"""Waiting-on-me through the state engine and ``/now`` (WP6.1).

Synthetic collectors (``wm`` for windows, ``agents`` for sessions) feed records through the ingest
gate, so the hook events reach the real session state machine, the writer and the live view. The
``waiting[]`` field of ``fylgja.now/1`` already exists; these tests only fill it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from api_support import (
    T0,
    Synthetic,
    Win,
    activity_snapshot,
    client,
    focus_event,
    make_daemon,
    session,
    session_snapshot,
    window_snapshot,
)
from appcontext_support import SLACK

from fylgja.api.sse import EventHub
from fylgja.collectors.appcontext import parse
from fylgja.collectors.appcontext.titles import parse_title
from fylgja.collectors.base import Capability, CollectorContext
from fylgja.core.daemon import Daemon
from fylgja.models import (
    BuildEvent,
    BuildPayload,
    BuildWorkflow,
    HookEvent,
    HookEventPayload,
    ProjectEvent,
    ProjectPayload,
    SessionActivity,
    SessionActivityEvent,
)
from fylgja.testing.fakes import FakeClock

EDITOR = Win("7", "3", "Editor", 70, "notes.md")
OTHER = Win("8", "3", "Mail", 80, "Inbox")
CHAT = Win("9", "4", "Chat", 90, "general (Channel) - Team - 2 new items - Slack", bundle=SLACK)
KEY = "claude:a"


def _windows(context: CollectorContext) -> None:
    now = context.clock.now()
    context.sink.emit(window_snapshot("wm", [EDITOR, OTHER, CHAT], now))
    context.sink.emit(focus_event("wm", "8", "3", now))


def _sessions(context: CollectorContext) -> None:
    now = context.clock.now()
    context.sink.emit(
        session_snapshot(
            "agents", [session("claude", "a", pid=71, cwd="/work/notes", host_pid=70)], now
        )
    )
    context.sink.emit(activity_snapshot("agents", {KEY: SessionActivity(state="idle")}, now))


def _hook(event: str, at_seconds: float, **fields: Any) -> HookEvent:
    return HookEvent(
        source="agents",
        entity_id=KEY,
        observed_at=T0 + timedelta(seconds=at_seconds),
        payload=HookEventPayload(session_id="a", event=event, **fields),
    )


def _daemon(clock: FakeClock | None = None, *extra: Synthetic) -> tuple[Daemon, Synthetic]:
    wm = Synthetic("wm", frozenset({Capability.WINDOWS}), emit=_windows)
    agents = Synthetic("agents", frozenset({Capability.SESSIONS}), emit=_sessions)
    return make_daemon(wm, agents, *extra, clock=clock), agents


async def _waiting(daemon: Daemon) -> list[dict[str, Any]]:
    async with client(daemon) as http:
        response = await http.get("/now")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["waiting"], list)
    return list(body["waiting"])


async def test_nothing_waits_on_an_idle_picture() -> None:
    daemon, _ = _daemon()
    await daemon.start()
    try:
        await daemon.settle()
        assert await _waiting(daemon) == []
    finally:
        await daemon.stop()


async def test_a_permission_prompt_appears_after_its_event_and_leaves_with_the_answer() -> None:
    daemon, agents = _daemon()
    await daemon.start()
    try:
        await daemon.settle()
        sink = agents.contexts[0].sink
        sink.emit(_hook("PreToolUse", 1, tool_name="Bash"))
        await daemon.settle()
        assert await _waiting(daemon) == []  # running a tool is not waiting
        sink.emit(_hook("PermissionRequest", 2, tool_name="Bash"))
        await daemon.settle()
        (entry,) = await _waiting(daemon)
        assert (entry["kind"], entry["target"], entry["target_kind"]) == (
            "permission",
            KEY,
            "session",
        )
        assert entry["reason"].startswith("waiting for permission")
        assert entry["since"]
        sink.emit(_hook("PostToolUse", 3, tool_name="Bash"))
        await daemon.settle()
        assert await _waiting(daemon) == []
    finally:
        await daemon.stop()


async def test_a_turn_done_waits_until_its_window_gains_focus_and_then_leaves() -> None:
    clock = FakeClock(T0)
    daemon, agents = _daemon(clock)
    await daemon.start()
    try:
        await daemon.settle()
        sink = agents.contexts[0].sink
        wm = daemon.sink("wm")
        sink.emit(_hook("UserPromptSubmit", 1))
        sink.emit(_hook("Stop", 2))
        await daemon.settle()
        (entry,) = await _waiting(daemon)
        assert (entry["kind"], entry["target"]) == ("turn_done", KEY)
        assert entry["reason"] == "finished a turn nobody has looked at"

        wm.emit(focus_event("wm", "8", "3", T0 + timedelta(seconds=3)))  # another window
        await daemon.settle()
        assert [e["kind"] for e in await _waiting(daemon)] == ["turn_done"]

        wm.emit(focus_event("wm", "7", "3", T0 + timedelta(seconds=4)))  # the session's window
        await daemon.settle()
        assert await _waiting(daemon) == []
        session_state = next(s for s in daemon.state().live.sessions.values()).state
        assert session_state == "seen"
    finally:
        await daemon.stop()


async def test_a_norn_run_waiting_for_input_appears_with_its_reason() -> None:
    daemon, agents = _daemon()
    await daemon.start()
    try:
        await daemon.settle()
        sink = agents.contexts[0].sink
        sink.emit(session_snapshot("agents", [session("norn", "wf:1", cwd="/work/flow")], T0))
        sink.emit(
            SessionActivityEvent(
                source="agents",
                entity_id="norn:wf:1",
                observed_at=T0 + timedelta(seconds=5),
                payload=SessionActivity(state="waiting_input", waiting_reason="the agent asked"),
            )
        )
        await daemon.settle()
        (entry,) = await _waiting(daemon)
        assert (entry["kind"], entry["target"]) == ("input", "norn:wf:1")
        assert entry["reason"] == "waiting for input: the agent asked"
    finally:
        await daemon.stop()


async def test_an_unread_marker_appears_in_its_window_s_entry_and_announces_itself() -> None:
    def emit(context: CollectorContext) -> None:
        title = parse_title(CHAT.bundle, CHAT.title)
        assert title is not None
        facts = parse.WindowFacts(CHAT.window_id, CHAT.bundle, CHAT.title, CHAT.workspace)
        record = parse.to_record(parse.title_item(title), facts, T0)
        context.sink.emit(record.model_copy(update={"source": "ctx"}))

    ctx = Synthetic("ctx", frozenset({Capability.CONTEXT}), emit=emit)
    daemon, _ = _daemon(None, ctx)
    await daemon.start()
    try:
        hub = EventHub(daemon.view)
        subscription = hub.subscribe()
        assert subscription is not None
        await daemon.settle()
        (entry,) = await _waiting(daemon)
        assert (entry["kind"], entry["target"], entry["target_kind"]) == ("unread", "9", "window")
        assert entry["reason"] == "2 unread in general"
        frames = []
        while not subscription.queue.empty():
            frames.append(subscription.queue.get_nowait())
        assert any(frame is not None and b"event: now.changed" in frame for frame in frames)
    finally:
        await daemon.stop()


async def test_every_change_of_the_waiting_picture_publishes_now_changed() -> None:
    daemon, agents = _daemon()
    await daemon.start()
    try:
        await daemon.settle()
        hub = EventHub(daemon.view)
        subscription = hub.subscribe()
        assert subscription is not None
        before = daemon.view.version
        agents.contexts[0].sink.emit(_hook("PermissionRequest", 2, tool_name="Edit"))
        await daemon.settle()
        assert daemon.view.version > before
        frames = []
        while not subscription.queue.empty():
            frames.append(subscription.queue.get_nowait())
        assert any(frame is not None and b"event: now.changed" in frame for frame in frames)
    finally:
        await daemon.stop()


async def test_waiting_is_absent_while_no_collector_can_fill_it() -> None:
    daemon = make_daemon(clock=FakeClock(T0))
    await daemon.start()
    try:
        async with client(daemon) as http:
            body = (await http.get("/now")).json()
    finally:
        await daemon.stop()
    assert "waiting" not in body


def _projects(context: CollectorContext) -> None:
    now = context.clock.now()
    for path, pr in (("/work/notes", 7), ("/work/old", None)):
        context.sink.emit(
            ProjectEvent(
                source="proj",
                entity_id=path,
                observed_at=now,
                payload=ProjectPayload(
                    path=path,
                    branch="main",
                    pr_number=pr,
                    pr_state="OPEN" if pr else None,
                    review_decision="REVIEW_REQUIRED" if pr else None,
                ),
            )
        )
        context.sink.emit(
            BuildEvent(
                source="proj",
                entity_id=path,
                observed_at=now,
                payload=BuildPayload(
                    state="fail",
                    project_path=path,
                    head_sha="abc",
                    workflows=(BuildWorkflow(workflow="ci", state="fail", run_started_at=now),),
                ),
            )
        )


async def test_a_failed_build_waits_only_on_a_project_touched_today() -> None:
    proj = Synthetic("proj", frozenset({Capability.PROJECT_STATE}), emit=_projects)
    daemon, _ = _daemon(None, proj)
    await daemon.start()
    try:
        await daemon.settle()
        entries = await _waiting(daemon)
    finally:
        await daemon.stop()
    # /work/notes has a session with activity today; /work/old has neither session nor focus.
    assert [(e["kind"], e["target"], e["target_kind"]) for e in entries] == [
        ("build_failed", "/work/notes", "project"),
    ]
    assert entries[0]["reason"].startswith("build failed on ")
