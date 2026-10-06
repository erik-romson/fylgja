"""Every read endpoint through HTTPX's ASGI transport (WP1.9).

A world of two synthetic collectors (``wm`` reports windows, ``agents`` reports sessions) is pushed
through the daemon's ingest gate; the tests read it back through the API. They also cover the
``/now`` serialization rule (no ``null``; later fields absent) and what an unavailable collector
does to ``/now``: its data is absent and the response is still 200.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from api_support import (
    BASE_URL,
    T0,
    Synthetic,
    Win,
    activity_snapshot,
    by_key,
    client,
    focus_event,
    make_daemon,
    nulls,
    session,
    session_snapshot,
    sessions_collector,
    window_snapshot,
    windows_collector,
    workspace_snapshot,
)

from fylgja.api.now_models import NOW_SCHEMA, Now
from fylgja.core.daemon import Daemon
from fylgja.models import HealthState, HookEvent, HookEventPayload, SessionActivity
from fylgja.testing.fakes import FakeClock, FakeFS

CMUX = "com.cmuxterm.app"
TERMINAL = "com.apple.Terminal"

FACTS: dict[str, Any] = {
    "title": "Fix the build",
    "prompt": "make the build green",
    "said": "Running the tests now.",
    "mode": "default",
    "branch": "main",
    "version": "2.1.289",
    "model": "claude-opus-5",
    "turns": 3,
    "started": 1790000000,
    "last_at": 1790000300,
    "prompts": ["make the build green"],
    "tools": {"Bash": 2},
    "files": [],
    "tasks": [{"status": "in_progress", "content": "fix the build"}],
}

LATER_TOP = ("usage", "presence", "next", "next_list_id")
LATER_SESSION = ("tokens", "cost_usd", "context_pct", "pipeline", "todos", "story", "children")
LATER_WORKSPACE = ("todos", "story")
LATER_PROJECT = ("pr", "build", "todos", "dirty_count")


def _world(daemon: Daemon) -> None:
    """Workspaces 1, 2, 3 and 10; three windows; three sessions of two kinds."""
    wm = daemon.sink("wm")
    agents = daemon.sink("agents")
    wm.emit(workspace_snapshot("wm", ["1", "2", "3", "10"], T0, focused="2"))
    wm.emit(
        window_snapshot(
            "wm",
            [
                Win("100", "2", "cmux", 500, "Shell", CMUX),
                Win("101", "2", "Safari", 700, "Docs sk-AAAAAAAAAAAAAAAAAAAAAAAA page"),
                Win("102", "10", "Terminal", 600, "Shell", TERMINAL),
            ],
            T0,
        )
    )
    wm.emit(focus_event("wm", "100", "2", T0))
    agents.emit(
        session_snapshot(
            "agents",
            [
                session(
                    "claude",
                    "s-1",
                    pid=501,
                    cwd="/work/alpha/src",
                    host_pid=500,
                    process_key="501-1790000000",
                    entrypoint="cli",
                    version="2.1.289",
                    model="claude-opus-5",
                    branch="main",
                ),
                session("claude", "s-2", pid=601, cwd="/work/beta", host_pid=600),
                session("codex", "t-3", cwd="/work/alpha"),
            ],
            T0,
        )
    )
    agents.emit(
        activity_snapshot(
            "agents",
            {
                "claude:s-1": SessionActivity(
                    state="waiting",
                    current_tool="Bash",
                    current_target="npm",
                    title="Fix the build",
                    facts=FACTS,
                ),
                "claude:s-2": SessionActivity(
                    state="working",
                    current_tool="Edit",
                    current_target="/work/beta/x.py",
                    title="Refactor",
                ),
            },
            T0,
        )
    )


@pytest.fixture
async def daemon() -> AsyncIterator[Daemon]:
    fs = FakeFS()
    fs.add_dir("/work/alpha/.git")
    instance = make_daemon(windows_collector(), sessions_collector(), fs=fs)
    await instance.start()
    _world(instance)
    await instance.settle()
    try:
        yield instance
    finally:
        await instance.stop()


async def _get(daemon: Daemon, path: str) -> Any:
    async with client(daemon) as http:
        response = await http.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    return response.json()


# --- /now --------------------------------------------------------------------------------


async def test_now_has_the_whole_picture(daemon: Daemon) -> None:
    now = await _get(daemon, "/now")
    assert now["schema"] == NOW_SCHEMA
    assert now["generated_at"].startswith("2026-10-05T12:00")
    assert [w["name"] for w in now["workspaces"]] == ["1", "2", "3", "10"]
    assert [s["key"] for s in now["sessions"]] == ["claude:s-1", "claude:s-2", "codex:t-3"]
    assert [p["path"] for p in now["projects"]] == ["/work/alpha", "/work/beta"]
    assert {c["name"]: c["state"] for c in now["collectors"]} == {
        "agents": "starting",
        "wm": "starting",
    }
    assert now["degraded"] == []
    Now.model_validate(now)  # the payload is a valid fylgja.now/1 document


async def test_now_never_carries_null_and_later_fields_are_absent(daemon: Daemon) -> None:
    now = await _get(daemon, "/now")
    assert list(nulls(now)) == []
    for name in LATER_TOP:
        assert name not in now
    assert now["waiting"] == []  # filled by waiting-on-me (WP6.1): present, and nothing waits
    assert "context" not in now["focused"]
    for item in now["sessions"]:
        assert not set(LATER_SESSION) & set(item), item["key"]
    for workspace in now["workspaces"]:
        assert not set(LATER_WORKSPACE) & set(workspace)
        assert all("context" not in w for w in workspace["windows"])
    for project in now["projects"]:
        assert not set(LATER_PROJECT) & set(project)


async def test_focused_window_and_its_session(daemon: Daemon) -> None:
    focused = (await _get(daemon, "/now"))["focused"]
    assert focused == {
        "window_id": "100",
        "workspace": "2",
        "app_name": "cmux",
        "bundle_id": CMUX,
        "title": "Shell",
        "session_key": "claude:s-1",
        "since": "2026-10-05T12:00:00Z",
    }


async def test_workspaces_carry_labels_windows_and_one_liners(daemon: Daemon) -> None:
    workspaces = by_key((await _get(daemon, "/now"))["workspaces"], "name")
    two = workspaces["2"]
    assert two["focused"] is True
    assert (two["label"], two["label_source"], two["inferred_label"]) == (
        "alpha",
        "inferred",
        "alpha",
    )
    assert two["one_liner"] == "alpha — Waiting for permission: Bash npm"
    assert [w["id"] for w in two["windows"]] == ["100", "101"]
    assert two["windows"][0] == {
        "id": "100",
        "app": "cmux",
        "bundle_id": CMUX,
        "title": "Shell",
        "focused": True,
    }
    assert two["windows"][1]["title"] == "Docs [redacted] page"  # redacted before the store
    assert two["projects"] == ["/work/alpha"]
    assert two["sessions"] == ["claude:s-1"]

    ten = workspaces["10"]
    assert (ten["label"], ten["focused"]) == ("beta", False)
    assert ten["one_liner"] == "beta — Edit /work/beta/x.py"

    empty = workspaces["1"]
    assert (empty["label"], empty["label_source"], empty["one_liner"]) == ("—", "none", "—")
    assert "inferred_label" not in empty  # nothing was inferred
    assert empty["windows"] == []
    assert empty["sessions"] == []
    assert empty["projects"] == []


async def test_sessions_carry_state_host_continuity_facts_and_one_liner(daemon: Daemon) -> None:
    sessions = by_key((await _get(daemon, "/now"))["sessions"], "key")
    first = sessions["claude:s-1"]
    assert first["kind"] == "claude"
    assert first["native_id"] == "s-1"
    assert first["state"] == "waiting"
    assert first["state_since"] == "2026-10-05T12:00:00Z"
    assert (first["current_tool"], first["current_target"]) == ("Bash", "npm")
    assert first["one_liner"] == "Waiting for permission: Bash npm"
    assert first["one_liner_from"] == "waiting"
    assert first["project_path"] == "/work/alpha"
    assert (first["entrypoint"], first["version"], first["model"]) == (
        "cli",
        "2.1.289",
        "claude-opus-5",
    )
    assert first["lane_key"] == first["process_key"] == "501-1790000000"
    assert first["facts"]["title"] == "Fix the build"
    assert first["facts"]["tasks"] == [{"status": "in_progress", "content": "fix the build"}]
    assert first["host"] == {
        "kind": "cmux",
        "window_id": "100",
        "workspace": "2",
        "via": "parent-chain",
        "confidence": "window-only",
    }

    second = sessions["claude:s-2"]
    assert (second["state"], second["one_liner_from"]) == ("working", "tool")
    assert second["host"]["kind"] == "other"
    assert second["host"]["window_id"] == "102"
    assert "facts" not in second

    codex = sessions["codex:t-3"]
    assert codex["host"] == {
        "via": "none",
        "confidence": "none",
        "unresolved": "session has no pid",
    }
    assert "one_liner" not in codex
    assert "state" not in codex


def _hook(event: str, seconds: int, **fields: Any) -> HookEvent:
    return HookEvent(
        source="agents",
        entity_id="claude:s-2",
        observed_at=T0 + timedelta(seconds=seconds),
        payload=HookEventPayload(session_id="s-2", event=event, **fields),
    )


async def test_hook_states_flow_into_now_and_a_finished_turn_becomes_seen(daemon: Daemon) -> None:
    """WP2.3 end to end: hook events change the session's state in ``/now`` without changing the
    schema, the registry's next report cannot undo them and the focus stream marks the turn seen."""
    agents, wm = daemon.sink("agents"), daemon.sink("wm")

    async def second() -> Any:
        sessions = by_key((await _get(daemon, "/now"))["sessions"], "key")
        return sessions["claude:s-2"]

    agents.emit(_hook("PermissionRequest", 1, tool_name="Edit", edited_paths=("/work/beta/x.py",)))
    await daemon.settle()
    asked = await second()
    assert asked["state"] == "waiting_permission"
    assert asked["state_since"] == "2026-10-05T12:00:01Z"
    assert asked["waiting_reason"] == "approve Edit"
    assert (asked["one_liner"], asked["one_liner_from"]) == (
        "Waiting for permission: Edit x.py",
        "waiting",
    )

    # the registry polls again and still says "working": the hook state stands
    agents.emit(
        activity_snapshot(
            "agents",
            {"claude:s-2": SessionActivity(state="working", title="Refactor")},
            T0 + timedelta(seconds=2),
        )
    )
    await daemon.settle()
    assert (await second())["state"] == "waiting_permission"

    agents.emit(_hook("Stop", 3))
    await daemon.settle()
    done = await second()
    assert (done["state"], done["state_since"]) == ("turn_done", "2026-10-05T12:00:03Z")

    wm.emit(focus_event("wm", "100", "2", T0 + timedelta(seconds=4)))  # another window
    await daemon.settle()
    assert (await second())["state"] == "turn_done"
    wm.emit(focus_event("wm", "102", "10", T0 + timedelta(seconds=5)))  # the session's window
    await daemon.settle()
    seen = await second()
    assert (seen["state"], seen["state_since"]) == ("seen", "2026-10-05T12:00:05Z")
    assert seen["title"] == "Refactor"
    Now.model_validate(await _get(daemon, "/now"))
    assert (await _get(daemon, "/now"))["schema"] == NOW_SCHEMA


