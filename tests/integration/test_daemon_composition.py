"""The daemon composition (WP1.9): order, data flow and plugin-agnostic core.

* start and stop run in order (sinks before sources; collectors stop first);
* records from fake collectors reach ``/now`` through the ingest gate, the writer, the live view
  and the resolver;
* the live view is rebuilt from the store on start;
* ``core/daemon.py`` names no collector.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from api_support import (
    HOME,
    T0,
    Synthetic,
    Win,
    activity_snapshot,
    by_key,
    client,
    config,
    entry_points,
    focus_event,
    make_daemon,
    session,
    session_snapshot,
    sessions_collector,
    window_snapshot,
    windows_collector,
)

import fylgja.core.daemon as daemon_module
from fylgja.collectors.base import Capability, CollectorContext
from fylgja.collectors.registry import discover
from fylgja.core import hosts
from fylgja.core.daemon import Daemon, SessionMemory, _Tap, _UnroutedSink
from fylgja.core.hosts import HostData
from fylgja.core.system import LocalFileSystem, SubprocessRunner
from fylgja.models import (
    HealthState,
    HookEvent,
    HookEventPayload,
    SessionActivity,
    SessionActivityEvent,
    SessionEvent,
    SessionPayload,
    TodoEvent,
    TodoPayload,
)
from fylgja.store import db
from fylgja.testing.fakes import FakeClock, FakeFS, FakeProcessRunner

START = ["start:writer", "start:live_view", "start:state_engine", "start:supervisor"]
STOP = ["stop:supervisor", "stop:state_engine", "stop:writer"]

PLANNED_BUILT_INS = ("aerospace", "claude", "codex", "opencode", "norn", "presence", "appcontext")
"""Every built-in collector of §3.1 except ``project``, which is an ordinary word."""
ORDINARY_WORDS = frozenset({"project"})
"""Built-in names that are also nouns of the core (projects are a core concept): the daemon may say
the word, so only the AST check below (no identifier, constant or import of exactly that name)
applies to them."""


async def _get(daemon: Daemon, path: str) -> Any:
    async with client(daemon) as http:
        response = await http.get(path)
    assert response.status_code == 200, response.text
    return response.json()


# --- order ---------------------------------------------------------------------------------


async def test_start_and_stop_run_in_order() -> None:
    daemon = make_daemon(windows_collector())
    assert (daemon.running, daemon.started) == (False, False)
    await daemon.start()
    assert daemon.lifecycle == START
    assert daemon.running
    assert daemon.started_at == T0
    assert daemon.supervisor.task("wm") is not None
    await daemon.stop()
    assert daemon.lifecycle == START + STOP
    assert not daemon.running
    assert daemon.started
    task = daemon.supervisor.task("wm")
    assert task is not None
    assert task.done()
    assert not daemon.writer.running


async def test_start_twice_is_refused_and_stop_is_idempotent() -> None:
    daemon = make_daemon()
    await daemon.stop()  # never started: nothing to do
    await daemon.start()
    with pytest.raises(RuntimeError):
        await daemon.start()
    await daemon.stop()
    await daemon.stop()
    assert daemon.lifecycle == START + STOP


async def test_a_failed_start_stops_what_it_started(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = make_daemon(windows_collector())

    def broken() -> None:
        raise sqlite3.OperationalError("cannot read the store")

    monkeypatch.setattr(daemon, "_rebuild_view", broken)
    with pytest.raises(sqlite3.OperationalError):
        await daemon.start()
    assert daemon.lifecycle == ["start:writer", "stop:writer"]
    assert not daemon.running
    assert not daemon.writer.running
    assert daemon.supervisor.task("wm") is None  # no collector ever started


async def test_stopping_persists_every_collector_s_final_health() -> None:
    daemon = make_daemon(windows_collector(), sessions_collector())
    await daemon.start()
    await daemon.stop()
    connection = db.connect_reader(daemon.writer.database)
    try:
        rows = {r["name"]: r["state"] for r in connection.execute("SELECT * FROM collector_health")}
    finally:
        connection.close()
    assert rows == {"wm": "disabled", "agents": "disabled"}  # stopped


# --- data flow -----------------------------------------------------------------------------


def _emitting_windows(context: CollectorContext) -> None:
    now = context.clock.now()
    context.sink.emit(
        window_snapshot(
            "wm", [Win("7", "3", "Editor", 70, "notes.md"), Win("8", "3", "Mail", 80)], now
        )
    )
    context.sink.emit(focus_event("wm", "7", "3", now))


def _emitting_sessions(context: CollectorContext) -> None:
    now = context.clock.now()
    context.sink.emit(
        session_snapshot(
            "agents",
            [session("claude", "a", pid=71, cwd="/work/notes", host_pid=70, entrypoint="cli")],
            now,
        )
    )
    context.sink.emit(
        activity_snapshot(
            "agents",
            {"claude:a": SessionActivity(state="idle", title="Notes", facts={"turns": 4})},
            now,
        )
    )


async def test_fake_collectors_flow_through_to_now() -> None:
    wm = Synthetic("wm", frozenset({Capability.WINDOWS}), emit=_emitting_windows)
    agents = Synthetic("agents", frozenset({Capability.SESSIONS}), emit=_emitting_sessions)
    daemon = make_daemon(wm, agents)
    await daemon.start()
    try:
        await daemon.settle()
        now = await _get(daemon, "/now")
    finally:
        await daemon.stop()
    workspace = by_key(now["workspaces"], "name")["3"]
    assert [w["id"] for w in workspace["windows"]] == ["7", "8"]
    assert workspace["label"] == "notes"
    assert workspace["one_liner"] == "notes — Notes"
    assert workspace["sessions"] == ["claude:a"]
    (only,) = now["sessions"]
    assert only["host"]["window_id"] == "7"
    assert (only["entrypoint"], only["facts"], only["one_liner_from"]) == (
        "cli",
        {"turns": 4},
        "title_reply",
    )
    assert now["focused"]["session_key"] == "claude:a"
    assert daemon.resolution is not None
    assert set(daemon.resolution.sessions) == {"claude:a"}


async def test_the_live_view_is_rebuilt_from_the_store_on_start() -> None:
    first = make_daemon(windows_collector())
    await first.start()
    first.sink("wm").emit(window_snapshot("wm", [Win("5", "4", "App", 50, "kept")], T0))
    await first.settle()
    await first.stop()

    second = make_daemon(windows_collector())  # same per-test database
    assert second.view.version == 0
    await second.start()
    try:
        now = await _get(second, "/now")
    finally:
        await second.stop()
    assert by_key(now["workspaces"], "name")["4"]["windows"][0]["title"] == "kept"


async def test_the_span_window_is_loaded_at_start_and_fed_by_the_span_engine() -> None:
    clock = FakeClock(T0)
    first = make_daemon(windows_collector(), clock=clock)
    assert first.span_window.apply in first.spans.listeners
    assert first.spans.on_away == first.view.touch
    await first.start()
    assert first.span_window.loaded
    first.sink("wm").emit(focus_event("wm", "5", "4", T0))
    await first.settle()
    assert [(s.window_id, s.ended_at) for s in first.span_window.view(T0)] == [("5", None)]
    assert first.activity().working
    await clock.advance(60)
    await first.stop()  # the open span ends with the daemon

    later = FakeClock(T0 + timedelta(minutes=5))
    second = make_daemon(windows_collector(), clock=later)  # same per-test database
    assert not second.span_window.loaded
    assert second.span_window.view(later.now()) == ()
    await second.start()
    try:
        held = second.span_window.view(later.now())
        assert [(s.window_id, s.ended_at, s.end_reason) for s in held] == [
            ("5", T0 + timedelta(seconds=60), "shutdown")
        ]
        activity = second.activity()
        assert (activity.active_seconds, activity.working) == (60.0, False)
    finally:
        await second.stop()
    assert second.lifecycle == START + STOP  # the load is part of the writer's start


async def test_a_record_the_gate_rejects_never_reaches_now() -> None:
    daemon = make_daemon(windows_collector(), sessions_collector())
    await daemon.start()
    try:
        # A sessions collector may not report windows: its record is rejected by the gate.
        daemon.sink("agents").emit(window_snapshot("agents", [Win("9", "9", "App", 90)], T0))
        daemon.sink("wm").emit(window_snapshot("other", [Win("9", "9", "App", 90)], T0))
        await daemon.settle()
        now = await _get(daemon, "/now")
    finally:
        await daemon.stop()
    assert now["workspaces"] == []
    assert sum(daemon.gate.stats(name).rejected_total for name in ("agents", "wm")) == 2


async def test_records_committed_through_emit_committed_are_observed_too() -> None:
    daemon = make_daemon(sessions_collector())
    await daemon.start()
    try:
        await daemon.sink("agents").emit_committed(
            SessionEvent(
                source="agents",
                entity_id="claude:e",
                observed_at=T0,
                payload=SessionPayload(kind="claude", native_id="e", version="2.1.300"),
            )
        )
        await daemon.settle()
        (only,) = (await _get(daemon, "/now"))["sessions"]
    finally:
        await daemon.stop()
    assert (only["key"], only["version"]) == ("claude:e", "2.1.300")


async def test_hook_time_cmux_ids_make_the_resolver_refresh_and_join_exactly() -> None:
    """A hook event with ``CMUX_*`` ids reaches the resolver and triggers a refresh (WP2.2)."""
    cmux = Win("66", "1", "cmux", 400, "t", "com.cmuxterm.app")

    async def loader() -> HostData:
        return HostData(home=HOME)

    daemon = make_daemon(windows_collector(), sessions_collector(), host_loader=loader)
    await daemon.start()
    try:
        daemon.sink("wm").emit(window_snapshot("wm", [cmux], T0))
        daemon.sink("agents").emit(
            session_snapshot(
                "agents", [session("claude", "x", pid=900, host_pid=400, cwd="/code/app")], T0
            )
        )
        await daemon.settle()
        refreshed = daemon.resolver.refresh
        calls: list[int] = []

        async def counting() -> Any:
            calls.append(1)
            return await refreshed()

        daemon.resolver.refresh = counting  # type: ignore[method-assign]
        await daemon.sink("agents").emit_committed(
            HookEvent(
                source="agents",
                entity_id="claude:x",
                observed_at=T0,
                payload=HookEventPayload(
                    session_id="x",
                    event="Stop",
                    ids={"CMUX_WORKSPACE_ID": "hw", "CMUX_SURFACE_ID": "hs"},
                ),
            )
        )
        await daemon.settle()
        assert calls, "the ids did not trigger a refresh"
        assert daemon.resolution is not None
        assert daemon.resolution.sessions["claude:x"].via == "cmux-hook-env"
    finally:
        await daemon.stop()


async def test_an_ended_session_is_forgotten() -> None:
    daemon = make_daemon(sessions_collector())
    await daemon.start()
    try:
        sink = daemon.sink("agents")
        sink.emit(session_snapshot("agents", [session("claude", "gone", version="1")], T0))
        await daemon.settle()
        assert daemon.memory.keys() == {"claude:gone"}
        sink.emit(
            SessionEvent(
                source="agents",
                entity_id="claude:gone",
                observed_at=T0,
                payload=SessionPayload(kind="claude", native_id="gone", ended_at=T0),
            )
        )
        await daemon.settle()
        assert (await _get(daemon, "/now"))["sessions"] == []
        assert daemon.memory.keys() == set()
    finally:
        await daemon.stop()


# --- the state engine ----------------------------------------------------------------------


async def test_the_resolver_reads_host_data_only_while_sessions_need_placing() -> None:
    calls: list[str] = []

    async def loader() -> HostData:
        calls.append("load")
        return HostData(home=HOME)

    daemon = make_daemon(windows_collector(), sessions_collector(), host_loader=loader)
    await daemon.start()
    try:
        daemon.sink("wm").emit(window_snapshot("wm", [Win("1", "1", "App", 10)], T0))
        await daemon.settle()
        assert calls == []
        daemon.sink("agents").emit(session_snapshot("agents", [session("claude", "x")], T0))
        await daemon.settle()
        assert calls
    finally:
        await daemon.stop()


async def test_the_tick_refreshes_the_resolver_and_the_gate() -> None:
    clock = FakeClock(T0)
    calls: list[str] = []

    async def loader() -> HostData:
        calls.append("load")
        return HostData(home=HOME)

    daemon = make_daemon(sessions_collector(), clock=clock, host_loader=loader, tick=30.0)
    await daemon.start()
    try:
        daemon.sink("agents").emit(session_snapshot("agents", [session("claude", "x")], T0))
        await daemon.settle()
        before = len(calls)
        await clock.advance(30.0)
        await daemon.settle()
        assert len(calls) > before
    finally:
        await daemon.stop()


async def test_a_failing_refresh_is_logged_and_the_daemon_keeps_serving(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken() -> HostData:
        raise OSError("host data unreadable")

    daemon = make_daemon(sessions_collector(), host_loader=broken)
    await daemon.start()
    try:
        with caplog.at_level(logging.ERROR, logger="fylgja.daemon"):
            daemon.sink("agents").emit(session_snapshot("agents", [session("claude", "x")], T0))
            await daemon.settle()
        assert "resolver refresh failed" in caplog.text
        assert [s["key"] for s in (await _get(daemon, "/now"))["sessions"]] == ["claude:x"]
    finally:
        await daemon.stop()


async def test_settle_with_a_debounce_on_a_fake_clock_only_flushes() -> None:
    daemon = make_daemon(windows_collector(), resolve_debounce=0.25)
    await daemon.start()
    try:
        daemon.sink("wm").emit(window_snapshot("wm", [Win("1", "1", "App", 10)], T0))
        await daemon.settle()
        assert len(daemon.view.windows) == 1
    finally:
        await daemon.stop()
    await daemon.settle()  # not running: returns at once


async def test_a_debounced_refresh_runs_on_the_real_clock() -> None:
    real = Daemon(
        config(),
        entry_points=entry_points(sessions_collector()),
        fs=FakeFS(),
        host_loader=_hosts,
        home=HOME,
        resolve_debounce=0.01,
    )
    await real.start()
    try:
        real.sink("agents").emit(
            session_snapshot("agents", [session("claude", "r")], real.clock.now())
        )
        await real.settle()
        assert real.resolution is not None
        assert "claude:r" in real.resolution.sessions
    finally:
        await real.stop()


async def _hosts() -> HostData:
    return HostData(home=HOME)


# --- collectors as the daemon sees them ----------------------------------------------------


async def test_collector_states_rows_and_reports() -> None:
    external = Synthetic("vendor", frozenset({Capability.CONTEXT}), builtin=False)
    daemon = make_daemon(windows_collector(), external)
    states = {s.name: s for s in daemon.collector_states()}
    assert states["wm"].available
    assert states["wm"].health.state is HealthState.STARTING
    assert states["vendor"].health.state is HealthState.DISABLED
    assert not states["vendor"].available
    assert not states["vendor"].enabled
    rows = {row.name: row for row in daemon.collector_rows()}
    assert rows["vendor"].api_version == "not_loaded"
    assert rows["wm"].health != "not_loaded"
    assert set(daemon.health_reports()) == {"wm", "vendor"}


async def test_a_duplicate_name_is_reported_failed_and_never_supervised() -> None:
    builtin = windows_collector("dup")
    rival = Synthetic("dup", frozenset({Capability.CONTEXT}), builtin=False)
    daemon = make_daemon(builtin, rival)
    states = [s for s in daemon.collector_states() if s.name == "dup"]
    assert sorted(s.health.state.value for s in states) == ["failed", "starting"]
    failed = next(s for s in states if s.health.state is HealthState.FAILED)
    assert failed.health.last_error_kind == "duplicate"
    assert set(daemon.health_reports()) == {"dup"}
    await daemon.start()
    try:
        now = await _get(daemon, "/now")
    finally:
        await daemon.stop()
    assert now["degraded"] == ["dup"]
    assert [c["state"] for c in now["collectors"]] == ["starting", "failed"]


# --- the pieces ----------------------------------------------------------------------------


def test_session_memory_follows_snapshots_and_events() -> None:
    memory = SessionMemory()
    memory.observe(
        session_snapshot("a", [session("k", "1", entrypoint="cli"), session("k", "2")], T0)
    )
    memory.observe(activity_snapshot("a", {"k:1": SessionActivity(facts={"turns": 1})}, T0))
    memory.observe(
        SessionActivityEvent(
            source="b", entity_id="x:9", observed_at=T0, payload=SessionActivity(facts={"turns": 9})
        )
    )
    first = memory.get("k:1")
    assert first is not None
    assert (first.entrypoint, first.facts) == ("cli", {"turns": 1})
    assert memory.keys() == {"k:1", "k:2", "x:9"}
    # A complete snapshot of source "a" forgets what it no longer lists, and only for "a".
    memory.observe(session_snapshot("a", [session("k", "1")], T0))
    memory.observe(activity_snapshot("a", {"k:1": SessionActivity()}, T0))
    assert memory.keys() == {"k:1", "x:9"}
    assert memory.get("k:1") is not None
    assert memory.get("k:1").facts is None
    memory.forget("x:9")
    memory.observe(
        TodoEvent(source="a", entity_id="t", observed_at=T0, payload=TodoPayload(text="t"))
    )
    assert memory.keys() == {"k:1"}
    assert memory.get("nope") is None


async def test_a_failing_observer_never_costs_the_record(caplog: pytest.LogCaptureFixture) -> None:
    daemon = make_daemon(sessions_collector())
    seen: list[str] = []

    def broken(record: Any) -> None:
        raise ValueError("observer bug")

    tap = _Tap(daemon.writer, (broken, lambda r: seen.append(r.entity_id)))
    await daemon.start()
    try:
        with caplog.at_level(logging.ERROR, logger="fylgja.daemon"):
            tap.submit(session_snapshot("agents", [session("claude", "t")], T0))
            await daemon.settle()
    finally:
        await daemon.stop()
    assert seen == ["all"]
    assert "state engine observer failed" in caplog.text
    assert "claude:t" in {row["session_key"] for row in _rows(daemon, "sessions")}


def _rows(daemon: Daemon, table: str) -> list[sqlite3.Row]:
    connection = db.connect_reader(daemon.writer.database)
    try:
        return list(connection.execute(f"SELECT * FROM {table}"))  # noqa: S608 - a test constant
    finally:
        connection.close()


async def test_the_unrouted_sink_drops(caplog: pytest.LogCaptureFixture) -> None:
    sink = _UnroutedSink()
    with caplog.at_level(logging.WARNING, logger="fylgja.daemon"):
        await sink.emit_committed(session_snapshot("x", [], T0))
    assert "dropped a record" in caplog.text


async def test_the_default_host_loader_reads_through_the_seams() -> None:
    runner = FakeProcessRunner()
    runner.script(hosts.CMUX_CLI, "--version", stdout="cmux 0.64.25 (99)\n")
    fs = FakeFS()
    loader = hosts.host_loader(runner, fs, home=HOME)
    first = await loader()
    second = await loader()
    assert first.home == HOME
    assert second.cmux.status is not None
    assert second.cmux.status.version == "0.64.25"
    assert runner.calls == [(hosts.CMUX_CLI, "--version")]  # detected once


async def test_the_real_seams(isolated_env: Path) -> None:
    fs = LocalFileSystem()
    folder = isolated_env / "seams"
    folder.mkdir()
    target = folder / "a.txt"
    target.write_text("hé", encoding="utf-8")
    assert fs.read_text(target) == "hé"
    assert fs.exists(target)
    assert not fs.is_dir(target)
    assert fs.listdir(folder) == ["a.txt"]
    assert fs.stat(target).size == 3
    assert fs.is_dir(folder)
    assert fs.stat(folder).is_dir
    fs.unlink(target)
    assert not fs.exists(target)
    result = await SubprocessRunner().run([sys.executable, "-c", "print('hi')"], timeout=20)
    assert (result.returncode, result.stdout.strip()) == (0, b"hi")
    with pytest.raises(TimeoutError):
        await SubprocessRunner().run(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5
        )
    piped = await SubprocessRunner().run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read())"], stdin=b"in", timeout=20
    )
    assert piped.stdout.strip() == b"in"


def test_the_daemon_builds_its_own_seams_by_default() -> None:
    daemon = Daemon(config(), entry_points=entry_points())
    assert daemon.clock.now().tzinfo is not None
    assert daemon.collector_states() == ()


# --- no collector names in the core ---------------------------------------------------------


def _collector_names() -> set[str]:
    names = {entry.name for entry in discover().entries} | set(PLANNED_BUILT_INS)
    return {name for name in names if name}


def test_no_collector_names_in_core_daemon() -> None:
    path = Path(daemon_module.__file__)
    source = path.read_text(encoding="utf-8")
    names = _collector_names()
    assert {"aerospace", "claude"} <= names  # the installed built-ins are in the list
    lowered = source.lower()
    named = [n for n in names - ORDINARY_WORDS if n.lower() in lowered and len(n) > 4]
    assert named == []
    tree = ast.parse(source)
    words: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            words.add(node.value.lower())
        elif isinstance(node, ast.Name):
            words.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            words.add(node.attr.lower())
        elif isinstance(node, ast.alias):
            words.update(part.lower() for part in node.name.split("."))
    assert words & {name.lower() for name in names} == set()
    assert "fylgja.collectors.aerospace" not in source
    assert "fylgja.collectors.claude" not in source


def test_the_daemon_module_imports_no_collector_package() -> None:
    tree = ast.parse(Path(daemon_module.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    collector_packages = {m for m in imported if m.startswith("fylgja.collectors.")}
    assert collector_packages <= {
        "fylgja.collectors.base",
        "fylgja.collectors.context",
        "fylgja.collectors.registry",
        "fylgja.collectors.seams",
        "fylgja.collectors.supervisor",
    }


def test_asyncio_is_the_only_concurrency() -> None:
    assert asyncio.iscoroutinefunction(Daemon.start)
    assert asyncio.iscoroutinefunction(Daemon.stop)
