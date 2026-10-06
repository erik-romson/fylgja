"""Freshness of ``usage`` and ``sessions[].tokens`` in ``/now`` over time (§4.7).

``fresh`` within twice the source's poll interval, ``stale`` after that, ``unavailable`` with an
``error_kind`` when the last attempt failed, and no section before any data, with the reason in
``collectors[]``. The transitions run on a :class:`FakeClock`: a synthetic plugin (or the real
project collector over a fake process runner) emits the records, the daemon's real writer and
``/now`` serve them, and the clock moves.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from api_support import (
    HOME,
    T0,
    Dist,
    EntryPoint,
    Synthetic,
    activity_snapshot,
    client,
    make_daemon,
    no_hosts,
    session,
    session_snapshot,
)
from project_support import context

from fylgja import paths
from fylgja.api.now_usage import TOKENS_POLL_SECONDS
from fylgja.collectors.base import (
    COLLECTOR_API_MAJOR,
    Capability,
    Collector,
    CollectorContext,
    CollectorPlugin,
)
from fylgja.collectors.project.collector import ProjectCollector, ProjectSettings
from fylgja.collectors.project.usage import cost_argv, usage_argv
from fylgja.config import parse_config
from fylgja.core.daemon import Daemon
from fylgja.core.writer import Writer
from fylgja.models import (
    HealthState,
    SessionActivity,
    SnapshotItem,
    TokenCounts,
    UsagePayload,
    UsageSnapshot,
)
from fylgja.store import db
from fylgja.testing.fakes import FakeClock, FakeFS, FakeProcessRunner, RecordingSink

CODEXBAR = Path(__file__).resolve().parent.parent / "fixtures" / "usage" / "codexbar"
RESET = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)


def _snapshot(
    at: datetime, *payloads: UsagePayload, source: str = "ps", complete: bool = False
) -> UsageSnapshot:
    return UsageSnapshot(
        source=source,
        entity_id="usage",
        observed_at=at,
        complete=complete,
        items=tuple(
            SnapshotItem(entity_id=f"{p.provider}:{p.window or p.cost_period or 'x'}", payload=p)
            for p in payloads
        ),
    )


def _window(
    provider: str = "claude",
    window: str = "five_hour",
    used: float = 23.5,
    interval: float | None = 150.0,
    source: str = "statusline",
) -> UsagePayload:
    return UsagePayload(
        provider=provider,
        window=window,
        used_pct=used,
        resets_at=RESET,
        usage_source=source,
        poll_interval_s=interval,
    )


def _cost(provider: str, usd: float, interval: float = 900.0) -> UsagePayload:
    return UsagePayload(
        provider=provider,
        cost_usd=usd,
        cost_period="30d",
        usage_source="codexbar",
        poll_interval_s=interval,
    )


def _failure(provider: str, kind: str, *, cost: bool = False) -> UsagePayload:
    return UsagePayload(
        provider=provider,
        usage_source="codexbar",
        error_kind=kind,
        cost_period="30d" if cost else None,
        poll_interval_s=300.0,
    )


def _daemon(
    emit: Callable[[CollectorContext], None] | None = None, **kwargs: Any
) -> tuple[Daemon, Synthetic, FakeClock]:
    clock = FakeClock(T0)
    plugin = Synthetic("ps", frozenset({Capability.SESSIONS, Capability.PROJECT_STATE}), emit=emit)
    for name, value in kwargs.items():
        setattr(plugin, name, value)
    return make_daemon(plugin, clock=clock), plugin, clock


async def _now(daemon: Daemon) -> dict[str, Any]:
    async with client(daemon) as http:
        response = await http.get("/now")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _usage(daemon: Daemon) -> dict[str, Any] | None:
    usage: dict[str, Any] | None = (await _now(daemon)).get("usage")
    return usage


# -- usage -------------------------------------------------------------------------------------


async def test_a_reading_goes_fresh_then_stale_and_a_new_one_makes_it_fresh_again() -> None:
    daemon, plugin, clock = _daemon(lambda c: c.sink.emit(_snapshot(T0, _window())))
    await daemon.start()
    try:
        await daemon.settle()
        usage = await _usage(daemon)
        assert usage is not None
        assert usage["freshness"] == "fresh"
        assert usage["source"] == "statusline"
        assert usage["observed_at"].startswith("2026-10-05T12:00:00")
        assert "error_kind" not in usage
        assert "cost" not in usage
        [provider] = usage["providers"]
        assert (provider["provider"], provider["window"], provider["used_pct"]) == (
            "claude",
            "five_hour",
            23.5,
        )
        assert provider["resets_at"].startswith("2026-10-06T16:00:00")
        assert (provider["source"], provider["freshness"]) == ("statusline", "fresh")

        await clock.advance(300)  # exactly twice the interval is still fresh
        assert (await _usage(daemon) or {})["freshness"] == "fresh"
        await clock.advance(1)
        usage = await _usage(daemon)
        assert usage is not None
        assert (usage["freshness"], usage["providers"][0]["freshness"]) == ("stale", "stale")
        assert "error_kind" not in usage  # stale is not a failure

        # the same reading seen again: nothing changes in the store, but it is fresh again
        plugin.contexts[0].sink.emit(_snapshot(clock.now(), _window()))
        await daemon.settle()
        usage = await _usage(daemon)
        assert usage is not None
        assert usage["freshness"] == "fresh"
        assert usage["observed_at"].startswith("2026-10-05T12:05:01")
    finally:
        await daemon.stop()


async def test_the_poll_interval_of_the_source_sets_the_window() -> None:
    daemon, _, clock = _daemon(
        lambda c: c.sink.emit(_snapshot(T0, _window(interval=30.0), _window("codex", "seven_day")))
    )
    await daemon.start()
    try:
        await daemon.settle()
        await clock.advance(61)
        usage = await _usage(daemon)
        assert usage is not None
        by_provider = {p["provider"]: p["freshness"] for p in usage["providers"]}
        assert by_provider == {"claude": "stale", "codex": "fresh"}
        assert usage["freshness"] == "fresh"  # the section is as good as its best part
    finally:
        await daemon.stop()


async def test_a_reading_without_an_interval_uses_the_default() -> None:
    daemon, _, clock = _daemon(lambda c: c.sink.emit(_snapshot(T0, _window(interval=None))))
    await daemon.start()
    try:
        await daemon.settle()
        await clock.advance(600)
        assert (await _usage(daemon) or {})["freshness"] == "fresh"
        await clock.advance(1)
        assert (await _usage(daemon) or {})["freshness"] == "stale"
    finally:
        await daemon.stop()


async def test_a_failed_attempt_keeps_the_reading_and_marks_it_unavailable_until_it_recovers() -> (
    None
):
    daemon, plugin, clock = _daemon(
        lambda c: c.sink.emit(_snapshot(T0, _window("codex", "five_hour", 5.0, 300.0, "codexbar")))
    )
    await daemon.start()
    try:
        await daemon.settle()
        sink = plugin.contexts[0].sink
        await clock.advance(10)
        sink.emit(_snapshot(clock.now(), _failure("codex", "codexbar_timeout"), complete=True))
        await daemon.settle()
        usage = await _usage(daemon)
        assert usage is not None
        assert (usage["freshness"], usage["error_kind"]) == ("unavailable", "codexbar_timeout")
        [provider] = usage["providers"]
        assert provider["used_pct"] == 5.0  # the last reading is kept
        assert (provider["freshness"], provider["error_kind"]) == (
            "unavailable",
            "codexbar_timeout",
        )

        await clock.advance(10)
        sink.emit(
            _snapshot(
                clock.now(),
                _window("codex", "five_hour", 6.0, 300.0, "codexbar"),
                complete=True,
            )
        )
        await daemon.settle()
        usage = await _usage(daemon)
        assert usage is not None
        assert usage["freshness"] == "fresh"
        assert "error_kind" not in usage
        assert usage["providers"][0]["used_pct"] == 6.0
    finally:
        await daemon.stop()


async def test_a_provider_that_only_ever_failed_is_listed_with_its_reason() -> None:
    def emit(c: CollectorContext) -> None:
        c.sink.emit(
            _snapshot(
                T0,
                _window("codex", "five_hour", 5.0, 300.0, "codexbar"),
                _failure("copilot", "codexbar_no_strategy"),
                complete=True,
            )
        )

    daemon, _, _ = _daemon(emit)
    await daemon.start()
    try:
        await daemon.settle()
        usage = await _usage(daemon)
        assert usage is not None
        by_provider = {p["provider"]: p for p in usage["providers"]}
        assert set(by_provider) == {"codex", "copilot"}
        copilot = by_provider["copilot"]
        assert (copilot["freshness"], copilot["error_kind"]) == (
            "unavailable",
            "codexbar_no_strategy",
        )
        assert "used_pct" not in copilot
        assert "window" not in copilot
        assert by_provider["codex"]["freshness"] == "fresh"
        assert usage["freshness"] == "fresh"  # one provider answering is enough
        assert "error_kind" not in usage
    finally:
        await daemon.stop()


async def test_when_nothing_answers_the_section_is_unavailable_with_the_reason() -> None:
    def emit(c: CollectorContext) -> None:
        c.sink.emit(
            _snapshot(
                T0,
                _failure("codex", "codexbar_missing"),
                _failure("copilot", "codexbar_missing"),
                complete=True,
            )
        )

    daemon, _, _ = _daemon(emit)
    await daemon.start()
    try:
        await daemon.settle()
        usage = await _usage(daemon)
        assert usage is not None
        assert (usage["freshness"], usage["error_kind"]) == ("unavailable", "codexbar_missing")
        assert usage["source"] == "codexbar"
        assert [p["provider"] for p in usage["providers"]] == ["codex", "copilot"]
        assert "cost" not in usage
    finally:
        await daemon.stop()


async def test_the_cost_is_the_sum_over_the_providers_with_its_own_freshness() -> None:
    def emit(c: CollectorContext) -> None:
        c.sink.emit(_snapshot(T0, _cost("codex", 24.25), _cost("claude", 3000.5), complete=True))

    daemon, plugin, clock = _daemon(emit)
    await daemon.start()
    try:
        await daemon.settle()
        usage = await _usage(daemon)
        assert usage is not None
        cost = usage["cost"]
        assert cost["period"] == "30d"
        assert cost["usd"] == pytest.approx(3024.75)
        assert (cost["source"], cost["freshness"]) == ("codexbar", "fresh")
        assert usage["providers"] == []  # a cost row is not a quota window
        await clock.advance(1800)  # twice the cost interval
        assert (await _usage(daemon) or {})["cost"]["freshness"] == "fresh"
        await clock.advance(1)
        usage = await _usage(daemon)
        assert usage is not None
        assert (usage["cost"]["freshness"], usage["freshness"]) == ("stale", "stale")

        plugin.contexts[0].sink.emit(
            _snapshot(
                clock.now(),
                _cost("codex", 24.25),
                _failure("claude", "codexbar_failed", cost=True),
                complete=True,
            )
        )
        await daemon.settle()
        usage = await _usage(daemon)
        assert usage is not None
        assert usage["cost"]["usd"] == pytest.approx(3024.75)  # the failed provider keeps its row
        assert usage["cost"]["freshness"] == "fresh"  # the other provider answered
    finally:
        await daemon.stop()


async def test_a_cost_that_nobody_can_supply_is_unavailable() -> None:
    def emit(c: CollectorContext) -> None:
        c.sink.emit(_snapshot(T0, _cost("codex", 1.5), complete=True))

    daemon, plugin, clock = _daemon(emit)
    await daemon.start()
    try:
        await daemon.settle()
        await clock.advance(5)
        plugin.contexts[0].sink.emit(
            _snapshot(clock.now(), _failure("codex", "codexbar_timeout", cost=True), complete=True)
        )
        await daemon.settle()
        usage = await _usage(daemon)
        assert usage is not None
        assert usage["cost"]["usd"] == 1.5
        assert (usage["cost"]["freshness"], usage["cost"]["error_kind"]) == (
            "unavailable",
            "codexbar_timeout",
        )
        assert (usage["freshness"], usage["error_kind"]) == ("unavailable", "codexbar_timeout")
    finally:
        await daemon.stop()


async def test_no_data_means_no_usage_section() -> None:
    daemon, _, _ = _daemon()
    await daemon.start()
    try:
        await daemon.settle()
        assert await _usage(daemon) is None
        assert "usage" not in await _now(daemon)
    finally:
        await daemon.stop()


async def test_the_rows_of_a_degraded_collector_are_not_shown() -> None:
    daemon, plugin, clock = _daemon(lambda c: c.sink.emit(_snapshot(T0, _window())))
    await daemon.start()
    try:
        await daemon.settle()
        assert await _usage(daemon) is not None
        plugin.doctor_state = HealthState.DEGRADED
        await clock.advance(1.0)  # the supervisor's first doctor probe
        await daemon.settle()
        assert await _usage(daemon) is None
    finally:
        await daemon.stop()


async def test_rows_from_an_earlier_run_fall_back_to_their_stored_time() -> None:
    """After a restart the daemon remembers no observation; the row's own start has to do."""
    writer = Writer(paths.database_file(), clock=FakeClock(T0))
    await writer.start()
    try:
        writer.submit(_snapshot(T0, _window(), _cost("codex", 2.0)))
        await writer.flush()
    finally:
        await writer.stop()
    daemon, _, clock = _daemon()  # nothing is observed in this run
    await daemon.start()
    try:
        await daemon.settle()
        usage = await _usage(daemon)
        assert usage is not None
        assert usage["freshness"] == "fresh"
        assert usage["observed_at"].startswith("2026-10-05T12:00:00")
        assert usage["cost"]["usd"] == 2.0
        await clock.advance(601)  # the default interval is 300 s
        usage = await _usage(daemon)
        assert usage is not None
        assert usage["freshness"] == "stale"
    finally:
        await daemon.stop()