async def test_a_session_with_hooks_only_appears_in_now(daemon: Daemon) -> None:
    daemon.sink("agents").emit(
        HookEvent(
            source="agents",
            entity_id="claude:h-9",
            observed_at=T0 + timedelta(seconds=1),
            payload=HookEventPayload(
                session_id="h-9", event="UserPromptSubmit", pid=777, cwd="/work/alpha"
            ),
        )
    )
    await daemon.settle()
    sessions = by_key((await _get(daemon, "/now"))["sessions"], "key")
    hooked = sessions["claude:h-9"]
    assert (hooked["state"], hooked["pid"], hooked["project_path"]) == (
        "working",
        777,
        "/work/alpha",
    )


async def test_projects_group_sessions_and_know_their_workspace(daemon: Daemon) -> None:
    projects = by_key((await _get(daemon, "/now"))["projects"], "path")
    alpha = projects["/work/alpha"]
    assert alpha["name"] == "alpha"
    assert alpha["repo_root"] == "/work/alpha"
    assert alpha["workspace"] == "2"
    assert alpha["sessions"] == ["claude:s-1", "codex:t-3"]
    beta = projects["/work/beta"]
    assert (beta["name"], beta["workspace"], beta["sessions"]) == ("beta", "10", ["claude:s-2"])
    assert "repo_root" not in beta


