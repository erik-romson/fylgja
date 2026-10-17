"""Waiting-on-me (WP6.1): the pure merge of sessions, projects and unread markers.

Every source has a fixture, one target counts once, the order is rank then age, and "touched
today" is judged against the start of the local day (a fake ``now`` and an explicit zone).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from fylgja.core.context_items import ContextEntry
from fylgja.core.waiting import (
    KINDS,
    RANKS,
    WaitingEntry,
    start_of_day,
    waiting_on_me,
)
from fylgja.store.rows import ActivityRow, ProjectRow, SessionRow

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
MIDNIGHT = datetime(2026, 10, 6, 0, 0, tzinfo=UTC)


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def session(
    key: str,
    state: str | None,
    since: datetime | None = None,
    *,
    reason: str | None = None,
    project: str | None = None,
    ended: datetime | None = None,
) -> SessionRow:
    kind, _, native = key.partition(":")
    return SessionRow(
        session_key=key,
        kind=kind,
        native_id=native,
        source="agents",
        pid=None,
        process_key=None,
        cwd=project,
        project_path=project,
        host_kind=None,
        host_ref=None,
        window_id=None,
        workspace=None,
        name=None,
        title="a title that never shows",
        model=None,
        branch=None,
        state=state,
        state_since=since,
        waiting_reason=reason,
        parent_key=None,
        predecessor_key=None,
        remote_url=None,
        first_seen=ago(600),
        ended_at=ended,
        last_seen=NOW,
    )


def activity(key: str, state: str | None, valid_from: datetime) -> ActivityRow:
    return ActivityRow(
        session_key=key,
        source="agents",
        valid_from=valid_from,
        state=state,
        waiting_reason=None,
        last_tool=None,
        current_target=None,
        open_task=None,
        stage=None,
        last_prompt=None,
        last_reply=None,
        one_liner=None,
        context_pct=None,
        cost_usd=None,
        tokens_input=None,
        tokens_output=None,
        tokens_cache_read=None,
        tokens_cache_write=None,
    )


def project(
    path: str,
    *,
    build: str | None = None,
    workflows: list[dict[str, Any]] | None = None,
    pr: int | None = None,
    review: str | None = None,
    pr_state: str | None = "OPEN",
    observed: datetime | None = None,
    updated: datetime | None = None,
) -> ProjectRow:
    stored = None if build is None else {"state": build, "workflows": workflows or []}
    return ProjectRow(
        path=path,
        repo_root=path,
        remote_url=None,
        repo_name="repo",
        group_name=None,
        branch="main",
        dirty=None,
        ahead=None,
        behind=None,
        pr_number=pr,
        pr_state=pr_state,
        review_decision=review,
        build_state=build,
        build=stored,
        build_observed_at=observed,
        build_error_kind=None,
        last_commit_at=None,
        updated_at=updated or ago(30),
    )


def marker(
    window: str, unread: int | bool | None, *, title: str | None = "general", at: datetime = NOW
) -> ContextEntry:
    return ContextEntry(
        window_id=window,
        key=f"title-{title}",
        source="ctx",
        kind="chat",
        title=title,
        data={},
        unread=unread,
        confidence="high",
        link=None,
        tier=1,
        observed_at=at,
    )


def run(**inputs: Any) -> list[WaitingEntry]:
    inputs.setdefault("tz", UTC)
    return waiting_on_me(now=NOW, **inputs)


def view(entries: list[WaitingEntry]) -> list[tuple[str, str]]:
    return [(e.kind, e.target) for e in entries]


# --------------------------------------------------------------------------------------------
# One fixture per source
# --------------------------------------------------------------------------------------------


def test_nothing_waits_when_nothing_is_given() -> None:
    assert run() == []


def test_a_session_waiting_for_permission() -> None:
    (entry,) = run(
        sessions=[
            session("claude:a", "waiting_permission", ago(5), reason="approve Write notes.txt")
        ]
    )
    assert (entry.kind, entry.target, entry.target_kind) == ("permission", "claude:a", "session")
    assert entry.reason == "waiting for permission: approve Write notes.txt"
    assert entry.since == ago(5)


def test_a_session_waiting_for_input_has_a_reason_in_words_without_a_detail() -> None:
    (entry,) = run(sessions=[session("claude:a", "waiting_input", ago(2))])
    assert (entry.kind, entry.reason) == ("input", "waiting for input")


def test_a_norn_run_waiting_for_input() -> None:
    (entry,) = run(
        sessions=[session("norn:wf:1", "waiting_input", ago(9), reason="the agent asked")]
    )
    assert (entry.kind, entry.target) == ("input", "norn:wf:1")
    assert entry.reason == "waiting for input: the agent asked"


def test_an_unseen_turn() -> None:
    (entry,) = run(sessions=[session("claude:a", "turn_done", ago(3))])
    assert (entry.kind, entry.target_kind) == ("turn_done", "session")
    assert entry.reason == "finished a turn nobody has looked at"


@pytest.mark.parametrize("state", ["passed", "failed", "cancelled"])
def test_a_finished_run_nobody_has_looked_at(state: str) -> None:
    row = replace(session("norn:wf:1", state, ago(5), project="/p"), name="plan_v2")
    (entry,) = run(sessions=[row], touched={"/p": ago(30)})
    assert (entry.kind, entry.target, entry.target_kind) == ("run_finished", "norn:wf:1", "session")
    assert (entry.reason, entry.since) == (f"plan_v2 {state}", ago(5))


def test_a_run_without_a_name_is_named_by_its_kind() -> None:
    (entry,) = run(sessions=[session("norn:wf:1", "failed", ago(5))])
    assert entry.reason == "norn failed"


def test_focus_on_the_project_after_the_run_ended_has_seen_it() -> None:
    row = session("norn:wf:1", "passed", ago(5), project="/p")
    assert run(sessions=[row], touched={"/p": ago(5)}) == []
    assert run(sessions=[row], touched={"/p": ago(1)}) == []


def test_a_finished_run_that_left_the_view_no_longer_waits() -> None:
    row = session("norn:wf:1", "passed", ago(5), project="/p", ended=ago(1))
    assert run(sessions=[row]) == []


def test_a_finished_run_without_a_state_time_dates_from_its_first_sight() -> None:
    (entry,) = run(sessions=[session("norn:wf:1", "passed")])
    assert entry.since == ago(600)


@pytest.mark.parametrize(
    "state", [None, "idle", "working", "running_tool", "seen", "ended", "waiting", "busy", "error"]
)
def test_other_states_do_not_wait_and_a_seen_turn_has_left(state: str | None) -> None:
    assert run(sessions=[session("claude:a", state, ago(1))]) == []


def test_an_ended_session_does_not_wait() -> None:
    ended = session("claude:a", "waiting_input", ago(5), ended=ago(1))
    assert run(sessions=[ended]) == []


def test_a_session_without_a_state_falls_back_to_its_activity() -> None:
    row = session("claude:a", None)
    (entry,) = run(
        sessions=[row], activity={"claude:a": activity("claude:a", "waiting_input", ago(4))}
    )
    assert (entry.kind, entry.since) == ("input", ago(4))


def test_the_state_of_the_row_wins_over_a_stale_activity() -> None:
    row = session("claude:a", "seen", ago(1))
    stale = {"claude:a": activity("claude:a", "turn_done", ago(8))}
    assert run(sessions=[row], activity=stale) == []


def test_a_failed_build_on_a_project_touched_today() -> None:
    row = project(
        "/work/a",
        build="fail",
        workflows=[
            {"workflow": "ci", "state": "fail", "run_started_at": ago(50).isoformat()},
            {"workflow": "lint", "state": "fail", "run_started_at": ago(70).isoformat()},
            {"workflow": "docs", "state": "pass", "run_started_at": ago(500).isoformat()},
        ],
    )
    (entry,) = run(projects=[row], touched={"/work/a": ago(10)})
    assert (entry.kind, entry.target, entry.target_kind) == ("build_failed", "/work/a", "project")
    assert entry.reason == "build failed on repo"
    assert entry.since == ago(70)  # the oldest failing run


def test_a_failed_build_without_workflow_times_dates_from_its_observation() -> None:
    row = project("/work/a", build="fail", observed=ago(15))
    (entry,) = run(projects=[row], touched={"/work/a": NOW})
    assert entry.since == ago(15)


def test_a_passing_build_does_not_wait() -> None:
    assert run(projects=[project("/work/a", build="pass")], touched={"/work/a": NOW}) == []


def test_the_build_state_column_is_the_fallback_of_the_stored_fold() -> None:
    row = project("/work/a", build="fail")
    row = ProjectRow(**{**row.__dict__, "build": {"workflows": []}})
    assert view(run(projects=[row], touched={"/work/a": NOW})) == [("build_failed", "/work/a")]


def test_a_review_request_on_a_project_touched_today() -> None:
    row = project("/work/a", pr=7, review="REVIEW_REQUIRED", updated=ago(40))
    (entry,) = run(projects=[row], touched={"/work/a": ago(1)})
    assert (entry.kind, entry.target) == ("review_requested", "/work/a")
    assert entry.reason == "review required on the pull request of repo"
    assert entry.since == ago(40)


@pytest.mark.parametrize(
    ("pr", "review", "pr_state"),
    [
        (None, "REVIEW_REQUIRED", None),
        (0, "REVIEW_REQUIRED", "OPEN"),
        (7, "APPROVED", "OPEN"),
        (7, None, "OPEN"),
        (7, "REVIEW_REQUIRED", "MERGED"),
        (7, "REVIEW_REQUIRED", "closed"),
    ],
)
def test_other_pull_requests_do_not_ask_for_a_review(
    pr: int | None, review: str | None, pr_state: str | None
) -> None:
    row = project("/work/a", pr=pr, review=review, pr_state=pr_state)
    assert run(projects=[row], touched={"/work/a": NOW}) == []


def test_unread_markers_come_from_the_titles_that_set_them() -> None:
    entries = run(
        contexts={
            "w1": [marker("w1", 3, title="general", at=ago(7))],
            "w2": [marker("w2", True, title="random")],
            "w3": [marker("w3", None, title="quiet")],
            "w4": [marker("w4", 0, title="empty")],
            "w5": [marker("w5", False, title="off")],
        }
    )
    assert view(entries) == [("unread", "w1"), ("unread", "w2")]
    first, second = entries
    assert (first.target_kind, first.reason, first.since) == (
        "window",
        "3 unread in general",
        ago(7),
    )
    assert second.reason == "unread in random"


def test_an_unread_window_sums_its_markers_and_may_lack_a_title() -> None:
    entry, *rest = run(
        contexts={
            "w1": [
                marker("w1", 2, title=None, at=ago(3)),
                marker("w1", 4, title="two", at=ago(9)),
                marker("w1", None, title="no marker"),
            ]
        }
    )
    assert rest == []
    assert (entry.reason, entry.since) == ("6 unread in two", ago(9))
    (bare,) = run(contexts={"w1": [marker("w1", True, title=None)]})
    assert bare.reason == "unread"


def test_a_long_reason_is_cut() -> None:
    (entry,) = run(sessions=[session("claude:a", "waiting_input", ago(1), reason="x  y\n" * 100)])
    assert len(entry.reason) <= len("waiting for input: ") + 120
    assert entry.reason.endswith("…")
    assert "\n" not in entry.reason


# --------------------------------------------------------------------------------------------
# One target, once
# --------------------------------------------------------------------------------------------


def test_a_project_that_qualifies_twice_counts_once_as_the_failed_build() -> None:
    row = project("/work/a", build="fail", pr=7, review="REVIEW_REQUIRED")
    assert view(run(projects=[row], touched={"/work/a": NOW})) == [("build_failed", "/work/a")]


def test_a_session_key_that_equals_a_window_id_is_still_a_different_target() -> None:
    entries = run(
        sessions=[session("claude:a", "turn_done", ago(2))],
        contexts={"claude:a": [marker("claude:a", 1)]},
    )
    assert view(entries) == [("turn_done", "claude:a"), ("unread", "claude:a")]


# --------------------------------------------------------------------------------------------
# Order
# --------------------------------------------------------------------------------------------


def test_ranks_follow_the_plan() -> None:
    assert RANKS["permission"] == RANKS["input"] < RANKS["turn_done"] == RANKS["run_finished"]
    assert RANKS["turn_done"] < RANKS["build_failed"] == RANKS["review_requested"]
    assert RANKS["review_requested"] < RANKS["unread"]
    assert set(RANKS) == set(KINDS)


def test_the_list_is_ordered_by_rank_then_oldest_first() -> None:
    entries = run(
        sessions=[
            session("claude:turn-new", "turn_done", ago(1)),
            session("claude:turn-old", "turn_done", ago(30)),
            session("claude:input", "waiting_input", ago(5)),
            session("claude:perm-old", "waiting_permission", ago(20)),
            session("claude:perm-new", "waiting_permission", ago(2)),
        ],
        projects=[
            project("/work/review", pr=3, review="REVIEW_REQUIRED", updated=ago(100)),
            project("/work/fail", build="fail", observed=ago(60)),
        ],
        touched={"/work/review": ago(5), "/work/fail": ago(5)},
        contexts={"w9": [marker("w9", 1, at=ago(500))]},
    )
    assert view(entries) == [
        ("permission", "claude:perm-old"),
        ("input", "claude:input"),
        ("permission", "claude:perm-new"),
        ("turn_done", "claude:turn-old"),
        ("turn_done", "claude:turn-new"),
        ("review_requested", "/work/review"),
        ("build_failed", "/work/fail"),
        ("unread", "w9"),
    ]


# --------------------------------------------------------------------------------------------
# Touched today
# --------------------------------------------------------------------------------------------

FAILED = project("/work/a", build="fail", observed=ago(15))


def failed(**inputs: Any) -> list[WaitingEntry]:
    return run(projects=[FAILED], **inputs)


def test_a_project_nobody_touched_today_does_not_wait() -> None:
    assert failed() == []
    assert failed(touched={"/work/other": NOW}) == []


def test_the_boundary_is_the_start_of_the_day() -> None:
    assert view(failed(touched={"/work/a": MIDNIGHT})) == [("build_failed", "/work/a")]
    assert failed(touched={"/work/a": MIDNIGHT - timedelta(microseconds=1)}) == []


def test_the_day_starts_at_local_midnight() -> None:
    plus_two = timezone(timedelta(hours=2))
    now = datetime(2026, 10, 6, 1, 0, tzinfo=plus_two)  # 23:00 UTC the evening before
    assert start_of_day(now, plus_two) == datetime(2026, 10, 6, 0, 0, tzinfo=plus_two)
    local_midnight = datetime(2026, 10, 6, 0, 0, tzinfo=plus_two)
    inputs: dict[str, Any] = {"projects": [FAILED], "tz": plus_two}
    assert waiting_on_me(now=now, touched={"/work/a": local_midnight}, **inputs)
    assert not waiting_on_me(
        now=now, touched={"/work/a": local_midnight - timedelta(seconds=1)}, **inputs
    )


def test_the_default_zone_is_the_local_one() -> None:
    assert start_of_day(NOW) == NOW.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)


def test_session_activity_today_touches_the_project() -> None:
    by_state = session("claude:a", "working", MIDNIGHT + timedelta(minutes=1), project="/work/a")
    assert view(failed(sessions=[by_state])) == [("build_failed", "/work/a")]
    by_interval = session("claude:a", "working", ago(60 * 20), project="/work/a")
    activity_today = {"claude:a": activity("claude:a", "working", MIDNIGHT)}
    assert view(failed(sessions=[by_interval], activity=activity_today)) == [
        ("build_failed", "/work/a")
    ]


def test_an_old_session_does_not_touch_the_project_even_while_it_lives() -> None:
    old = session("claude:a", "idle", MIDNIGHT - timedelta(hours=1), project="/work/a")
    stale = {"claude:a": activity("claude:a", "idle", MIDNIGHT - timedelta(hours=1))}
    assert failed(sessions=[old], activity=stale) == []


def test_a_session_of_another_project_or_an_ended_one_touches_nothing() -> None:
    other = session("claude:b", "working", ago(1), project="/work/b")
    ended = session("claude:c", "working", ago(1), project="/work/a", ended=ago(1))
    assert failed(sessions=[other, ended]) == []


def test_a_session_without_a_state_time_has_no_activity_to_count() -> None:
    row = session("claude:a", "idle", None, project="/work/a")
    assert failed(sessions=[row]) == []


# --------------------------------------------------------------------------------------------
# What feeds it
# --------------------------------------------------------------------------------------------


def test_the_last_focus_of_a_project_is_the_end_of_its_latest_span_or_now() -> None:
    from fylgja.core.spans import Span, last_focus_by_project

    def span(project: str | None, start: datetime, end: datetime | None) -> Span:
        return Span(
            window_id="1",
            workspace="1",
            app_bundle_id=None,
            title=None,
            project_path=project,
            session_key=None,
            started_at=start,
            ended_at=end,
            end_reason=None if end is None else "focus",
        )

    found = last_focus_by_project(
        [
            span("/a", ago(300), ago(200)),
            span("/a", ago(100), ago(90)),
            span("/a", ago(250), ago(240)),
            span("/b", ago(10), None),
            span(None, ago(5), ago(4)),
        ],
        NOW,
    )
    assert found == {"/a": ago(90), "/b": NOW}


def test_the_live_view_announces_a_change_that_is_not_a_row() -> None:
    from fylgja.core.live_view import LiveUpdate, LiveView

    view_ = LiveView()
    seen: list[LiveUpdate] = []
    view_.subscribe(seen.append)
    view_.touch()
    assert [(u.version, u.changes) for u in seen] == [(1, ())]
    assert view_.version == 1


def test_the_context_memory_reports_a_real_change_once() -> None:
    from fylgja.collectors.appcontext import parse
    from fylgja.collectors.appcontext.titles import parse_title
    from fylgja.core.context_items import ContextMemory

    title = parse_title(
        "com.tinyspeck.slackmacgap", "general (Channel) - Team - 2 new items - Slack"
    )
    assert title is not None
    facts = parse.WindowFacts("9", "com.tinyspeck.slackmacgap", "general", "4")
    memory = ContextMemory()
    calls: list[int] = []
    memory.on_change = lambda: calls.append(1)
    first = parse.to_record(parse.title_item(title), facts, NOW)
    memory.observe(first)
    memory.observe(first.model_copy(update={"observed_at": NOW + timedelta(seconds=30)}))
    assert len(calls) == 1  # the same item seen again is not a change
