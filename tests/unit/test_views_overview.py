"""``build_overview``: the Overview's rules, moved from the page into the daemon
(``docs/api/views.md``).

Every test builds a ``/now`` by hand and checks one rule the page once ran in JavaScript: the caps
and their ``*_more`` counts, waiting sessions first, no card for an idle workspace, the click
targets of cards and Next items, the todo groups and their titles, ``workspace_order``, and the
serialization rule (no ``null``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from api_support import nulls

from fylgja.api.now_models import (
    Build,
    ContextItem,
    Focused,
    Freshness,
    LabelSource,
    NextItem,
    Now,
    Project,
    PullRequest,
    Session,
    SessionState,
    Todo,
    Window,
    Workspace,
    dump_json,
)
from fylgja.api.view_models import FocusTarget, NextList, Overview
from fylgja.api.views import MAX_CONTEXT, MAX_SESSIONS, MAX_TODOS, build_overview, scope_title
from fylgja.core.todo_import import SCOPE_PROJECT, SCOPE_SESSION, SCOPE_WORKSPACE

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
LIST_ID = "nl-0000000000000000"


# --------------------------------------------------------------------------------------------
# A hand-built /now
# --------------------------------------------------------------------------------------------


def win(window_id: str, *, focused: bool = False, context: Sequence[ContextItem] = ()) -> Window:
    return Window(id=window_id, app="App", focused=focused, context=list(context) or None)


def ws(
    name: str,
    *,
    windows: Sequence[Window] = (),
    sessions: Sequence[str] = (),
    projects: Sequence[str] = (),
    todos: Sequence[Todo] = (),
    focused: bool = False,
    label: str = "—",
    inferred: str | None = None,
    one_liner: str = "",
) -> Workspace:
    return Workspace(
        name=name,
        label=label,
        label_source=LabelSource.NONE,
        inferred_label=inferred,
        one_liner=one_liner,
        focused=focused,
        windows=list(windows),
        projects=list(projects),
        sessions=list(sessions),
        todos=list(todos) or None,
    )


def sess(
    key: str,
    state: SessionState | None = None,
    *,
    one_liner: str | None = None,
    title: str | None = None,
    name: str | None = None,
    project_path: str | None = None,
    children: Sequence[Session] = (),
) -> Session:
    kind, _, native = key.partition(":")
    return Session(
        key=key,
        kind=kind,
        native_id=native,
        state=state,
        one_liner=one_liner,
        title=title,
        name=name,
        project_path=project_path,
        children=list(children) or None,
    )


def proj(path: str, name: str, *, workspace: str | None = None, **fields: Any) -> Project:
    return Project(path=path, name=name, workspace=workspace, sessions=[], **fields)


def todo(
    todo_id: str,
    text: str = "a todo",
    *,
    scope: tuple[str, str] | None = (SCOPE_WORKSPACE, "1"),
    status: Literal["open", "next"] = "open",
    source: str | None = "fylgja",
) -> Todo:
    return Todo(
        id=todo_id,
        text=text,
        status=status,
        scope_kind=None if scope is None else scope[0],
        scope_ref=None if scope is None else scope[1],
        source=source,
    )


def now(
    *,
    workspaces: Sequence[Workspace] = (),
    sessions: Sequence[Session] = (),
    projects: Sequence[Project] = (),
    focused: str | None = None,
) -> Now:
    return Now(
        generated_at=T0,
        focused=None if focused is None else Focused(workspace=focused),
        workspaces=list(workspaces),
        sessions=list(sessions),
        projects=list(projects),
        collectors=[],
        degraded=[],
    )


def item(target_ref: str, rank: int = 1, kind: str = "input") -> NextItem:
    return NextItem(
        suggestion_id=f"{LIST_ID}.{rank}",
        kind=kind,
        target_ref=target_ref,
        score=100.0,
        reason=f"reason {rank}",
        rank=rank,
    )


def overview(
    picture: Now,
    todos: Sequence[Todo] = (),
    items: Sequence[NextItem] = (),
    workspace_order: Sequence[str] | None = None,
) -> Overview:
    return build_overview(
        picture,
        todos,
        NextList(list_id=LIST_ID, items=list(items)),
        generated_at=T0,
        workspace_order=workspace_order,
    )


def window(ref: str) -> FocusTarget:
    return FocusTarget(kind="window", ref=ref)


# --------------------------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------------------------


def test_the_caps_hold_and_count_what_they_leave_out() -> None:
    keys = [f"claude:s{i}" for i in range(MAX_SESSIONS + 2)]
    items = [ContextItem(kind="tab", title=f"tab {i}") for i in range(MAX_CONTEXT + 2)]
    todos = [todo(f"t{i}", f"todo {i}") for i in range(MAX_TODOS + 2)]
    picture = now(
        workspaces=[
            ws(
                "1",
                windows=[win("10", context=items[:2]), win("11", context=items[2:])],
                sessions=keys,
                todos=todos,
            )
        ],
        sessions=[sess(k) for k in keys],
    )
    (card,) = overview(picture).cards
    assert [s.key for s in card.sessions] == keys[:MAX_SESSIONS]
    assert card.sessions_more == 2
    # context items are flattened over the windows, in window order
    assert [c.text for c in card.context] == ["tab 0", "tab 1", "tab 2"]
    assert card.context_more == 2
    assert [t.text for t in card.todos] == [f"todo {i}" for i in range(MAX_TODOS)]
    assert card.todos_more == 2


def test_under_the_caps_nothing_is_left_out() -> None:
    picture = now(
        workspaces=[ws("1", windows=[win("10")], sessions=["claude:a"], todos=[todo("t")])],
        sessions=[sess("claude:a")],
    )
    (card,) = overview(picture).cards
    assert (card.sessions_more, card.context_more, card.todos_more) == (0, 0, 0)
    assert card.context == []


def test_waiting_sessions_come_first_and_the_rest_keep_the_now_order() -> None:
    states = {
        "claude:a": SessionState.IDLE,
        "claude:b": SessionState.WAITING_INPUT,
        "claude:c": SessionState.WORKING,
        "claude:d": SessionState.TURN_DONE,
        "claude:e": None,
        "claude:f": SessionState.WAITING_PERMISSION,
        "claude:g": SessionState.WAITING,
    }
    picture = now(
        workspaces=[ws("1", sessions=list(states))],
        sessions=[sess(k, state) for k, state in states.items()],
    )
    (card,) = overview(picture).cards
    assert [s.key for s in card.sessions] == [
        "claude:b",
        "claude:d",
        "claude:f",
        "claude:g",
        "claude:a",
        "claude:c",
    ]
    assert card.sessions_more == 1  # claude:e, the last of the rest
    assert [s.waiting for s in card.sessions] == [True] * 4 + [False] * 2


def test_a_waiting_session_beyond_the_cap_is_shown_first() -> None:
    keys = [f"claude:s{i}" for i in range(MAX_SESSIONS + 1)]
    sessions = [sess(k, SessionState.WORKING) for k in keys[:-1]]
    sessions.append(sess(keys[-1], SessionState.WAITING_INPUT))
    picture = now(workspaces=[ws("1", sessions=keys)], sessions=sessions)
    (card,) = overview(picture).cards
    assert card.sessions[0].key == keys[-1]
    assert card.sessions_more == 1


def test_a_card_session_carries_its_state_text_and_target() -> None:
    child = sess("claude:child", SessionState.RUNNING_TOOL, title="child title")
    picture = now(
        workspaces=[ws("1", sessions=["norn:run", "claude:child", "codex:x", "gone:key"])],
        sessions=[
            sess("norn:run", children=[child], name="run name"),
            sess("codex:x", SessionState.BUSY, one_liner="doing it", title="ignored"),
        ],
    )
    (card,) = overview(picture).cards
    shown = {s.key: s for s in card.sessions}
    assert list(shown) == ["norn:run", "claude:child", "codex:x"]  # a key /now lacks is skipped
    assert (shown["norn:run"].state, shown["norn:run"].text) == ("unknown", "run name")
    assert (shown["claude:child"].state, shown["claude:child"].text) == (
        "running_tool",
        "child title",
    )
    assert shown["codex:x"].text == "doing it"
    assert shown["codex:x"].go == FocusTarget(kind="session", ref="codex:x")
    bare = now(workspaces=[ws("1", sessions=["opencode:z"])], sessions=[sess("opencode:z")])
    assert overview(bare).cards[0].sessions[0].text == "opencode"  # the kind, last


def test_an_idle_workspace_gets_no_card() -> None:
    picture = now(
        workspaces=[
            ws("1"),
            ws("2", windows=[win("20")]),
            ws("3", sessions=["claude:a"]),
            ws("4", todos=[todo("t", scope=(SCOPE_WORKSPACE, "4"))]),
            ws("5", projects=["/work/alpha/main"]),
        ],
        sessions=[sess("claude:a")],
    )
    assert [c.name for c in overview(picture).cards] == ["2", "3", "4"]
    assert overview(now(workspaces=[ws("1")])).cards == []
    assert overview(now()).cards == []


def test_a_card_click_focuses_the_focused_window_else_the_first() -> None:
    picture = now(
        workspaces=[
            ws("1", windows=[win("10"), win("11", focused=True)]),
            ws("2", windows=[win("20"), win("21")]),
            ws("3", todos=[todo("t", scope=(SCOPE_WORKSPACE, "3"))]),
        ]
    )
    cards = {c.name: c for c in overview(picture).cards}
    assert cards["1"].go == window("11")
    assert cards["2"].go == window("20")
    assert cards["3"].go is None
    assert (cards["1"].windows, cards["3"].windows) == (2, 0)


def test_a_card_carries_the_workspace_label_focus_and_one_liner() -> None:
    picture = now(
        workspaces=[
            ws("1", windows=[win("10")], label="alpha", one_liner="alpha — busy", focused=True),
            ws("2", windows=[win("20")], label="", inferred="beta"),
            ws("3", windows=[win("30")]),
        ]
    )
    cards = {c.name: c for c in overview(picture).cards}
    assert (cards["1"].label, cards["1"].focused, cards["1"].one_liner) == (
        "alpha",
        True,
        "alpha — busy",
    )
    assert (cards["2"].label, cards["2"].focused, cards["2"].one_liner) == ("beta", False, None)
    assert cards["3"].label == "—"


def test_a_card_project_line_has_what_the_page_shows() -> None:
    failed = Build(state="fail", source="gh", observed_at=T0, freshness=Freshness.FRESH)
    passed = Build(state="pass", source="gh", observed_at=T0, freshness=Freshness.FRESH)
    picture = now(
        workspaces=[
            ws(
                "1",
                windows=[win("10")],
                projects=["/work/alpha/main", "/work/beta/main", "/work/gone"],
            )
        ],
        projects=[
            proj(
                "/work/alpha/main",
                "alpha/main",
                branch="CBS-1",
                dirty=True,
                pr=PullRequest(number=7, checks="pass"),
                build=failed,
            ),
            proj("/work/beta/main", "beta", build=passed, pr=PullRequest(number=0)),
        ],
    )
    alpha, beta = overview(picture).cards[0].projects
    assert alpha.model_dump(exclude_none=True) == {
        "name": "alpha/main",
        "branch": "CBS-1",
        "dirty": True,
        "pr_number": 7,
        "pr_checks": "pass",
        "build_state": "fail",
        "build_failed": True,
    }
    assert beta.model_dump(exclude_none=True) == {
        "name": "beta",
        "dirty": False,
        "build_state": "pass",
        "build_failed": False,
    }


def test_a_context_item_shows_its_title_else_its_kind_and_its_unread_mark() -> None:
    items = [
        ContextItem(kind="tab", title="a tab"),
        ContextItem(kind="chat", unread=3),
        ContextItem(kind="note", title="a note", unread=0),
    ]
    picture = now(workspaces=[ws("1", windows=[win("10", context=items)])])
    shown = overview(picture).cards[0].context
    assert [(c.text, c.unread) for c in shown] == [
        ("a tab", False),
        ("chat", True),
        ("a note", False),
    ]


def test_a_card_todo_keeps_the_now_order_and_marks_next() -> None:
    todos = [todo("a", "first", status="next"), todo("b", "second")]
    picture = now(workspaces=[ws("1", todos=todos)])
    shown = overview(picture).cards[0].todos
    assert [(t.text, t.next) for t in shown] == [("first", True), ("second", False)]


def test_workspace_order_puts_the_named_cards_first_and_keeps_the_rest_in_order() -> None:
    picture = now(workspaces=[ws(n, windows=[win(n + "0")]) for n in ("1", "2", "3", "10")])
    assert [c.name for c in overview(picture).cards] == ["1", "2", "3", "10"]
    ordered = overview(picture, workspace_order=["10", "missing", "2", "10"])
    assert [c.name for c in ordered.cards] == ["10", "2", "1", "3"]
    assert [c.name for c in overview(picture, workspace_order=[]).cards] == ["1", "2", "3", "10"]


# --------------------------------------------------------------------------------------------
# Next
# --------------------------------------------------------------------------------------------


def test_each_next_target_rule() -> None:
    picture = now(
        workspaces=[
            ws("1", windows=[win("10"), win("11", focused=True)]),
            ws("2", windows=[win("20")]),
            ws("3", sessions=["claude:a"]),
        ],
        sessions=[sess("claude:a", children=[sess("claude:child")])],
        projects=[
            proj("/work/alpha/main", "alpha", workspace="2"),
            proj("/work/beta/main", "beta"),
            proj("/work/gamma/main", "gamma", workspace="3"),
        ],
    )
    todos = [
        todo("t-ws", scope=(SCOPE_WORKSPACE, "1")),
        todo("t-project", scope=(SCOPE_PROJECT, "/work/alpha/main")),
        todo("t-elsewhere", scope=(SCOPE_WORKSPACE, "9")),
    ]
    targets = {
        "claude:a": FocusTarget(kind="session", ref="claude:a"),
        "/work/alpha/main": window("20"),  # a project with a workspace: that workspace's window
        "10": window("10"),  # a window id: that window
        "2": window("20"),  # a workspace name: its window
        "1": window("11"),  # its focused window first
        "t-ws": window("11"),  # an open todo of workspace scope: that workspace's window
        "t-project": None,
        "t-elsewhere": None,  # a workspace /now does not show
        "/work/beta/main": None,  # a project without a workspace
        "/work/gamma/main": None,  # a project whose workspace has no window
        "3": None,  # a workspace without a window
        "claude:child": None,  # only top-level sessions are matched, as the page did
        "nothing:here": None,
    }
    items = [item(ref, rank) for rank, ref in enumerate(targets, start=1)]
    answer = overview(picture, todos, items)
    assert answer.next_list_id == LIST_ID
    assert [e.rank for e in answer.next] == list(range(1, len(targets) + 1))
    assert {ref: e.go for ref, e in zip(targets, answer.next, strict=True)} == targets
    assert answer.next[0].reason == "reason 1"
    assert answer.next[0].kind == "input"


def test_a_next_item_without_a_rank_takes_its_position() -> None:
    unranked = NextItem(suggestion_id="x", kind="todo", target_ref="t", score=1.0, reason="r")
    answer = overview(now(), items=[item("a", rank=1), unranked])
    assert [e.rank for e in answer.next] == [1, 2]


def test_an_empty_next_list_is_an_empty_strip() -> None:
    answer = overview(now())
    assert answer.next == []
    assert answer.next_list_id == LIST_ID


# --------------------------------------------------------------------------------------------
# Focused workspace and todos
# --------------------------------------------------------------------------------------------


def test_the_focused_workspace_comes_from_focused_else_the_flag() -> None:
    flagged = [ws("1"), ws("2", focused=True), ws("3", focused=True)]
    assert overview(now(workspaces=flagged)).focused_workspace == "2"
    assert overview(now(workspaces=flagged, focused="3")).focused_workspace == "3"
    assert overview(now(workspaces=[ws("1")])).focused_workspace is None


def test_the_focused_workspace_group_comes_first() -> None:
    todos = [
        todo("a", "elsewhere", scope=(SCOPE_WORKSPACE, "1")),
        todo("b", "here", scope=(SCOPE_WORKSPACE, "2"), status="next"),
        todo("c", "also here", scope=(SCOPE_WORKSPACE, "2")),
    ]
    picture = now(workspaces=[ws("1"), ws("2", focused=True)])
    focused, everything, one = overview(picture, todos).todo_groups
    assert (focused.kind, focused.title, focused.empty) == ("focused", "Focused workspace 2", None)
    assert [(t.id, t.next) for t in focused.todos] == [("b", True), ("c", False)]
    assert (everything.kind, everything.title, everything.empty) == ("all", "All workspaces", None)
    assert everything.todos == []
    assert (one.kind, one.title, [t.id for t in one.todos]) == ("scope", "Workspace 1 —", ["a"])


def test_empty_groups_say_so() -> None:
    picture = now(workspaces=[ws("1", focused=True)])
    focused, everything = overview(picture).todo_groups
    assert (focused.empty, everything.empty) == ("Nothing open here", "Nothing else open")
    only_here = overview(picture, [todo("a", scope=(SCOPE_WORKSPACE, "1"))]).todo_groups
    assert [(g.kind, g.empty) for g in only_here] == [
        ("focused", None),
        ("all", "Nothing else open"),
    ]


def test_without_a_focused_workspace_every_todo_is_under_all_workspaces() -> None:
    groups = overview(now(workspaces=[ws("1")]), [todo("a")]).todo_groups
    assert [(g.kind, g.title) for g in groups] == [
        ("all", "All workspaces"),
        ("scope", "Workspace 1 —"),
    ]


def test_scope_groups_are_in_first_seen_order_with_their_titles() -> None:
    long_ref = "claude:" + "x" * 60
    picture = now(
        workspaces=[ws("1", label="alpha"), ws("2", focused=True)],
        sessions=[
            sess("claude:a", title="fix the parser", project_path="/work/alpha/main"),
            sess("codex:b", one_liner="reading", project_path="/work/zeta/main"),
            sess("norn:c", name="a run"),
        ],
        projects=[proj("/work/alpha/main", "alpha/main")],
    )
    todos = [
        todo("1", scope=(SCOPE_SESSION, "claude:a")),
        todo("2", scope=(SCOPE_PROJECT, "/work/alpha/main")),
        todo("3", scope=(SCOPE_WORKSPACE, "1")),
        todo("4", scope=(SCOPE_SESSION, "claude:a")),
        todo("5", scope=(SCOPE_PROJECT, "/work/beta/main/")),
        todo("6", scope=(SCOPE_SESSION, "codex:b")),
        todo("7", scope=(SCOPE_SESSION, "norn:c")),
        todo("8", scope=(SCOPE_SESSION, "claude:gone")),
        todo("9", scope=(SCOPE_SESSION, long_ref)),
        todo("10", scope=None),
    ]
    groups = overview(picture, todos).todo_groups[2:]
    assert (
        [(g.title, [t.id for t in g.todos]) for g in groups]
        == [
            ("fix the parser · alpha/main · claude", ["1", "4"]),
            ("Project alpha/main", ["2"]),
            ("Workspace 1 alpha", ["3"]),
            ("Project main", ["5"]),  # a project /now does not show: the base name of its path
            ("reading · main · codex", ["6"]),
            ("a run · norn", ["7"]),  # a session without a project leaves the part out
            ("Session claude:gone", ["8"]),
            ("Session …" + long_ref[-39:], ["9"]),  # 40 characters with the ellipsis
            ("No scope", ["10"]),
        ]
    )
    assert len(groups[7].title) == len("Session ") + 40
    assert {g.kind for g in groups} == {"scope"}


def test_scope_title_prefers_the_session_title() -> None:
    session = sess("claude:a", title="title", one_liner="one-liner", name="name")
    sessions = {"claude:a": session}
    assert scope_title(SCOPE_SESSION, "claude:a", sessions, {}, {}) == "title · claude"
    plain = {"claude:a": sess("claude:a", one_liner="one-liner", name="name")}
    assert scope_title(SCOPE_SESSION, "claude:a", plain, {}, {}) == "one-liner · claude"
    assert scope_title("other", "x", {}, {}, {}) == "No scope"


def test_a_todo_row_can_be_finished_only_when_native() -> None:
    todos = [
        todo("a", source="fylgja"),
        todo("b", source="project-todo-md"),
        todo("c", source=None),
        todo("d", source="claude-tasks", status="next"),
    ]
    everything, group = overview(now(workspaces=[ws("1")]), todos).todo_groups
    assert everything.todos == []
    assert [(t.id, t.source, t.can_finish, t.next) for t in group.todos] == [
        ("a", "fylgja", True, False),
        ("b", "project-todo-md", False, False),
        ("c", None, True, False),
        ("d", "claude-tasks", False, True),
    ]


# --------------------------------------------------------------------------------------------
# The whole answer
# --------------------------------------------------------------------------------------------


def test_the_answer_has_its_schema_and_never_a_null() -> None:
    picture = now(
        workspaces=[
            ws("1", windows=[win("10", context=[ContextItem(kind="tab")])], sessions=["claude:a"]),
            ws("2", focused=True),
        ],
        sessions=[sess("claude:a")],
        projects=[proj("/work/alpha/main", "alpha", workspace="1")],
    )
    todos = [todo("a", source=None), todo("b", scope=(SCOPE_WORKSPACE, "2"))]
    answer = overview(picture, todos, [item("claude:a"), item("/nothing", 2)])
    body = json.loads(dump_json(answer))
    assert body["schema"] == "fylgja.view.overview/1"
    assert body["generated_at"].startswith("2026-10-05T12:00:00")
    assert list(nulls(body)) == []
    assert "go" not in body["next"][1]  # absent, never null
    assert "label" in body["cards"][0]
    assert Overview.model_validate(body) == answer


def test_a_bare_now_is_an_empty_overview() -> None:
    bare = Now(generated_at=T0, collectors=[], degraded=[])
    body = json.loads(dump_json(overview(bare)))
    assert body == {
        "schema": "fylgja.view.overview/1",
        "generated_at": body["generated_at"],
        "next": [],
        "next_list_id": LIST_ID,
        "cards": [],
        "todo_groups": [
            {"title": "All workspaces", "kind": "all", "empty": "Nothing else open", "todos": []}
        ],
    }