# --- the other read endpoints --------------------------------------------------------------


async def test_workspaces_endpoints(daemon: Daemon) -> None:
    now = await _get(daemon, "/now")
    assert await _get(daemon, "/workspaces") == now["workspaces"]
    assert await _get(daemon, "/workspaces/10") == by_key(now["workspaces"], "name")["10"]
    async with client(daemon) as http:
        missing = await http.get("/workspaces/99")
    assert missing.status_code == 404


async def test_sessions_endpoints(daemon: Daemon) -> None:
    now = await _get(daemon, "/now")
    assert await _get(daemon, "/sessions") == now["sessions"]
    assert (
        await _get(daemon, "/sessions/claude:s-2") == by_key(now["sessions"], "key")["claude:s-2"]
    )
    async with client(daemon) as http:
        missing = await http.get("/sessions/claude:nope")
    assert missing.status_code == 404


async def test_projects_endpoint(daemon: Daemon) -> None:
    assert await _get(daemon, "/projects") == (await _get(daemon, "/now"))["projects"]


async def test_collectors_endpoint_lists_registry_rows_with_live_health(daemon: Daemon) -> None:
    rows = by_key(await _get(daemon, "/collectors"), "name")
    assert set(rows) == {"agents", "wm"}
    wm = rows["wm"]
    assert (wm["origin"], wm["state"], wm["enabled"]) == ("built-in", "enabled", True)
    assert wm["capabilities"] == ["windows"]
    assert wm["health"]["state"] == "starting"