# -- the reason shows in collectors[] -----------------------------------------------------------


def _project_daemon(
    runner: FakeProcessRunner, tmp_path: Path, **settings: Any
) -> tuple[Daemon, FakeClock, list[ProjectCollector]]:
    clock = FakeClock(T0)
    made: list[ProjectCollector] = []

    def factory(config: Any, ctx: CollectorContext) -> Collector:
        assert isinstance(config, ProjectSettings)
        collector = ProjectCollector(
            config,
            ctx,
            database=tmp_path / "none.db",
            fs=FakeFS(),
            runner=runner,
        )
        made.append(collector)
        return collector

    plugin = CollectorPlugin(
        name="project",
        api_major=COLLECTOR_API_MAJOR,
        capabilities=frozenset({Capability.TODOS, Capability.PROJECT_STATE}),
        config_model=ProjectSettings,
        factory=factory,
    )
    config = parse_config(
        {"collectors": {"project": {"enabled": True, "codexbar_command": "cb", **settings}}},
        source="test-config.toml",
    )
    daemon = Daemon(
        config,
        entry_points=lambda: [EntryPoint("project", "x:plugin", Dist("fylgja"), plugin)],
        clock=clock,
        fs=FakeFS(),
        host_loader=no_hosts,
        home=HOME,
        resolve_debounce=0.0,
    )
    return daemon, clock, made


