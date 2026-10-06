"""The ``gh`` call budget and the rules around it (WP5.4, §4.7): never more than the limit in any
sixty minutes, ETag reuse on a 304, only active projects polled, and a budget refusal that simply
lets the section age. Time is a :class:`FakeClock`; no ``gh`` runs."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from project_support import (
    GH,
    TimedRunner,
    activity,
    api_response,
    engine,
    events,
    fs_with,
    git_fixture,
    projects,
    script_git,
    step,
    store_with_activity,
)

from fylgja.collectors.project import adapter, ghparse, gitstate
from fylgja.collectors.project.state import CallBudget, ProjectStateEngine
from fylgja.testing.fakes import FakeClock

HOUR = 3600.0
ETAG = 'W/"0123456789abcdef0123456789abcdef"'


def _repo(name: str) -> ghparse.GitHubRepo:
    return ghparse.GitHubRepo("example", name)


def _arrange(runner: TimedRunner, names: list[str]) -> None:
    """Every project ``/code/<name>`` has its own GitHub repository ``example/<name>``."""
    for name in names:
        root = f"/code/{name}"
        script_git(runner, root, remote=f"https://github.com/example/{name}.git")
        repo = _repo(name)
        runner.script(
            *ghparse.pr_argv("gh", repo, "main"), stdout=(GH / "pr-open-checks.json").read_text()
        )
        runner.script(*ghparse.actions_argv("gh", repo, "main"), stdout=api_response("api-200.txt"))
        runner.script(
            *ghparse.actions_argv("gh", repo, "main", ETAG),
            stdout=api_response("api-304.txt"),
            returncode=1,
        )


def _world(
    names: list[str], **settings: object
) -> tuple[FakeClock, TimedRunner, ProjectStateEngine]:
    clock = FakeClock()
    runner = TimedRunner(clock)
    _arrange(runner, names)
    state = engine(clock, runner, fs_with(*[f"/code/{n}" for n in names]), **settings)
    return clock, runner, state


def _worst_window(times: list[float], window: float = HOUR) -> int:
    """The most calls that fall in any ``window`` seconds."""
    worst = 0
    for index, start in enumerate(times):
        worst = max(worst, sum(1 for t in times[index:] if t - start < window))
    return worst


# -- the budget itself ------------------------------------------------------------------------


def test_the_budget_is_a_rolling_window() -> None:
    budget = CallBudget(3)
    assert [budget.try_acquire(t) for t in (0, 10, 20, 30)] == [True, True, True, False]
    assert budget.used(30) == 3
    assert not budget.try_acquire(3599)  # the first call is still inside the hour
    assert budget.try_acquire(3600)  # ... and it has left at 3600 s
    assert budget.used(3600) == 3


def test_a_budget_of_zero_never_allows_a_call() -> None:
    budget = CallBudget(0)
    assert not budget.try_acquire(0)
    assert budget.used(0) == 0


# -- never more than the limit over a simulated day ------------------------------------------


async def test_the_runs_budget_is_never_exceeded_over_24_hours_with_many_projects() -> None:
    names = [f"app{i}" for i in range(10)]
    clock, runner, state = _world(names)
    known = projects(*names)
    live = activity(live=[p.path for p in known])
    for _ in range(24 * 60):  # a tick every minute for a day
        await state.poll(known, live)
        step(clock, 60)
    runs = runner.calls_of("api")
    prs = runner.calls_of("pr")
    assert _worst_window(runs) <= 30  # gh_calls_per_hour
    assert _worst_window(prs) <= 120  # gh_pr_calls_per_hour
    # the budget is used, not starved: 10 live projects want far more than 30 an hour
    assert len([t for t in runs if t < HOUR]) == 30
    assert len(runs) >= 24 * 30 - 30
    # and git, which is local, still ran for every project on its own timer
    assert len(runner.git_calls("status")) >= 10 * (24 * 12 - 1)


async def test_a_smaller_budget_is_respected_too() -> None:
    clock, runner, state = _world(["app", "other"], gh_calls_per_hour=4)
    known = projects("app", "other")
    for _ in range(6 * 120):
        await state.poll(known, activity(live=[p.path for p in known]))
        step(clock, 30)
    assert _worst_window(runner.calls_of("api")) <= 4
    assert len(runner.calls_of("api")) >= 6 * 4 - 4


async def test_a_budget_refusal_is_not_an_attempt_the_section_just_ages() -> None:
    clock, runner, state = _world(["app", "other"], gh_calls_per_hour=1)
    known = projects("app", "other")
    first = await state.poll(known, activity(live=[p.path for p in known]))
    builds = events(first)[1]
    assert [b.entity_id for b in builds] == ["/code/app"]  # the other found the budget spent
    assert all(b.payload.error_kind is None for b in builds)
    assert len(runner.calls_of("api")) == 1
    await clock.advance(1800)
    again = await state.poll(known, activity(live=[p.path for p in known]))
    assert events(again)[1] == []  # still no budget, still no record, and no error either


# -- ETag -------------------------------------------------------------------------------------


async def test_a_304_reuses_the_last_result_and_the_next_call_sends_the_etag() -> None:
    clock, runner, state = _world(["app"])
    known = projects("app")
    live = activity(live=["/code/app"])
    first = events(await state.poll(known, live))[1]
    assert [b.payload.state for b in first] == ["pass"]
    api = [argv for _, argv in runner.times if argv[1] == "api"]
    assert api == [ghparse.actions_argv("gh", _repo("app"), "main")]  # no If-None-Match yet
    await clock.advance(301)
    second = events(await state.poll(known, live))[1]
    api = [argv for _, argv in runner.times if argv[1] == "api"]
    assert api[-1] == ghparse.actions_argv("gh", _repo("app"), "main", ETAG)
    # the fold was reused as it was, and the section was observed again
    assert second[0].payload.workflows == first[0].payload.workflows
    assert second[0].payload.head_sha == first[0].payload.head_sha == "aaa111"
    assert second[0].observed_at > first[0].observed_at
    assert second[0].payload.error_kind is None


async def test_a_200_after_a_304_replaces_the_fold() -> None:
    clock, runner, state = _world(["app"])
    known, live = projects("app"), activity(live=["/code/app"])
    await state.poll(known, live)
    runner._scripts = [s for s in runner._scripts if "If-None-Match" not in " ".join(s.prefix)]
    runner.script(
        *ghparse.actions_argv("gh", _repo("app"), "main", ETAG),
        stdout=api_response("api-200.txt").replace('"success"', '"failure"'),
    )
    await clock.advance(301)
    build = events(await state.poll(known, live))[1][0]
    assert build.payload.state == "fail"


async def test_a_branch_change_forgets_the_etag() -> None:
    clock, runner, state = _world(["app"])
    known, live = projects("app"), activity(live=["/code/app"])
    await state.poll(known, live)
    runner._scripts.clear()
    script_git(
        runner,
        "/code/app",
        status="status-dirty-ahead-behind.txt",
        remote="https://github.com/example/app.git",
    )
    runner.script("gh", stdout="no pull requests found", returncode=1)
    runner.script(
        *ghparse.actions_argv("gh", _repo("app"), "feature/x"), stdout=api_response("api-200.txt")
    )
    await clock.advance(301)
    await state.poll(known, live)
    last = [argv for _, argv in runner.times if argv[1] == "api"][-1]
    assert last == ghparse.actions_argv("gh", _repo("app"), "feature/x")


# -- only active projects ---------------------------------------------------------------------


async def test_only_projects_with_a_live_session_or_recent_focus_are_polled_for_builds() -> None:
    names = ["live", "focused", "recent", "idle"]
    clock, runner, state = _world(names)
    known = projects(*names)
    seen = activity(
        live=["/code/live"], focused="/code/focused", recent=["/code/recent"], clock=clock
    )
    records = await state.poll(known, seen)
    built = {b.entity_id for b in events(records)[1]}
    assert built == {"/code/live", "/code/focused", "/code/recent"}
    pr_repos = {argv[5] for _, argv in runner.times if argv[1:3] == ("pr", "view")}
    assert pr_repos == {"example/live", "example/focused", "example/recent"}
    api_repos = {argv[3].split("/")[2] for _, argv in runner.times if argv[1] == "api"}
    assert api_repos == {"live", "focused", "recent"}
    # git is for every known project
    assert len(runner.git_calls("status")) == 4


async def test_the_most_active_project_gets_the_budget_first() -> None:
    names = ["first", "second", "third"]
    clock, runner, state = _world(names, gh_calls_per_hour=1)
    seen = activity(
        live=["/code/second"], focused="/code/third", recent=["/code/first"], clock=clock
    )
    records = await state.poll(projects(*names), seen)
    assert [b.entity_id for b in events(records)[1]] == ["/code/third"]  # the focused one


async def test_a_project_stops_being_polled_when_its_activity_leaves_the_window() -> None:
    clock, runner, state = _world(["app"])
    known = projects("app")
    await state.poll(known, activity(live=["/code/app"]))
    assert len(runner.calls_of("api")) == 1
    await clock.advance(301)
    await state.poll(known, activity())  # no session, no recent focus any more
    assert len(runner.calls_of("api")) == 1


def test_the_activity_window_is_read_from_the_store(tmp_path: Path) -> None:
    clock = FakeClock()
    now = clock.now()
    store = store_with_activity(
        tmp_path / "fylgja.db",
        now,
        projects=["/code/a", "/code/b", "/code/c", "/code/d", "/code/e"],
        live=["/code/a"],
        spans=[
            ("/code/b", 1800, 1700),  # focused 30 min ago
            ("/code/c", 7200, 7100),  # two hours ago: outside the hour
            ("/code/d", 100, None),  # focused now
            ("/code/b", 600, 500),  # b again, more recently
        ],
    )
    found = adapter.read_activity(store, now, 3600)
    assert found.live == frozenset({"/code/a"})
    assert set(found.last_focus) == {"/code/b", "/code/d"}
    assert found.focused == "/code/d"
    assert found.recency("/code/b") == now - timedelta(seconds=500)
    assert found.recency("/code/d") == now
    assert found.recency("/code/c") is None
    assert [found.active(p) for p in ("/code/a", "/code/b", "/code/c", "/code/e")] == [
        True,
        True,
        False,
        False,
    ]


def test_a_missing_store_has_no_activity_and_a_broken_one_is_an_error(tmp_path: Path) -> None:
    now = FakeClock().now()
    assert adapter.read_activity(tmp_path / "none.db", now, 3600) == adapter.ProjectActivity()
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a database" * 64)
    with pytest.raises(adapter.StoreUnreadable):
        adapter.read_activity(broken, now, 3600)


# -- sooner on focus --------------------------------------------------------------------------


async def test_focus_moving_into_a_project_brings_its_build_check_forward() -> None:
    clock, runner, state = _world(["app"], focus_refresh_seconds=30)
    known = projects("app")
    await state.poll(known, activity(live=["/code/app"]))
    assert runner.calls_of("api") == [0.0]
    await clock.advance(10)
    await state.poll(known, activity(live=["/code/app"], focused="/code/app", clock=clock))
    assert runner.calls_of("api") == [0.0]  # inside the refresh gap
    await clock.advance(21)
    await state.poll(known, activity(live=["/code/app"], focused="/code/app", clock=clock))
    assert runner.calls_of("api") == [0.0, 31.0]  # 31 s, not 300 s


async def test_a_moved_branch_tip_brings_the_build_check_forward() -> None:
    clock, runner, state = _world(["app"], focus_refresh_seconds=30)
    known, live = projects("app"), activity(live=["/code/app"])
    await state.poll(known, live)
    runner._scripts = [s for s in runner._scripts if s.prefix[1] != "--no-optional-locks"]
    moved = git_fixture("status-clean.txt").replace("1" * 40, "9" * 40)
    runner.script(*gitstate.status_argv("/code/app"), stdout=moved)
    runner.script(*gitstate.commit_time_argv("/code/app"), stdout="1790000100\n")
    runner.script(*gitstate.remote_argv("/code/app"), stdout="https://github.com/example/app.git\n")
    await clock.advance(100)
    # focus arrives: git looks again (nothing else would for 200 s) and sees the moved tip
    await state.poll(known, activity(live=["/code/app"], focused="/code/app", clock=clock))
    assert runner.calls_of("api") == [0.0, 100.0]


# -- rate limit and failures -------------------------------------------------------------------


async def test_few_calls_remaining_blocks_gh_for_an_hour_and_the_build_says_so() -> None:
    clock, runner, state = _world(["app"])
    runner._scripts = [s for s in runner._scripts if s.prefix[1] != "api"]
    runner.script(
        *ghparse.actions_argv("gh", _repo("app"), "main"),
        stdout=api_response("api-200.txt").replace("4823", "40"),
    )
    known, live = projects("app"), activity(live=["/code/app"])
    first = events(await state.poll(known, live))[1]
    assert first[0].payload.error_kind is None  # this answer was fine
    assert state.gh_issue == "gh_rate_limited"
    await clock.advance(301)
    second = events(await state.poll(known, live))[1]
    assert second[0].payload.error_kind == "gh_rate_limited"
    assert len(runner.calls_of("api")) == 1  # nothing was asked while blocked
    await clock.advance(3600)
    assert state.gh_issue is None


async def test_a_failed_runs_call_keeps_the_last_fold_with_an_error_kind_and_backs_off() -> None:
    clock, runner, state = _world(["app"])
    known, live = projects("app"), activity(live=["/code/app"])
    await state.poll(known, live)
    runner._scripts = [s for s in runner._scripts if s.prefix[1] != "api"]
    runner.script(
        *ghparse.actions_argv("gh", _repo("app"), "main", ETAG),
        stdout="HTTP/2.0 503 Service Unavailable\n\n{}",
        returncode=1,
    )
    kinds: list[tuple[float, str | None, str]] = []
    for _ in range(30):
        await clock.advance(60)
        for record in events(await state.poll(known, live))[1]:
            kinds.append((clock.monotonic(), record.payload.error_kind, record.payload.state))
    assert {kind for _, kind, _ in kinds} == {"gh_server_error"}
    assert {state_ for _, _, state_ in kinds} == {"pass"}  # the last fold stays
    gaps = [b[0] - a[0] for a, b in zip(kinds, kinds[1:], strict=False)]
    assert gaps[:2] == [300.0, 600.0]  # backed off: 300 s after the first failure, then doubled


async def test_a_response_that_cannot_be_folded_is_a_build_parse_error() -> None:
    clock, runner, state = _world(["app"])
    runner._scripts = [s for s in runner._scripts if s.prefix[1] != "api"]
    runner.script(
        *ghparse.actions_argv("gh", _repo("app"), "main"), stdout="HTTP/2 200 OK\n\nnot json"
    )
    records = await state.poll(projects("app"), activity(live=["/code/app"]))
    assert events(records)[1][0].payload.error_kind == "build_parse"


async def test_a_gh_that_times_out_or_cannot_start_is_a_short_code() -> None:
    for script, kind in (
        ({"timeout": True}, "gh_timeout"),
        ({"error": PermissionError("x")}, "gh_failed"),
    ):
        clock, runner, state = _world(["app"])
        runner._scripts = [s for s in runner._scripts if s.prefix[1] != "api"]
        runner.script("gh", "api", **script)  # type: ignore[arg-type]
        records = await state.poll(projects("app"), activity(live=["/code/app"]))
        assert events(records)[1][0].payload.error_kind == kind


async def test_a_304_without_a_known_fold_is_a_failure_not_a_reuse() -> None:
    clock, runner, state = _world(["app"])
    runner._scripts = [s for s in runner._scripts if s.prefix[1] != "api"]
    runner.script("gh", "api", stdout=api_response("api-304.txt"), returncode=1)
    records = await state.poll(projects("app"), activity(live=["/code/app"]))
    assert events(records)[1][0].payload.error_kind == "gh_failed"
