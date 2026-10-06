"""Freshness of the project sections in ``/now`` and the collector's behaviour over time (§4.7).

``fresh`` within twice the poll interval, ``stale`` after that, ``unavailable`` with an
``error_kind`` when the last attempt failed, and no section at all before any data. The
transitions run on a :class:`FakeClock`: a synthetic plugin emits the records the project
collector builds, the daemon's real writer and ``/now`` serve them, and the clock moves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api_support import T0, Synthetic, client, make_daemon, session, session_snapshot
from project_support import (
    GH,
    api_response,
    context,
    script_git,
    script_pr,
    script_runs,
    store_with_activity,
)

from fylgja.collectors.base import Capability, CollectorContext
from fylgja.collectors.project import builds
from fylgja.collectors.project.collector import ProjectCollector, ProjectSettings
from fylgja.core.daemon import Daemon
from fylgja.core.freshness import freshness
from fylgja.models import (
    BuildEvent,
    HealthState,
    ProjectEvent,
    ProjectPayload,
)
from fylgja.testing.fakes import FakeClock, FakeFS, FakeProcessRunner, RecordingSink

AT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
APP = "/code/app"

# -- the rule ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "error", "expected"),
    [
        (0, None, "fresh"),
        (299, None, "fresh"),
        (600, None, "fresh"),  # exactly twice the interval is still fresh
        (601, None, "stale"),
        (86400, None, "stale"),
        (-30, None, "fresh"),  # an observation from the future (clock skew)
        (0, "gh_timeout", "unavailable"),
        (5000, "gh_not_authenticated", "unavailable"),
    ],
)
def test_the_freshness_rule(age: float, error: str | None, expected: str) -> None:
    assert freshness(AT - timedelta(seconds=age), AT, 300.0, error) == expected


def test_the_interval_scales_the_window() -> None:
    observed = AT - timedelta(seconds=100)
    assert freshness(observed, AT, 30.0) == "stale"
    assert freshness(observed, AT, 60.0) == "fresh"
    assert freshness(observed, AT, 60.0, "") == "fresh"  # an empty error kind is no error


# -- /now -------------------------------------------------------------------------------------


def _fold(name: str = "actions-fail-multi.json") -> builds.BuildFold:
    return builds.fold_runs_body((GH / name).read_bytes(), "example/repo")


def _daemon(emit: Any = None) -> tuple[Daemon, Synthetic, FakeClock]:
    clock = FakeClock(T0)

    def start(context: CollectorContext) -> None:
        context.sink.emit(
            session_snapshot("ps", [session("claude", "s1", pid=500, cwd=APP, host_pid=100)], T0)
        )
        if emit is not None:
            emit(context)

    plugin = Synthetic("ps", frozenset({Capability.SESSIONS, Capability.PROJECT_STATE}), emit=start)
    return make_daemon(plugin, clock=clock), plugin, clock


def _project_event(at: datetime, **fields: Any) -> ProjectEvent:
    return ProjectEvent(
        source="ps", entity_id=APP, observed_at=at, payload=ProjectPayload(path=APP, **fields)
    )


def _build_event(
    at: datetime, error_kind: str | None = None, interval: float | None = 300.0
) -> BuildEvent:
    return BuildEvent(
        source="ps",
        entity_id=APP,
        observed_at=at,
        payload=_fold().to_payload(APP, error_kind=error_kind, poll_interval_s=interval),
    )


async def _project(daemon: Daemon) -> dict[str, Any]:
    async with client(daemon) as http:
        response = await http.get("/now")
    assert response.status_code == 200, response.text
    projects = {p["path"]: p for p in response.json()["projects"]}
    return projects[APP]  # type: ignore[no-any-return]


async def test_the_git_state_fills_the_existing_project_fields() -> None:
    def emit(context: CollectorContext) -> None:
        context.sink.emit(
            _project_event(
                T0,
                repo_root=APP,
                branch="feature/x",
                dirty=True,
                ahead=2,
                behind=1,
                last_commit_at=T0 - timedelta(hours=3),
                pr_number=1268,
                pr_state="OPEN",
                review_decision="REVIEW_REQUIRED",
            )
        )

    daemon, _, _ = _daemon(emit)
    await daemon.start()
    try:
        await daemon.settle()
        project = await _project(daemon)
    finally:
        await daemon.stop()
    assert (project["branch"], project["dirty"], project["ahead"], project["behind"]) == (
        "feature/x",
        True,
        2,
        1,
    )
    assert project["last_commit_at"].startswith("2026-10-05T09:00:00")
    assert project["pr"] == {"number": 1268, "state": "OPEN", "review_decision": "REVIEW_REQUIRED"}
    assert "build" not in project  # no data yet: the section is absent


async def test_a_branch_without_a_pr_has_no_pr_and_a_cleared_review_decision() -> None:
    def emit(context: CollectorContext) -> None:
        context.sink.emit(_project_event(T0, branch="a", dirty=False, pr_number=7, pr_state="OPEN"))
        context.sink.emit(
            _project_event(
                T0 + timedelta(seconds=1),
                branch="b",
                dirty=False,
                pr_number=0,
                pr_state="none",
                review_decision="",
            )
        )

    daemon, _, _ = _daemon(emit)
    await daemon.start()
    try:
        await daemon.settle()
        project = await _project(daemon)
    finally:
        await daemon.stop()
    assert project["branch"] == "b"
    assert "pr" not in project


async def test_a_git_observation_clears_what_it_does_not_have() -> None:
    def emit(context: CollectorContext) -> None:
        context.sink.emit(_project_event(T0, branch="main", dirty=False, ahead=3, behind=1))
        # a detached HEAD without an upstream: no branch, no ahead or behind
        context.sink.emit(_project_event(T0 + timedelta(seconds=1), dirty=True))

    daemon, _, _ = _daemon(emit)
    await daemon.start()
    try:
        await daemon.settle()
        project = await _project(daemon)
    finally:
        await daemon.stop()
    assert project["dirty"] is True
    assert not {"branch", "ahead", "behind"} & set(project)


async def test_the_build_goes_fresh_then_stale_then_unavailable_then_fresh_again() -> None:
    daemon, plugin, clock = _daemon(lambda context: context.sink.emit(_build_event(T0)))
    await daemon.start()
    try:
        await daemon.settle()
        build = (await _project(daemon))["build"]
        assert (build["freshness"], build["state"], build["source"]) == ("fresh", "fail", "gh")
        assert build["head_sha"] == "bbb222"
        assert build["url"] == "https://github.com/example/repo/commit/bbb222/checks"
        assert [w["workflow"] for w in build["workflows"]] == ["ci.yml", "lint.yml"]
        assert "error_kind" not in build

        await clock.advance(600)
        assert (await _project(daemon))["build"]["freshness"] == "fresh"  # twice the interval
        await clock.advance(1)
        assert (await _project(daemon))["build"]["freshness"] == "stale"

        sink = plugin.contexts[0].sink  # the next attempt fails
        sink.emit(_build_event(clock.now(), error_kind="gh_not_authenticated"))
        await daemon.settle()
        build = (await _project(daemon))["build"]
        assert (build["freshness"], build["error_kind"]) == ("unavailable", "gh_not_authenticated")
        assert build["state"] == "fail"  # the last fold is kept

        sink.emit(_build_event(clock.now() + timedelta(seconds=1)))
        await daemon.settle()
        assert (await _project(daemon))["build"]["freshness"] == "fresh"
    finally:
        await daemon.stop()


async def test_the_poll_interval_stored_with_a_build_sets_its_window() -> None:
    daemon, _, clock = _daemon(lambda context: context.sink.emit(_build_event(T0, interval=30.0)))
    await daemon.start()
    try:
        await daemon.settle()
        await clock.advance(61)
        assert (await _project(daemon))["build"]["freshness"] == "stale"
    finally:
        await daemon.stop()


async def test_a_build_without_an_interval_falls_back_to_the_default_poll() -> None:
    daemon, _, clock = _daemon(lambda context: context.sink.emit(_build_event(T0, interval=None)))
    await daemon.start()
    try:
        await daemon.settle()
        await clock.advance(599)
        assert (await _project(daemon))["build"]["freshness"] == "fresh"
        await clock.advance(2)
        assert (await _project(daemon))["build"]["freshness"] == "stale"
    finally:
        await daemon.stop()


# -- the collector over time -------------------------------------------------------------------


def _collector(
    tmp_path: Any, clock: FakeClock, runner: FakeProcessRunner, sink: RecordingSink, **settings: Any
) -> ProjectCollector:
    store = store_with_activity(tmp_path / "fylgja.db", clock.now(), projects=[APP], live=[APP])
    fs = FakeFS()
    fs.add_dir(APP)
    return ProjectCollector(
        ProjectSettings(database=store, **settings),
        context(clock, sink),
        database=store,
        fs=fs,
        runner=runner,
    )


async def test_the_collector_emits_project_and_build_records_and_keeps_observing(
    tmp_path: Any,
) -> None:
    clock, runner, sink = FakeClock(AT), FakeProcessRunner(), RecordingSink()
    script_git(runner, APP)
    script_pr(runner, name="pr-pending-checks.json")
    script_runs(runner, stdout=api_response("api-200.txt"))
    collector = _collector(tmp_path, clock, runner, sink)
    assert await collector.poll_state() == 3  # git, then the PR on top of it, then the build
    kinds = [type(r).__name__ for r in sink.records]
    assert kinds == ["ProjectEvent", "ProjectEvent", "BuildEvent"]
    first_build = sink.of_kind(sink.records[-1].entity_kind)[0]
    await clock.advance(301)
    runner._scripts = [s for s in runner._scripts if s.prefix[1] != "api"]
    script_runs(
        runner,
        etag='W/"0123456789abcdef0123456789abcdef"',
        stdout=api_response("api-304.txt"),
        returncode=1,
    )
    await collector.poll_state()
    builds_seen = [r for r in sink.records if isinstance(r, BuildEvent)]
    assert len(builds_seen) == 2
    assert builds_seen[1].observed_at - first_build.observed_at == timedelta(seconds=301)
    assert collector.state_emissions == 4  # an unchanged project is not emitted again


async def test_the_collector_runs_both_loops_and_stops_cleanly(tmp_path: Any) -> None:
    import asyncio

    clock, runner, sink = FakeClock(AT), FakeProcessRunner(), RecordingSink()
    script_git(runner, APP)
    script_pr(runner)
    script_runs(runner, stdout=api_response("api-200.txt"))
    collector = _collector(tmp_path, clock, runner, sink)
    task = asyncio.create_task(collector.run())
    for _ in range(12):
        await clock.advance(5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(isinstance(r, BuildEvent) for r in sink.records)
    assert runner.active == 0


async def test_doctor_follows_git_and_gh(tmp_path: Any) -> None:
    clock, runner, sink = FakeClock(AT), FakeProcessRunner(), RecordingSink()
    collector = _collector(tmp_path, clock, runner, sink)
    assert (await collector.doctor()).detail == "not polled yet"
    collector.poll()
    await collector.poll_state()  # nothing is scripted: git is missing
    report = await collector.doctor()
    assert (report.state, report.error_kind) == (HealthState.DEGRADED, "git_missing")
    assert "1 of 1" in (report.detail or "")
    runner.script("gh", returncode=4, stderr="gh auth login")
    script_git(runner, APP)
    await clock.advance(301)
    await collector.poll_state()
    report = await collector.doctor()
    assert report.state is HealthState.OK  # gh being unavailable does not degrade the collector
    assert "gh unavailable (gh_not_authenticated)" in (report.detail or "")


async def test_an_unreadable_store_emits_nothing_and_the_error_loop_is_survived(
    tmp_path: Any,
) -> None:
    clock, runner, sink = FakeClock(AT), FakeProcessRunner(), RecordingSink()
    collector = _collector(tmp_path, clock, runner, sink)
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a database" * 64)
    collector._database = broken
    assert await collector.poll_state() == 0
    assert sink.records == []


async def test_a_crash_in_the_state_loop_is_reported_and_survived(tmp_path: Any) -> None:
    import asyncio

    clock, runner, sink = FakeClock(AT), FakeProcessRunner(), RecordingSink()
    collector = _collector(tmp_path, clock, runner, sink)

    async def boom(*args: Any, **kwargs: Any) -> list[Any]:
        raise RuntimeError("boom")

    collector._engine.poll = boom  # type: ignore[method-assign]
    task = asyncio.create_task(collector.run())
    await clock.advance(5)
    report = await collector.doctor()
    assert (report.state, report.error_kind) == (HealthState.DEGRADED, "poll_failed")
    assert "project state poll failed" in (report.detail or "")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