def _script_codexbar(runner: FakeProcessRunner) -> None:
    runner.script(
        "cb", "usage", "--provider", "codex", stdout=(CODEXBAR / "usage-codex.json").read_bytes()
    )
    runner.script(
        "cb",
        "usage",
        "--provider",
        "copilot",
        stdout=(CODEXBAR / "usage-copilot-no-strategy.json").read_bytes(),
        returncode=1,
    )
    runner.script(
        "cb", "cost", "--provider", "codex", stdout=(CODEXBAR / "cost-codex.json").read_bytes()
    )
    runner.script(
        "cb", "cost", "--provider", "claude", stdout=(CODEXBAR / "cost-claude.json").read_bytes()
    )


async def _settle_project(daemon: Daemon, clock: FakeClock) -> None:
    await daemon.settle()
    for _ in range(3):
        await clock.advance(1.0)
        await daemon.settle()


def _detail(now: dict[str, Any]) -> str:
    [info] = [c for c in now["collectors"] if c["name"] == "project"]
    return str(info.get("detail", ""))


async def test_codexbar_answers_fill_usage_through_the_real_project_collector(
    tmp_path: Path,
) -> None:
    runner = FakeProcessRunner()
    _script_codexbar(runner)
    daemon, clock, _ = _project_daemon(runner, tmp_path)
    await daemon.start()
    try:
        await _settle_project(daemon, clock)
        now = await _now(daemon)
        usage = now["usage"]
        providers = {(p["provider"], p.get("window")): p for p in usage["providers"]}
        assert set(providers) == {("codex", "five_hour"), ("codex", "seven_day"), ("copilot", None)}
        assert providers[("codex", "seven_day")]["used_pct"] == 3
        assert providers[("copilot", None)]["error_kind"] == "codexbar_no_strategy"
        assert usage["cost"]["usd"] == pytest.approx(24.276115 + 3039.778643)
        assert usage["freshness"] == "fresh"
        assert now["degraded"] == []  # CodexBar failing for a provider does not degrade anything
        assert "CodexBar unavailable (codexbar_no_strategy)" in _detail(now)
        assert runner.calls[0] == usage_argv("cb", "codex")
        assert cost_argv("cb", "claude") in runner.calls
    finally:
        await daemon.stop()