async def test_health_endpoint(daemon: Daemon) -> None:
    health = await _get(daemon, "/health")
    assert health["status"] == "ok"
    assert health["now_schema"] == NOW_SCHEMA
    assert health["started_at"] == "2026-10-05T12:00:00Z"
    assert health["live_version"] >= 1
    assert health["degraded"] == []
    assert health["version"]


async def test_health_before_start_and_after_stop() -> None:
    daemon = make_daemon(windows_collector())
    assert (await _get(daemon, "/health"))["status"] == "starting"
    await daemon.start()
    await daemon.stop()
    assert (await _get(daemon, "/health"))["status"] == "stopping"


# --- unavailable collectors ----------------------------------------------------------------


async def _degrade(daemon: Daemon, spec: Synthetic, clock: FakeClock) -> None:
    spec.doctor_state = HealthState.DEGRADED
    await clock.advance(1.0)  # the supervisor's first doctor probe
    await daemon.settle()


async def test_a_degraded_collector_s_section_is_absent_and_now_is_200() -> None:
    clock = FakeClock(T0)
    agents = sessions_collector()
    daemon = make_daemon(windows_collector(), agents, clock=clock)
    await daemon.start()
    try:
        _world(daemon)
        await daemon.settle()
        assert "sessions" in await _get(daemon, "/now")
        await _degrade(daemon, agents, clock)
        now = await _get(daemon, "/now")
        assert "sessions" not in now
        assert "projects" not in now
        assert now["degraded"] == ["agents"]
        info = by_key(now["collectors"], "name")["agents"]
        assert (info["state"], info["last_error_kind"]) == ("degraded", "synthetic")
        assert by_key(now["workspaces"], "name")["2"]["sessions"] == []
        assert by_key(now["collectors"], "name")["wm"]["state"] == "ok"
        assert await _get(daemon, "/sessions") == []
        assert await _get(daemon, "/projects") == []
        async with client(daemon) as http:
            assert (await http.get("/sessions/claude:s-1")).status_code == 404
        assert (await _get(daemon, "/health"))["degraded"] == ["agents"]
    finally:
        await daemon.stop()


