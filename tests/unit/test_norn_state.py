"""Run state of the norn collector under ``FakeClock`` and fake pid liveness (A10).

The plugin's events come from the committed inbox fixtures (``fylgja.hook/1`` envelopes drained
by the shared inbox drainer); the files' view comes from the sanitized run files. A crashed run is
``stale`` both with and without the plugin; ``sdk-py`` sessions nest under their run, also
through the daemon's live view.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from norn_support import (
    FILES_NOW,
    INBOX_NOW,
    SUBSCRIBER_PID,
    Rig,
    by_suffix,
    copy_case,
    deliver,
    index_of,
    ps_table,
)

from fylgja.collectors.base import (
    COLLECTOR_API_MAJOR,
    Capability,
    CollectorContext,
    CollectorPlugin,
)
from fylgja.collectors.context import build_context
from fylgja.collectors.norn import adapter, parse, state
from fylgja.collectors.norn.collector import NornCollector, NornSettings
from fylgja.collectors.registry import EntryPointLike
from fylgja.core.daemon import Daemon
from fylgja.core.one_liner import session_one_liner
from fylgja.models import (
    EntityKind,
    HealthReport,
    HealthState,
    SessionActivity,
    SessionActivitySnapshot,
    SessionEvent,
    SessionPayload,
    SessionSnapshot,
)
from fylgja.testing.fakes import FakeClock, FakeFS, FakeProcessRunner

T = INBOX_NOW


def rig(**settings: Any) -> Rig:
    return Rig(start=INBOX_NOW, alive={SUBSCRIBER_PID}, **settings)


async def feed(r: Rig, scenario: str, stop: int | None = None, *, start: int = 0) -> None:
    deliver(r.inbox_dir, scenario, start=start, stop=stop)
    await r.drain()


def activity_of(r: Rig, suffix: str) -> SessionActivity:
    """The activity of the run whose key ends with ``suffix`` in the newest snapshot."""
    snapshots = [x for x in r.sink.records if isinstance(x, SessionActivitySnapshot)]
    items = {i.entity_id: i.payload for i in snapshots[-1].items}
    found = [payload for key, payload in items.items() if key.endswith(suffix)]
    assert len(found) == 1, list(items)
    return found[0]


# --- heartbeat freshness ----------------------------------------------------------------------


async def test_a_run_is_running_while_the_heartbeat_is_fresh_and_the_pid_is_alive() -> None:
    r = rig()
    await feed(r, "lifecycle", index_of("lifecycle", "Heartbeat") + 1)
    await r.clock.advance(30)  # the heartbeat arrived at +30 s
    await r.poll()
    assert by_suffix(r.states(), ":7") == "running"
    activity = activity_of(r, ":7")
    assert activity.state == "running"
    assert activity.stage is not None
    assert (activity.stage.name, activity.stage.index, activity.stage.total) == ("draft plan", 2, 5)


async def test_the_heartbeat_boundary_is_90_seconds() -> None:
    r = rig()
    await feed(r, "lifecycle", index_of("lifecycle", "Heartbeat") + 1)
    await r.clock.advance(30 + 90)  # exactly 90 s after the heartbeat
    await r.poll()
    assert by_suffix(r.states(), ":7") == "running"
    await r.clock.advance(0.5)
    await r.poll()
    assert by_suffix(r.states(), ":7") == "stale"


async def test_a_dead_pid_makes_a_fresh_run_stale_at_once() -> None:
    r = rig()
    await feed(r, "lifecycle", index_of("lifecycle", "Heartbeat") + 1)
    await r.clock.advance(31)
    await r.poll()
    assert by_suffix(r.states(), ":7") == "running"
    r.alive.clear()  # kill -9: no finish event, no more heartbeats
    await r.poll()
    assert by_suffix(r.states(), ":7") == "stale"
    assert session_one_liner(activity_of(r, ":7")) == ("plan_with_review stale", "stage")


async def test_a_run_whose_pid_is_unknown_is_trusted_while_its_heartbeat_is_fresh() -> None:
    event = parse.NornEvent(
        name="Heartbeat",
        at=T,
        received_at_ns=1,
        envelope_id="e",
        key="norn:x@a:1",
        pipeline="x",
    )
    run = state.apply_event(None, event)
    assert run.pid is None
    assert state.status(run, now=T + timedelta(seconds=90), pid_alive=_never_alive) == "running"
    assert state.status(run, now=T + timedelta(seconds=91), pid_alive=_never_alive) == "stale"


def _never_alive(pid: int) -> bool:  # pragma: no cover - must not be asked
    raise AssertionError(f"pid {pid} was asked")


# --- WaitingInput and the other states --------------------------------------------------------


async def test_waiting_input_shows_until_the_run_moves_on() -> None:
    r = rig()
    waiting_at = index_of("lifecycle", "WaitingInput") + 1
    await feed(r, "lifecycle", waiting_at)
    await r.clock.advance(40)
    await r.poll()
    assert by_suffix(r.states(), ":7") == "waiting_input"
    activity = activity_of(r, ":7")
    assert activity.waiting_reason == "agent question"
    assert session_one_liner(activity) == ("Waiting for input: agent question", "waiting")

    await feed(r, "lifecycle", waiting_at + 1, start=waiting_at)  # the next StageStarted
    await r.clock.advance(20)
    await r.poll()
    assert by_suffix(r.states(), ":7") == "running"
    assert activity_of(r, ":7").waiting_reason is None


async def test_a_paused_run_is_paused_and_then_cancelled() -> None:
    r = rig()
    await feed(r, "cancelled", index_of("cancelled", "RunPaused") + 1)
    await r.clock.advance(5)
    await r.poll()
    assert by_suffix(r.states(), ":9") == "paused"
    await feed(r, "cancelled", start=index_of("cancelled", "RunPaused") + 1)
    await r.clock.advance(4)
    await r.poll()
    assert by_suffix(r.states(), ":9") == "cancelled"


@pytest.mark.parametrize(
    ("scenario", "suffix", "terminal"),
    [("lifecycle", ":7", "passed"), ("failed", ":8", "failed"), ("cancelled", ":9", "cancelled")],
)
async def test_each_finish_event_gives_a_terminal_state_that_never_goes_stale(
    scenario: str, suffix: str, terminal: str
) -> None:
    r = rig()
    await feed(r, scenario)
    await r.clock.advance(100)
    await r.poll()
    assert by_suffix(r.states(), suffix) == terminal
    text, rule = session_one_liner(activity_of(r, suffix))
    assert (text, rule) == (f"plan_with_review {terminal}", "stage")
    r.alive.clear()  # the process ended, as it does after a finish
    await r.clock.advance(300)
    await r.poll()
    assert by_suffix(r.states(), suffix) == terminal


async def test_a_finished_run_leaves_the_listing_after_finished_seconds() -> None:
    r = rig(finished_seconds=900)
    await feed(r, "failed")
    await r.clock.advance(3 + 900)
    await r.poll()
    assert by_suffix(r.states(), ":8") == "failed"
    await r.clock.advance(1)
    await r.poll()
    assert r.states() == {}
    snapshot = [x for x in r.sink.records if isinstance(x, SessionSnapshot)][-1]
    assert snapshot.items == ()  # a complete snapshot: the store ends the run


async def test_events_after_a_finish_and_replays_change_nothing() -> None:
    r = rig()
    await feed(r, "lifecycle")
    deliver(r.inbox_dir, "lifecycle")  # the same envelopes again (a replay after a crash)
    await r.drain()
    await r.clock.advance(100)
    await r.poll()
    assert by_suffix(r.states(), ":7") == "passed"
    assert r.collector.inbox.totals.quarantined == 0


async def test_an_older_event_does_not_undo_a_newer_one() -> None:
    r = rig()
    await feed(r, "lifecycle", 3)
    await feed(r, "lifecycle", 2, start=1)  # StageStarted again, older than what was applied
    run = next(iter(r.collector._plugin.values()))
    assert run.stage_names == ("preflight",)


async def test_a_run_is_rebuilt_from_a_heartbeat_after_a_restart() -> None:
    r = rig()
    heartbeat = index_of("lifecycle", "Heartbeat")
    await feed(r, "lifecycle", heartbeat + 1, start=heartbeat)  # the first envelopes were lost
    await r.clock.advance(31)
    await r.poll()
    assert by_suffix(r.states(), ":7") == "running"
    run = next(iter(r.collector._plugin.values()))
    assert (run.pipeline, run.pid, run.stage) == ("plan_with_review", SUBSCRIBER_PID, "draft plan")


# --- a crashed run is stale, with and without the plugin ------------------------------------


async def test_a_crashed_run_is_stale_with_the_plugin() -> None:
    r = rig()
    await feed(r, "lifecycle", index_of("lifecycle", "Heartbeat") + 1)
    await r.clock.advance(30)
    await r.poll()
    assert by_suffix(r.states(), ":7") == "running"
    await r.clock.advance(91)  # no heartbeat since: the process is gone or stuck
    await r.poll()
    assert by_suffix(r.states(), ":7") == "stale"
    report = await r.collector.doctor()
    assert report.state is HealthState.DEGRADED or "1 stale" in (report.detail or "")


async def test_a_crashed_run_is_stale_without_the_plugin(tmp_path: Path) -> None:
    root = copy_case(tmp_path, "crashed")
    r = Rig(roots=[str(root)], start=FILES_NOW)
    r.script_processes(ps_table((1, "/sbin/launchd"), (700, "/bin/zsh -l")))
    await r.poll()
    assert [v for v in r.states().values()] == ["stale"]
    activity = activity_of(r, ":1")
    assert activity.state == "stale"
    assert session_one_liner(activity) == ("plan_with_review stale", "stage")
    assert (await r.collector.doctor()).state is HealthState.OK


async def test_an_unfinished_run_with_a_matching_process_is_running_until_it_ends(
    tmp_path: Path,
) -> None:
    root = copy_case(tmp_path, "crashed")
    argv = "/Users/user/projects/erik.romson/norn/main/norn/__main__.py ui"
    cwd = "/Users/user/projects/erik-romson/fylgja/main"
    r = Rig(roots=[str(root)], start=FILES_NOW, alive={4100})
    r.script_processes(
        ps_table((1, "/sbin/launchd"), (4100, f"/usr/bin/python3 {argv}")), cwds={4100: cwd}
    )
    await r.poll()
    assert list(r.states().values()) == ["running"]
    activity = activity_of(r, ":1")
    assert activity.stage is not None
    assert activity.stage.name == "check open questions"
    assert activity.stage.index is not None
    assert activity.stage.total is None  # the files do not say how many stages there will be
    assert session_one_liner(activity) == ("plan_with_review · check open questions", "stage")
    r.alive.clear()  # the process exits without a finish event
    await r.poll()
    assert list(r.states().values()) == ["stale"]


async def test_a_process_in_another_directory_does_not_match(tmp_path: Path) -> None:
    root = copy_case(tmp_path, "crashed")
    argv = "/Users/user/projects/erik.romson/norn/main/norn/__main__.py ui"
    r = Rig(roots=[str(root)], start=FILES_NOW, alive={4100})
    r.script_processes(ps_table((4100, f"/usr/bin/python3 {argv}")), cwds={4100: "/elsewhere"})
    await r.poll()
    assert list(r.states().values()) == ["stale"]


async def test_a_failing_process_table_keeps_the_last_match_and_degrades(tmp_path: Path) -> None:
    root = copy_case(tmp_path, "crashed")
    argv = "/Users/user/projects/erik.romson/norn/main/norn/__main__.py ui"
    cwd = "/Users/user/projects/erik-romson/fylgja/main"
    r = Rig(roots=[str(root)], start=FILES_NOW, alive={4100}, scan_interval_seconds=5)
    r.script_processes(ps_table((4100, f"/usr/bin/python3 {argv}")), cwds={4100: cwd})
    await r.poll()
    assert list(r.states().values()) == ["running"]
    r.runner._scripts.clear()  # ps disappears
    await r.clock.advance(6)
    await r.poll()
    assert list(r.states().values()) == ["running"]  # a failed command does not end a run
    report = await r.collector.doctor()
    assert (report.state, report.error_kind) == (HealthState.DEGRADED, "ps_missing")


# --- merging the plugin's view and the files' view --------------------------------------------


def test_the_plugin_wins_and_the_files_fill_the_gaps() -> None:
    plugin = state.Run(key="k", plugin=True, alive_at=T, stage="a", sessions=("s1",))
    files = state.Run(
        key="k",
        pipeline="p",
        cwd="/w",
        argv=("a", "b"),
        stage="b",
        sessions=("s2", "s1"),
        started_at=T,
    )
    merged = state.merge(plugin, files)
    assert merged is not None
    assert (merged.pipeline, merged.cwd, merged.argv, merged.stage) == ("p", "/w", ("a", "b"), "a")
    assert merged.sessions == ("s1", "s2")
    assert state.merge(None, files) is files
    assert state.merge(plugin, None) is plugin
    assert state.merge(None, None) is None


def test_a_finish_the_daemon_missed_is_taken_from_the_files() -> None:
    plugin = state.Run(key="k", plugin=True, alive_at=T, waiting="agent", stage_index=2)
    files = state.Run(key="k", outcome="failed", finished_at=T, stage="last", stage_total=9)
    merged = state.merge(plugin, files)
    assert merged is not None
    assert (merged.outcome, merged.waiting, merged.stage, merged.stage_total) == (
        "failed",
        None,
        "last",
        9,
    )
    done = state.Run(key="k", plugin=True, outcome="passed")
    assert state.merge(done, files).outcome == "passed"  # type: ignore[union-attr]


def test_a_plugin_run_without_a_stage_takes_the_files_stage() -> None:
    merged = state.merge(
        state.Run(key="k", plugin=True), state.Run(key="k", stage="s", stage_index=3)
    )
    assert merged is not None
    assert (merged.stage, merged.stage_index) == ("s", 3)


# --- nesting (sdk-py sessions under their run) ------------------------------------------------


async def test_agent_sessions_of_a_run_are_linked_to_it_once() -> None:
    r = rig()
    await feed(r, "lifecycle", index_of("lifecycle", "TurnEvent") + 1)
    await r.clock.advance(6)
    await r.poll()
    links = [x for x in r.sink.records if isinstance(x, SessionEvent)]
    assert len(links) == 1
    link = links[0]
    assert link.source == "norn"
    assert link.entity_id == "claude:00000000-0000-4000-8000-00000000a001"
    assert (link.payload.kind, link.payload.native_id) == (
        "claude",
        "00000000-0000-4000-8000-00000000a001",
    )
    assert link.payload.parent_key is not None
    assert link.payload.parent_key.startswith("norn:plan_with_review@")
    await r.poll()
    await r.clock.advance(2)
    await r.poll()
    assert len([x for x in r.sink.records if isinstance(x, SessionEvent)]) == 1

    await feed(r, "lifecycle", index_of("lifecycle", "TurnEvent") + 5)  # a second session id
    await r.clock.advance(60)
    await r.poll()
    links = [x for x in r.sink.records if isinstance(x, SessionEvent)]
    assert [x.entity_id[-4:] for x in links] == ["a001", "a002"]


async def test_a_finished_run_links_nothing_new() -> None:
    r = rig()
    await feed(r, "lifecycle")
    await r.clock.advance(100)
    await r.poll()
    assert by_suffix(r.states(), ":7") == "passed"
    assert [x for x in r.sink.records if isinstance(x, SessionEvent)] == []


async def test_only_claude_sessions_are_nested() -> None:
    run = state.Run(key="norn:x@a:1", provider="codex", sessions=("t1",))
    assert state.children_of(run) == ()
    assert state.children_of(state.Run(key="norn:x@a:1", sessions=("s",))) == (
        ("claude:s", "norn:x@a:1"),
    )


async def test_the_sessions_of_an_unfinished_run_in_the_files_are_nested(tmp_path: Path) -> None:
    root = copy_case(tmp_path, "crashed")
    r = Rig(roots=[str(root)], start=FILES_NOW)
    r.script_processes()
    await r.poll()
    links = [x for x in r.sink.records if isinstance(x, SessionEvent)]
    assert len(links) == 1
    assert links[0].payload.parent_key is not None


class _Idle:
    """A collector that only sits there: the test drives the real norn collector by hand."""

    def __init__(self, context: CollectorContext) -> None:
        self._context = context

    async def run(self) -> None:
        await asyncio.Event().wait()

    async def doctor(self) -> HealthReport:
        return HealthReport(state=HealthState.OK, checked_at=self._context.clock.now())


def _norn_daemon(clock: FakeClock) -> Daemon:
    """A daemon whose ``norn`` plugin has the real settings model and an idle collector."""
    from api_support import HOME, Dist, EntryPoint, config, no_hosts, sessions_collector

    norn = CollectorPlugin(
        name="norn",
        api_major=COLLECTOR_API_MAJOR,
        capabilities=frozenset({Capability.SESSIONS}),
        config_model=NornSettings,
        factory=lambda settings, context: _Idle(context),
    )
    agents = sessions_collector("agents").plugin()
    eps: list[EntryPointLike] = [
        EntryPoint(name=p.name, value=f"test.{p.name}:plugin", dist=Dist("fylgja"), target=p)
        for p in (agents, norn)
    ]
    return Daemon(
        config(),
        entry_points=lambda: list(eps),
        clock=clock,
        fs=FakeFS(),
        host_loader=no_hosts,
        home=HOME,
        resolve_debounce=0.0,
    )


async def test_nested_agent_sessions_are_listed_under_their_run_in_now() -> None:
    """Through the live view: the Claude session norn drives is a child of the run in ``/now``."""
    from api_support import client, session, session_snapshot

    clock = FakeClock(INBOX_NOW)
    daemon = _norn_daemon(clock)
    await daemon.start()
    try:
        agent = "00000000-0000-4000-8000-00000000a001"
        daemon.sink("agents").emit(
            session_snapshot(
                "agents",
                [session("claude", agent, pid=900, cwd="/Users/user/projects/example")],
                INBOX_NOW,
            )
        )
        collector = NornCollector(
            NornSettings(roots=[], inbox_watch="poll"),
            build_context("norn", daemon.sink("norn"), clock=clock),
            seams=adapter.Seams(
                FakeProcessRunner(), adapter.LocalFileSystem(), pid_alive=lambda pid: True
            ),
        )
        deliver(collector.inbox.directory, "lifecycle", stop=index_of("lifecycle", "TurnEvent") + 1)
        await collector.inbox.drain()
        await clock.advance(6)
        await collector.poll()
        await daemon.settle()

        async with client(daemon) as http:
            now = (await http.get("/now")).json()
            keys = [s["key"] for s in now["sessions"]]
            assert len(keys) == 1, keys  # the agent session is not an independent session
            run = now["sessions"][0]
            assert run["key"].startswith("norn:plan_with_review@")
            assert run["state"] == "running"
            assert run["pipeline"] == "plan_with_review"
            assert run["stage"] == {"name": "draft plan", "index": 2, "total": 5}
            assert run["one_liner"] == "plan_with_review · draft plan (2/5)"
            assert [c["key"] for c in run["children"]] == [f"claude:{agent}"]
            assert run["children"][0]["parent_key"] == run["key"]
            found = await http.get(f"/sessions/claude:{agent}")
            assert found.status_code == 200
            assert found.json()["parent_key"] == run["key"]
    finally:
        await daemon.stop()


async def test_a_session_whose_run_is_gone_is_a_normal_session() -> None:
    from api_support import client, session, session_snapshot

    clock = FakeClock(INBOX_NOW)
    daemon = _norn_daemon(clock)
    await daemon.start()
    try:
        daemon.sink("agents").emit(
            session_snapshot("agents", [session("claude", "orphan", pid=901)], INBOX_NOW)
        )
        daemon.sink("norn").emit(
            SessionEvent(
                source="norn",
                entity_id="claude:orphan",
                observed_at=INBOX_NOW + timedelta(seconds=1),
                payload=SessionPayload(
                    kind="claude", native_id="orphan", parent_key="norn:gone@000000:1"
                ),
            )
        )
        await daemon.settle()
        async with client(daemon) as http:
            now = (await http.get("/now")).json()
        assert [s["key"] for s in now["sessions"]] == ["claude:orphan"]
        assert now["sessions"][0]["parent_key"] == "norn:gone@000000:1"
        assert now["sessions"][0].get("children") is None
    finally:
        await daemon.stop()


# --- the records ------------------------------------------------------------------------------


async def test_records_use_the_norn_source_and_the_run_key() -> None:
    r = rig()
    await feed(r, "lifecycle", 3)
    await r.clock.advance(3)
    await r.poll()
    kinds = {x.entity_kind for x in r.sink.records}
    assert kinds == {EntityKind.SESSION, EntityKind.SESSION_ACTIVITY}
    snapshot = next(x for x in r.sink.records if isinstance(x, SessionSnapshot))
    (item,) = snapshot.items
    assert item.entity_id == f"norn:{item.payload.native_id}"
    assert (item.payload.kind, item.payload.pid, item.payload.name) == (
        "norn",
        SUBSCRIBER_PID,
        "plan_with_review",
    )
    assert item.payload.cwd == "/Users/user/projects/example"
    assert item.payload.started_at == datetime(2026, 10, 5, 14, 0, tzinfo=UTC)


async def test_an_unchanged_state_is_emitted_again_only_at_the_heartbeat() -> None:
    r = rig(heartbeat_seconds=30)
    await feed(r, "lifecycle", 2)
    await r.poll()
    first = r.collector.emissions
    await r.clock.advance(2)
    await r.poll()
    assert r.collector.emissions == first
    await r.clock.advance(30)
    await r.poll()
    assert r.collector.emissions == first + 1


async def test_a_bad_envelope_is_quarantined_and_the_rest_is_applied() -> None:
    r = rig()
    deliver(r.inbox_dir, "lifecycle", stop=2)
    bad = r.inbox_dir / "1791208899000000000-0b0b0b0b-0b0b-4b0b-8b0b-0b0b0b0b0b0b.json"
    bad.write_text('{"schema": "fylgja.hook/1"}', encoding="utf-8")
    await r.drain()
    assert r.collector.inbox.totals.quarantined == 1
    assert (r.inbox_dir / "quarantine" / bad.name).exists()
    await r.poll()
    assert by_suffix(r.states(), ":7") == "running"


async def test_the_inbox_is_drained_only_after_the_events_were_applied() -> None:
    r = rig()
    deliver(r.inbox_dir, "lifecycle", stop=3)
    await r.drain()
    assert sorted(p.name for p in r.inbox_dir.glob("*.json")) == []
    assert r.collector.inbox.totals.committed == 3
    assert r.sink.records == []  # nothing is committed as a hook event: the state is the record