async def test_a_missing_codexbar_is_unavailable_with_the_reason_and_not_degraded(
    tmp_path: Path,
) -> None:
    daemon, clock, _ = _project_daemon(FakeProcessRunner(), tmp_path)
    await daemon.start()
    try:
        await _settle_project(daemon, clock)
        now = await _now(daemon)
        usage = now["usage"]
        assert (usage["freshness"], usage["error_kind"]) == ("unavailable", "codexbar_missing")
        assert "cost" not in usage
        assert now["degraded"] == []
        assert "CodexBar unavailable (codexbar_missing)" in _detail(now)
    finally:
        await daemon.stop()


async def test_disabled_usage_is_absent_and_the_collector_says_why(tmp_path: Path) -> None:
    runner = FakeProcessRunner()
    _script_codexbar(runner)
    daemon, clock, _ = _project_daemon(runner, tmp_path, usage_enabled=False)
    await daemon.start()
    try:
        await _settle_project(daemon, clock)
        now = await _now(daemon)
        assert "usage" not in now
        assert "usage: disabled" in _detail(now)
        assert runner.calls == []
    finally:
        await daemon.stop()


async def test_before_the_first_ask_usage_is_absent_and_the_reason_is_in_the_detail(
    tmp_path: Path,
) -> None:
    clock = FakeClock(T0)
    collector = ProjectCollector(
        ProjectSettings(codexbar_command="cb"),
        context(clock, RecordingSink()),
        database=tmp_path / "none.db",
        fs=FakeFS(),
        runner=FakeProcessRunner(),
    )
    report = await collector.doctor()
    assert report.state is HealthState.OK
    assert "usage: no data yet (CodexBar not asked yet)" in (report.detail or "")