async def test_a_degraded_collector_leaves_out_only_its_own_entries() -> None:
    clock = FakeClock(T0)
    agents, more = sessions_collector("agents"), sessions_collector("more")
    daemon = make_daemon(windows_collector(), agents, more, clock=clock)
    await daemon.start()
    try:
        _world(daemon)
        daemon.sink("more").emit(session_snapshot("more", [session("opencode", "o-1")], T0))
        await daemon.settle()
        await _degrade(daemon, agents, clock)
        now = await _get(daemon, "/now")
        assert [s["key"] for s in now["sessions"]] == ["opencode:o-1"]
        assert now["degraded"] == ["agents"]
    finally:
        await daemon.stop()


async def test_a_disabled_windows_collector_leaves_workspaces_and_focus_absent() -> None:
    daemon = make_daemon(
        windows_collector(),
        sessions_collector(),
        user_config={"collectors": {"wm": {"enabled": False}}},
    )
    await daemon.start()
    try:
        now = await _get(daemon, "/now")
        assert "workspaces" not in now
        assert "focused" not in now
        assert now["sessions"] == []
        assert by_key(now["collectors"], "name")["wm"]["state"] == "disabled"
        assert now["degraded"] == []
        assert await _get(daemon, "/workspaces") == []
    finally:
        await daemon.stop()


async def test_a_failed_collector_is_named_in_degraded() -> None:
    crashing = Synthetic("agents", sessions_collector().capabilities, crash=True)
    daemon = make_daemon(windows_collector(), crashing)
    await daemon.start()
    try:
        await daemon.settle()
        now = await _get(daemon, "/now")
        assert "sessions" not in now
        assert now["degraded"] == ["agents"]
        info = by_key(now["collectors"], "name")["agents"]
        assert (info["state"], info["last_error_kind"]) == ("failed", "RuntimeError")
    finally:
        await daemon.stop()


async def test_with_no_collector_at_all_now_is_minimal() -> None:
    daemon = make_daemon()
    await daemon.start()
    try:
        now = await _get(daemon, "/now")
        assert set(now) == {"schema", "generated_at", "collectors", "degraded"}
        assert now["collectors"] == []
        assert now["degraded"] == []
    finally:
        await daemon.stop()


# --- edge cases of the model ---------------------------------------------------------------


async def test_a_state_outside_the_vocabulary_is_unknown_and_a_shared_window_has_no_session() -> (
    None
):
    daemon = make_daemon(windows_collector(), sessions_collector())
    await daemon.start()
    try:
        _world(daemon)
        agents = daemon.sink("agents")
        agents.emit(
            session_snapshot(
                "agents",
                [
                    session("claude", "s-1", pid=501, cwd="/work/alpha", host_pid=500),
                    session("claude", "s-9", pid=901, cwd="/work/alpha", host_pid=500),
                ],
                T0,
            )
        )
        agents.emit(
            activity_snapshot(
                "agents",
                {"claude:s-9": SessionActivity(state="frobnicating", facts={"turns": "x"})},
                T0,
            )
        )
        await daemon.settle()
        now = await _get(daemon, "/now")
        sessions = by_key(now["sessions"], "key")
        assert sessions["claude:s-9"]["state"] == "unknown"
        assert "facts" not in sessions["claude:s-9"]  # a facts block that does not fit is left out
        assert sessions["claude:s-9"]["one_liner"] == "—"
        assert sessions["claude:s-9"]["one_liner_from"] == "none"
        assert "session_key" not in now["focused"]  # two sessions share the focused window
    finally:
        await daemon.stop()


def test_the_client_base_url_is_an_allowed_host() -> None:
    assert BASE_URL == "http://127.0.0.1:7319"
