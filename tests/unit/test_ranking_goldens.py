"""The "what next" ranking (WP6.2): golden lists for each ``PLAN.md`` §7 rule alone and combined.

Every case is a fixed live view and a fixed ``now``; the expected order, scores and reason strings
are written out in full. Ties break by age, and the output is the same for the same input.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fylgja.core.context_items import ContextEntry
from fylgja.core.ranking import (
    CONTEXT_SWITCH,
    IDLE_WINDOW,
    MAX_ITEMS,
    SCORES,
    TODO,
    Candidate,
    rank_next,
)
from fylgja.core.waiting import RANKS, WaitingEntry, waiting_on_me
from fylgja.store.rows import ActivityRow, ProjectRow, SessionRow, TodoRow

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def entry(
    kind: str, target: str, since: datetime | None, reason: str = "", target_kind: str = "session"
) -> WaitingEntry:
    return WaitingEntry(
        kind=kind, target=target, target_kind=target_kind, reason=reason or kind, since=since
    )


def session(
    key: str,
    *,
    workspace: str | None = None,
    project: str | None = None,
    state: str | None = None,
    since: datetime | None = None,
    reason: str | None = None,
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
        workspace=workspace,
        name=None,
        title=None,
        model=None,
        branch=None,
        state=state,
        state_since=since,
        waiting_reason=reason,
        parent_key=None,
        predecessor_key=None,
        remote_url=None,
        first_seen=ago(600),
        ended_at=None,
        last_seen=NOW,
    )


def active(key: str, valid_from: datetime) -> ActivityRow:
    return ActivityRow(
        session_key=key,
        source="agents",
        valid_from=valid_from,
        state="idle",
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


def todo(
    todo_id: str,
    text: str,
    *,
    status: str = "next",
    scope: tuple[str, str] = ("workspace", "3"),
    created: datetime | None = None,
) -> TodoRow:
    when = created or ago(300)
    return TodoRow(
        id=todo_id,
        scope_kind=scope[0],
        scope_ref=scope[1],
        text=text,
        status=status,
        created_at=when,
        done_at=None,
        source="fylgja",
        source_ref=None,
        link=None,
        updated_at=when,
    )


def shape(candidates: list[Candidate]) -> list[tuple[str, str, float, str]]:
    return [(c.kind, c.target_ref, c.score, c.reason) for c in candidates]


# --------------------------------------------------------------------------------------------
# Each rule alone
# --------------------------------------------------------------------------------------------


def test_nothing_gives_an_empty_list() -> None:
    assert rank_next(now=NOW) == []


def test_rule_1_an_agent_waiting_for_permission_or_input() -> None:
    found = rank_next(
        now=NOW,
        waiting=[
            entry("permission", "claude:a", ago(5), "waiting for permission: Bash"),
            entry("input", "norn:run@1", ago(9), "waiting for input: agent question"),
        ],
    )
    assert shape(found) == [
        ("input", "norn:run@1", 100.0, "waiting for input: agent question"),
        ("permission", "claude:a", 100.0, "waiting for permission: Bash"),
    ]  # the same score: the older wait comes first


def test_rule_2_a_finished_turn_nobody_has_seen() -> None:
    found = rank_next(
        now=NOW,
        waiting=[entry("turn_done", "claude:b", ago(20), "finished a turn nobody has looked at")],
    )
    assert shape(found) == [("turn_done", "claude:b", 90.0, "finished a turn nobody has looked at")]


def test_rule_3_a_todo_marked_next_in_the_focused_workspace() -> None:
    found = rank_next(
        now=NOW,
        todos=[
            todo("t1", "write the release notes"),
            todo("t2", "an open one", status="open"),
            todo("t3", "another workspace", scope=("workspace", "4")),
        ],
        focused_workspace="3",
    )
    assert shape(found) == [
        (TODO, "t1", 80.0, "todo marked next in workspace 3: write the release notes")
    ]


def test_rule_3_needs_a_focused_workspace() -> None:
    assert rank_next(now=NOW, todos=[todo("t1", "x")], focused_workspace=None) == []


def test_rule_3_a_project_or_session_todo_counts_for_the_workspace_it_sits_in() -> None:
    sessions = [
        session("claude:a", workspace="3", project="/work/alpha"),
        session("claude:b", workspace="4", project="/work/beta"),
    ]
    found = rank_next(
        now=NOW,
        sessions=sessions,
        todos=[
            todo("t1", "by project", scope=("project", "/work/alpha"), created=ago(60)),
            todo("t2", "by session", scope=("session", "claude:a"), created=ago(90)),
            todo("t3", "elsewhere", scope=("project", "/work/beta")),
            todo("t4", "unknown", scope=("session", "claude:gone")),
        ],
        focused_workspace="3",
    )
    assert shape(found) == [
        (TODO, "t2", 80.0, "todo marked next in workspace 3: by session"),
        (TODO, "t1", 80.0, "todo marked next in workspace 3: by project"),
    ]  # equal scores: the older todo first


def test_rule_3_a_long_text_is_clipped_to_one_line() -> None:
    found = rank_next(now=NOW, todos=[todo("t1", "word  " * 60 + "\nend")], focused_workspace="3")
    reason = found[0].reason
    assert reason.startswith("todo marked next in workspace 3: word word")
    assert reason.endswith("…")
    assert "\n" not in reason
    assert len(reason) <= len("todo marked next in workspace 3: ") + 100


def test_rule_4_a_failed_build_or_a_review_on_a_project_touched_today() -> None:
    found = rank_next(
        now=NOW,
        waiting=[
            entry("review_requested", "/work/b", ago(50), "review required on b", "project"),
            entry("build_failed", "/work/a", ago(40), "build failed on a", "project"),
        ],
    )
    assert shape(found) == [
        ("review_requested", "/work/b", 70.0, "review required on b"),
        ("build_failed", "/work/a", 70.0, "build failed on a"),
    ]


def rule_5(
    *,
    focus: dict[str, float] | None = None,
    sessions: list[SessionRow] | None = None,
    activity: dict[str, ActivityRow] | None = None,
    todos: list[TodoRow] | None = None,
    focused: str | None = "1",
) -> list[Candidate]:
    return rank_next(
        now=NOW,
        sessions=sessions or [],
        activity=activity or {},
        todos=[todo("t1", "pick up", status="open", scope=("workspace", "6"))]
        if todos is None
        else todos,
        focused_workspace=focused,
        focus_seconds={"6": 3600.0, "7": 600.0} if focus is None else focus,
    )


def test_rule_5_the_workspace_with_most_focus_that_has_an_open_todo_and_went_quiet() -> None:
    found = rule_5()
    assert shape(found) == [
        (
            CONTEXT_SWITCH,
            "6",
            60.0,
            "workspace 6 has had the most focus today, holds an open todo "
            "and has been quiet for an hour",
        )
    ]
    assert found[0].since == ago(300)  # the age of its oldest open todo


def test_rule_5_counts_the_open_todos_and_picks_only_one_workspace() -> None:
    found = rule_5(
        todos=[
            todo("t1", "a", status="open", scope=("workspace", "6")),
            todo("t2", "b", status="next", scope=("workspace", "6")),
            todo("t3", "c", status="open", scope=("workspace", "7")),
        ],
        focused="1",
    )
    assert [c.target_ref for c in found if c.kind == CONTEXT_SWITCH] == ["6"]
    assert "holds 2 open todos" in found[0].reason


def test_rule_5_goes_to_the_next_workspace_when_the_top_one_has_no_open_todo() -> None:
    found = rule_5(todos=[todo("t1", "c", status="open", scope=("workspace", "7"))])
    assert [c.target_ref for c in found] == ["7"]


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("it is the workspace in focus", {"focused": "6", "focus": {"6": 3600.0}}),
        ("it has no open todo", {"todos": []}),
        ("it has no focus today", {"focus": {"6": 0.0}}),
        ("a done todo is not open", {"todos": [todo("t1", "x", status="done")]}),
    ],
)
def test_rule_5_does_not_offer(label: str, kwargs: dict[str, Any]) -> None:
    assert rule_5(**kwargs) == [], label


def test_rule_5_a_session_active_within_the_hour_keeps_the_workspace_busy() -> None:
    busy = session("claude:a", workspace="6", state="idle", since=ago(30))
    assert rule_5(sessions=[busy]) == []
    by_activity = session("claude:a", workspace="6", state="idle", since=ago(300))
    assert rule_5(sessions=[by_activity], activity={"claude:a": active("claude:a", ago(59))}) == []
    quiet = session("claude:a", workspace="6", state="idle", since=ago(61))
    assert [
        c.kind for c in rule_5(sessions=[quiet], activity={"claude:a": active("claude:a", ago(61))})
    ] == [CONTEXT_SWITCH]
    other = session("claude:b", workspace="9", state="idle", since=ago(1))
    assert [c.kind for c in rule_5(sessions=[other])] == [CONTEXT_SWITCH]


def test_rule_5_the_idle_window_is_an_hour() -> None:
    assert timedelta(hours=1) == IDLE_WINDOW


def test_rule_6_unread_teams_or_slack_lowest() -> None:
    found = rank_next(
        now=NOW,
        waiting=[entry("unread", "9", ago(2), "3 unread in general (Channel)", "window")],
    )
    assert shape(found) == [("unread", "9", 50.0, "3 unread in general (Channel)")]


# --------------------------------------------------------------------------------------------
# Combinations
# --------------------------------------------------------------------------------------------


def everything() -> dict[str, Any]:
    return {
        "now": NOW,
        "waiting": [
            entry("unread", "9", ago(500), "2 unread in general", "window"),
            entry("build_failed", "/work/a", ago(400), "build failed on a", "project"),
            entry("turn_done", "claude:b", ago(300), "finished a turn nobody has looked at"),
            entry("permission", "claude:a", ago(1), "waiting for permission: Bash"),
        ],
        "todos": [
            todo("t1", "write the notes", created=ago(200)),
            todo("t6", "pick up", status="open", scope=("workspace", "6"), created=ago(250)),
        ],
        "focused_workspace": "3",
        "focus_seconds": {"6": 4000.0, "3": 9000.0},
    }


def test_every_rule_together_comes_out_in_the_plan_order() -> None:
    assert shape(rank_next(**everything())) == [
        ("permission", "claude:a", 100.0, "waiting for permission: Bash"),
        ("turn_done", "claude:b", 90.0, "finished a turn nobody has looked at"),
        (TODO, "t1", 80.0, "todo marked next in workspace 3: write the notes"),
        ("build_failed", "/work/a", 70.0, "build failed on a"),
        (
            CONTEXT_SWITCH,
            "6",
            60.0,
            "workspace 6 has had the most focus today, holds an open todo "
            "and has been quiet for an hour",
        ),
        ("unread", "9", 50.0, "2 unread in general"),
    ]


def test_a_newer_higher_rule_beats_an_older_lower_one() -> None:
    found = rank_next(
        now=NOW,
        waiting=[
            entry("unread", "9", ago(900), "unread", "window"),
            entry("turn_done", "claude:b", ago(1), "finished"),
            entry("input", "claude:c", ago(2), "waiting for input"),
        ],
    )
    assert [c.kind for c in found] == ["input", "turn_done", "unread"]


def test_the_scores_follow_the_waiting_ranks() -> None:
    by_rank: dict[int, set[float]] = {}
    for kind, rank in RANKS.items():
        by_rank.setdefault(rank, set()).add(SCORES[kind])
    assert all(len(scores) == 1 for scores in by_rank.values())
    ordered = [next(iter(by_rank[rank])) for rank in sorted(by_rank)]
    assert ordered == sorted(ordered, reverse=True)
    assert SCORES[TODO] > SCORES["build_failed"] > SCORES[CONTEXT_SWITCH] > SCORES["unread"]
    assert SCORES["turn_done"] > SCORES[TODO]


def test_a_real_waiting_merge_feeds_the_ranking() -> None:
    sessions = [
        session("claude:a", state="waiting_permission", since=ago(3), reason="Bash"),
        session("claude:b", state="turn_done", since=ago(8)),
        session("claude:c", state="idle", since=ago(1)),
    ]
    entries = waiting_on_me(now=NOW, sessions=sessions)
    assert shape(rank_next(now=NOW, waiting=entries, sessions=sessions)) == [
        ("permission", "claude:a", 100.0, "waiting for permission: Bash"),
        ("turn_done", "claude:b", 90.0, "finished a turn nobody has looked at"),
    ]


def test_a_build_and_a_review_of_one_project_are_one_target_in_waiting_so_one_item() -> None:
    project = ProjectRow(
        path="/work/a",
        repo_root="/work/a",
        remote_url=None,
        repo_name="a",
        group_name=None,
        branch="main",
        dirty=None,
        ahead=None,
        behind=None,
        pr_number=4,
        pr_state="OPEN",
        review_decision="REVIEW_REQUIRED",
        build_state="fail",
        build={"state": "fail", "workflows": []},
        build_observed_at=ago(10),
        build_error_kind=None,
        last_commit_at=None,
        updated_at=ago(30),
    )
    sessions = [session("claude:a", project="/work/a", state="idle", since=ago(5))]
    entries = waiting_on_me(now=NOW, sessions=sessions, projects=[project])
    found = rank_next(now=NOW, waiting=entries, sessions=sessions)
    assert [(c.kind, c.target_ref, c.score) for c in found] == [("build_failed", "/work/a", 70.0)]


def test_an_unread_marker_shows_its_title_only() -> None:
    marker = ContextEntry(
        window_id="9",
        key="k",
        source="ctx",
        kind="chat",
        title="general (Channel)",
        data={},
        unread=4,
        tier=1,
        confidence="high",
        link=None,
        observed_at=ago(4),
    )
    found = rank_next(now=NOW, waiting=waiting_on_me(now=NOW, contexts={"9": [marker]}))
    assert shape(found) == [("unread", "9", 50.0, "4 unread in general (Channel)")]


# --------------------------------------------------------------------------------------------
# Ties, determinism, limits
# --------------------------------------------------------------------------------------------


def test_ties_are_broken_by_age_oldest_first_then_the_unknown_age_then_the_target() -> None:
    found = rank_next(
        now=NOW,
        waiting=[
            entry("permission", "claude:z", None),
            entry("permission", "claude:b", ago(5)),
            entry("permission", "claude:a", ago(5)),
            entry("permission", "claude:y", ago(30)),
            entry("input", "claude:c", ago(5)),
        ],
    )
    assert [c.target_ref for c in found] == [
        "claude:y",  # the oldest
        "claude:a",  # the same age: the kind (permission before input), then the target
        "claude:b",
        "claude:c",
        "claude:z",  # no age: last
    ]


def test_the_same_view_gives_the_same_list_whatever_the_input_order() -> None:
    base = everything()
    expected = rank_next(**base)
    assert rank_next(**base) == expected
    generator = random.Random(7)
    for _ in range(20):
        shuffled = dict(base)
        for name in ("waiting", "todos"):
            items = list(base[name])
            generator.shuffle(items)
            shuffled[name] = items
        assert rank_next(**shuffled) == expected


def test_a_list_holds_at_most_the_limit() -> None:
    waiting = [entry("permission", f"claude:{n:02d}", ago(n + 1)) for n in range(MAX_ITEMS + 5)]
    assert len(rank_next(now=NOW, waiting=waiting)) == MAX_ITEMS
    assert [c.target_ref for c in rank_next(now=NOW, waiting=waiting, limit=2)] == [
        f"claude:{MAX_ITEMS + 4:02d}",
        f"claude:{MAX_ITEMS + 3:02d}",
    ]
    assert rank_next(now=NOW, waiting=waiting, limit=0) == []


def test_an_ended_session_does_not_place_a_todo() -> None:
    gone = session("claude:a", workspace="3")
    ended = SessionRow(**{**gone.__dict__, "ended_at": ago(1)})
    found = rank_next(
        now=NOW,
        sessions=[ended],
        todos=[todo("t1", "x", scope=("session", "claude:a"))],
        focused_workspace="3",
    )
    assert found == []