# -- tokens ------------------------------------------------------------------------------------

KEY = "claude:s1"


def _agent_records(tokens: TokenCounts | None, at: datetime, **readings: Any) -> list[Any]:
    return [
        session_snapshot("ps", [session("claude", "s1", pid=500, cwd="/work/a", host_pid=100)], at),
        activity_snapshot(
            "ps", {KEY: SessionActivity(state="idle", tokens=tokens, **readings)}, at
        ),
    ]


def _count(sql: str) -> int:
    connection = db.connect_reader(paths.database_file())
    try:
        return int(connection.execute(sql).fetchone()[0])
    finally:
        connection.close()


async def test_tokens_cost_and_context_fill_the_session_and_age_with_it() -> None:
    tokens = TokenCounts(input=10, output=20, cache_read=300, cache_write=40)

    def emit(c: CollectorContext) -> None:
        for record in _agent_records(tokens, T0, cost_usd=1.25, context_pct=42.5):
            c.sink.emit(record)

    daemon, _, clock = _daemon(emit)
    await daemon.start()
    try:
        await daemon.settle()
        [found] = (await _now(daemon))["sessions"]
        assert found["tokens"] == {
            "input": 10,
            "output": 20,
            "cache_read": 300,
            "cache_write": 40,
            "source": "transcript",
            "observed_at": found["tokens"]["observed_at"],
            "freshness": "fresh",
        }
        assert found["tokens"]["observed_at"].startswith("2026-10-05T12:00:00")
        assert (found["cost_usd"], found["context_pct"]) == (1.25, 42.5)

        await clock.advance(2 * TOKENS_POLL_SECONDS)
        assert (await _now(daemon))["sessions"][0]["tokens"]["freshness"] == "fresh"
        await clock.advance(1)
        found = (await _now(daemon))["sessions"][0]
        assert found["tokens"]["freshness"] == "stale"
        assert found["cost_usd"] == 1.25  # a reading has no freshness of its own
    finally:
        await daemon.stop()


async def test_a_session_without_readings_has_no_tokens_cost_or_context() -> None:
    def emit(c: CollectorContext) -> None:
        for record in _agent_records(None, T0):
            c.sink.emit(record)

    daemon, _, _ = _daemon(emit)
    await daemon.start()
    try:
        await daemon.settle()
        [found] = (await _now(daemon))["sessions"]
        assert not {"tokens", "cost_usd", "context_pct"} & set(found)
    finally:
        await daemon.stop()


async def test_readings_update_the_open_interval_in_place_and_survive_a_new_one() -> None:
    first = TokenCounts(input=1, output=2, cache_read=3, cache_write=4)

    def emit(c: CollectorContext) -> None:
        for record in _agent_records(first, T0, cost_usd=0.5):
            c.sink.emit(record)

    daemon, plugin, clock = _daemon(emit)
    await daemon.start()
    try:
        await daemon.settle()
        sink = plugin.contexts[0].sink
        await clock.advance(5)
        grown = TokenCounts(input=11, output=22, cache_read=33, cache_write=44)
        for record in _agent_records(grown, clock.now(), cost_usd=0.75, context_pct=10.0):
            sink.emit(record)
        await daemon.settle()
        assert _count("SELECT count(*) FROM session_snapshots") == 1  # no new interval
        [found] = (await _now(daemon))["sessions"]
        assert found["tokens"]["input"] == 11
        assert (found["cost_usd"], found["context_pct"]) == (0.75, 10.0)

        # a state change opens a new interval, which carries the readings over
        await clock.advance(5)
        for record in _agent_records(None, clock.now()):
            sink.emit(record)
        sink.emit(activity_snapshot("ps", {KEY: SessionActivity(state="busy")}, clock.now()))
        await daemon.settle()
        assert _count("SELECT count(*) FROM session_snapshots") == 2
        [found] = (await _now(daemon))["sessions"]
        assert found["state"] == "busy"
        assert found["tokens"]["output"] == 22
        assert (found["cost_usd"], found["context_pct"]) == (0.75, 10.0)
    finally:
        await daemon.stop()


async def test_an_activity_record_of_an_ended_session_stores_no_readings() -> None:
    """An activity record of a session that already ended stores nothing, readings included."""
    tokens = TokenCounts(input=1, output=1, cache_read=1, cache_write=1)

    def emit(c: CollectorContext) -> None:
        for record in _agent_records(tokens, T0):
            c.sink.emit(record)

    daemon, plugin, clock = _daemon(emit)
    await daemon.start()
    try:
        await daemon.settle()
        sink = plugin.contexts[0].sink
        await clock.advance(5)
        sink.emit(session_snapshot("ps", [], clock.now()))  # the session ended
        await daemon.settle()
        assert not (await _now(daemon)).get("sessions")
        sink.emit(
            activity_snapshot(
                "ps",
                {
                    KEY: SessionActivity(
                        state="idle",
                        tokens=TokenCounts(input=9, output=9, cache_read=9, cache_write=9),
                    )
                },
                clock.now() + timedelta(seconds=1),
            )
        )
        await daemon.settle()
        assert _count("SELECT count(*) FROM session_snapshots WHERE tokens_input = 9") == 0
    finally:
        await daemon.stop()
