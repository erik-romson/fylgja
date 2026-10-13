"""``build_items``: navigation items from a hand-built ``/now`` (``docs/decisions/0051``)."""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from fylgja.api.item_inputs import build_items
from fylgja.api.now_models import (
    ContextItem,
    LabelSource,
    Now,
    Project,
    PullRequest,
    Session,
    SessionState,
    Todo,
    Waiting,
    Window,
    Workspace,
)
from fylgja.config import ItemsConfig
from fylgja.core.items import GoTarget, Item
from fylgja.core.waiting import start_of_day
from fylgja.store.rows import SpanRow

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CONFIG = ItemsConfig(
    ticket_prefixes=["CBS"], default_ranker="workspace", work_session_break_minutes=30
)
ALPHA = "/work/alpha/main"
BETA = "/work/beta/main"


def window(
    window_id: str,
    *,
    app: str = "PyCharm",
    bundle: str | None = "com.example.ide",
    title: str | None = None,
    kinds: Sequence[str] = (),
    data: dict[str, Any] | None = None,
) -> Window:
    context = [ContextItem(kind=kind, data=data) for kind in kinds]
    return Window(id=window_id, app=app, bundle_id=bundle, title=title, context=context or None)


def workspace(
    name: str,
    *,
    windows: Sequence[Window] = (),
    sessions: Sequence[str] = (),
    todos: Sequence[Todo] = (),
) -> Workspace:
    return Workspace(
        name=name,
        label=name,
        label_source=LabelSource.NONE,
        one_liner="",
        windows=list(windows),
        projects=[],
        sessions=list(sessions),
        todos=list(todos) or None,
    )


def session(
    key: str,
    path: str | None,
    *,
    one_liner: str | None = None,
    branch: str | None = None,
    children: Sequence[Session] = (),
) -> Session:
    kind, _, native = key.partition(":")
    return Session(
        key=key,
        kind=kind,
        native_id=native,
        state=SessionState.WORKING,
        project_path=path,
        one_liner=one_liner,
        branch=branch,
        children=list(children) or None,
    )


def project(path: str, **fields: Any) -> Project:
    return Project(path=path, name=path.rsplit("/", 1)[-1], sessions=[], **fields)


def span(
    window_id: str | None,
    *,
    start: int,
    end: int | None,
    path: str | None = None,
    bundle: str | None = None,
    session_key: str | None = None,
    title: str | None = None,
) -> SpanRow:
    """A span from ``start`` to ``end`` minutes before T0 (``end=None``: still open)."""
    return SpanRow(
        window_id=window_id,
        workspace="1",
        app_bundle_id=bundle,
        title=title,
        project_path=path,
        session_key=session_key,
        started_at=T0 - timedelta(minutes=start),
        ended_at=None if end is None else T0 - timedelta(minutes=end),
        end_reason=None,
    )


def now(
    *,
    workspaces: Sequence[Workspace] = (),
    sessions: Sequence[Session] = (),
    projects: Sequence[Project] = (),
    waiting: Sequence[Waiting] = (),
) -> Now:
    return Now(
        generated_at=T0,
        workspaces=list(workspaces),
        sessions=list(sessions),
        projects=list(projects),
        waiting=list(waiting) or None,
        collectors=[],
        degraded=[],
    )


def by_key(items: Sequence[Item]) -> dict[str, Item]:
    return {item.key: item for item in items}


def alpha_picture() -> tuple[Now, dict[str, str]]:
    ide = window("w1", kinds=["ide_project"], title="alpha – source.xml")
    picture = now(
        workspaces=[workspace("1", windows=[ide], sessions=["claude:s1"])],
        sessions=[session("claude:s1", ALPHA, one_liner="Fixing the port mapping test")],
        projects=[
            project(
                ALPHA,
                branch="CBS-2564-fix",
                dirty=True,
                pr=PullRequest(number=123, state="OPEN"),
                group="grp",
            )
        ],
        waiting=[Waiting(kind="input", target="claude:s1", reason="asks")],
    )
    return picture, {"w1": ALPHA}


def test_a_window_and_a_session_in_one_folder_make_one_item() -> None:
    picture, folders = alpha_picture()
    items = build_items(picture, folders, [], CONFIG)
    assert [item.key for item in items] == [f"folder:{ALPHA}"]
    item = items[0]
    assert item.kind == "folder"
    assert item.name == "alpha/main"
    assert item.path == ALPHA
    assert item.group == "grp"
    assert {(p.kind, p.ref, p.role) for p in item.places} == {
        ("window", "w1", "ide"),
        ("session", "claude:s1", "agent"),
    }
    assert all(p.open for p in item.places)
    assert item.go == GoTarget(kind="session", ref="claude:s1")
    assert item.go_by_role == {
        "ide": GoTarget(kind="window", ref="w1"),
        "agent": GoTarget(kind="session", ref="claude:s1"),
    }
    assert item.state == {"input", "dirty", "pr_open"}
    assert len(item.id) == 16


def test_the_summary_is_exact() -> None:
    picture, folders = alpha_picture()
    ide = window("w1", kinds=["ide_project"], data={"file": "source.xml"})
    picture = picture.model_copy(
        update={"workspaces": [workspace("1", windows=[ide], sessions=["claude:s1"])]}
    )
    (item,) = build_items(picture, folders, [], CONFIG)
    assert item.summary == (
        "CBS-2564 · CBS-2564-fix · PR #123 open · dirty · source.xml"
        ' · "Fixing the port mapping test"'
    )


def test_two_folders_make_two_items_named_by_parent_and_folder() -> None:
    picture = now(
        workspaces=[workspace("1", windows=[window("w1"), window("w2")])],
    )
    items = build_items(picture, {"w1": ALPHA, "w2": BETA}, [], CONFIG)
    assert {item.name for item in items} == {"alpha/main", "beta/main"}


def test_a_window_without_a_folder_makes_an_app_item() -> None:
    chrome = window("w3", app="Google Chrome", bundle="com.example.browser")
    picture = now(workspaces=[workspace("1", windows=[chrome])])
    (item,) = build_items(picture, {}, [], CONFIG)
    assert item.key == "app:com.example.browser"
    assert item.kind == "app"
    assert item.name == "Google Chrome"
    assert item.path is None
    assert item.go == GoTarget(kind="window", ref="w3")
    assert item.go_by_role == {}


def test_an_app_without_a_bundle_id_is_keyed_by_its_name() -> None:
    picture = now(workspaces=[workspace("1", windows=[window("w4", app="Notes", bundle=None)])])
    (item,) = build_items(picture, {}, [], CONFIG)
    assert item.key == "app:Notes"


def test_a_window_with_neither_app_nor_bundle_is_skipped() -> None:
    bare = Window(id="w9")
    assert build_items(now(workspaces=[workspace("1", windows=[bare])]), {}, [], CONFIG) == ()


def test_a_closed_window_from_spans_is_a_closed_place_and_go_is_none() -> None:
    spans = [span("gone", start=40, end=30, path=ALPHA, title="alpha – a.py")]
    (item,) = build_items(now(), {}, spans, CONFIG)
    assert item.key == f"folder:{ALPHA}"
    (only,) = item.places
    assert (only.ref, only.open, only.role) == ("gone", False, "app")
    assert only.last_focus_at == T0 - timedelta(minutes=30)
    assert item.last_focus_at == T0 - timedelta(minutes=30)
    assert item.go is None
    assert item.go_by_role == {}


def test_a_closed_span_without_a_folder_makes_an_app_item() -> None:
    spans = [span("gone", start=20, end=10, bundle="com.example.app")]
    (item,) = build_items(now(), {}, spans, CONFIG)
    assert item.key == "app:com.example.app"
    assert item.name == "com.example.app"


def test_spans_older_than_the_days_are_not_listed() -> None:
    old = span("gone", start=60 * 24 * 20, end=60 * 24 * 20 - 5, path=ALPHA)
    assert build_items(now(), {}, [old], CONFIG) == ()
    recent = span("gone", start=60 * 24 * 3, end=60 * 24 * 3 - 5, path=ALPHA)
    assert len(build_items(now(), {}, [recent], CONFIG)) == 1
    assert build_items(now(), {}, [recent], CONFIG, days=2) == ()


def test_a_span_of_an_open_window_joins_that_place() -> None:
    picture = now(workspaces=[workspace("1", windows=[window("w1")])])
    spans = [span("w1", start=30, end=20, path=BETA), span("w1", start=10, end=5, path=BETA)]
    (item,) = build_items(picture, {"w1": ALPHA}, spans, CONFIG)
    assert item.key == f"folder:{ALPHA}"
    (only,) = item.places
    assert only.open
    assert only.last_focus_at == T0 - timedelta(minutes=5)


def test_a_span_of_a_session_sets_its_last_focus() -> None:
    picture = now(sessions=[session("claude:s1", ALPHA)])
    spans = [span("gone", start=30, end=20, path=ALPHA, session_key="claude:s1")]
    (item,) = build_items(picture, {}, spans, CONFIG)
    by_ref = {p.ref: p for p in item.places}
    assert by_ref["claude:s1"].last_focus_at == T0 - timedelta(minutes=20)
    assert by_ref["gone"].open is False


def test_tickets_come_from_the_branch_titles_and_folder_name() -> None:
    picture = now(
        workspaces=[workspace("1", windows=[window("w1", title="cbs-77 notes")])],
        sessions=[session("claude:s1", "/work/cbslink/CBS-512", branch="CBS-2564-fix")],
        projects=[project("/work/cbslink/CBS-512", branch="CBS-2564-fix")],
    )
    items = by_key(build_items(picture, {"w1": "/work/cbslink/CBS-512"}, [], CONFIG))
    assert items["folder:/work/cbslink/CBS-512"].tickets == ("CBS-512", "CBS-2564", "CBS-77")


def test_no_prefixes_means_no_tickets() -> None:
    picture, folders = alpha_picture()
    off = ItemsConfig(ticket_prefixes=[], default_ranker="workspace", work_session_break_minutes=30)
    (item,) = build_items(picture, folders, [], off)
    assert item.tickets == ()


def test_seconds_today_sums_closed_and_open_spans_up_to_now() -> None:
    picture = now(workspaces=[workspace("1", windows=[window("w1")])])
    spans = [span("w1", start=2, end=1), span("w1", start=1, end=None)]
    (item,) = build_items(picture, {"w1": ALPHA}, spans, CONFIG)
    midnight = start_of_day(T0)
    expected = sum(
        (T0 - max(T0 - timedelta(minutes=begin), midnight)).total_seconds()
        - (T0 - max(T0 - timedelta(minutes=finish), midnight)).total_seconds()
        for begin, finish in ((2, 1), (1, 0))
    )
    assert item.seconds_today == expected
    assert expected <= 120.0


def test_seconds_today_ignores_what_happened_before_midnight() -> None:
    long_ago = span("gone", start=60 * 24 * 3 + 10, end=60 * 24 * 3, path=ALPHA)
    (item,) = build_items(now(), {}, [long_ago], CONFIG)
    assert item.seconds_today == 0.0


def test_child_sessions_count_and_sessions_without_a_path_do_not() -> None:
    child = session("claude:c1", ALPHA)
    parent = session("claude:p1", ALPHA, children=[child])
    loose = session("claude:l1", None)
    (item,) = build_items(now(sessions=[parent, loose]), {}, [], CONFIG)
    assert {p.ref for p in item.places} == {"claude:p1", "claude:c1"}


def test_state_carries_next_todos_of_the_project_and_the_workspace() -> None:
    nxt = Todo(id="t1", text="x", status="next")
    picture = now(
        workspaces=[workspace("1", windows=[window("w1")], todos=[nxt])],
        projects=[project(BETA, todos=[nxt])],
        sessions=[session("claude:s1", BETA)],
    )
    items = by_key(build_items(picture, {"w1": ALPHA}, [], CONFIG))
    assert "todo_next" in items[f"folder:{ALPHA}"].state
    assert "todo_next" in items[f"folder:{BETA}"].state


def test_a_project_target_waiting_marks_the_folder() -> None:
    picture = now(
        sessions=[session("claude:s1", ALPHA)],
        waiting=[Waiting(kind="build_failed", target=ALPHA, reason="red")],
    )
    (item,) = build_items(picture, {}, [], CONFIG)
    assert item.state == {"build_failed"}
    assert all(not p.waiting for p in item.places)


def test_a_folder_with_a_project_but_no_place_or_focus_is_not_listed() -> None:
    assert build_items(now(projects=[project(ALPHA)]), {}, [], CONFIG) == ()


def test_the_places_are_capped_open_first() -> None:
    windows = [window(f"w{n:02d}") for n in range(10)]
    spans = [span(f"old{n}", start=100 + n, end=90 + n, path=ALPHA) for n in range(5)]
    picture = now(workspaces=[workspace("1", windows=windows)])
    (item,) = build_items(picture, {w.id: ALPHA for w in windows}, spans, CONFIG)
    assert len(item.places) == 8
    assert all(p.open for p in item.places)


def test_the_result_is_identical_for_shuffled_inputs() -> None:
    chrome = window("w3", app="Google Chrome", bundle="com.example.browser")
    ide = window("w1", kinds=["ide_project"])
    term = window("w2", app="Term", bundle="com.example.term", kinds=["terminal_session"])
    sessions = [session("claude:s1", ALPHA, one_liner="one"), session("codex:s2", ALPHA)]
    spans = [
        span("w1", start=50, end=40, path=ALPHA),
        span("w1", start=30, end=20, path=ALPHA),
        span("gone", start=60, end=55, path=BETA, title="a"),
        span("gone", start=60, end=55, path=BETA, title="b"),
        span("c1", start=15, end=10, bundle="com.example.browser", session_key="claude:s1"),
    ]
    waiting = [Waiting(kind="input", target="codex:s2", reason="x")]
    folders = {"w1": ALPHA, "w2": ALPHA}

    def build(seed: int) -> tuple[Item, ...]:
        rng = random.Random(seed)
        first_ws = [chrome, ide]
        second_ws = [term]
        rng.shuffle(first_ws)
        shuffled_sessions = list(sessions)
        rng.shuffle(shuffled_sessions)
        shuffled_spans = list(spans)
        rng.shuffle(shuffled_spans)
        spaces = [
            workspace("2", windows=second_ws),
            workspace("1", windows=first_ws, sessions=["claude:s1", "codex:s2"]),
        ]
        rng.shuffle(spaces)
        picture = now(workspaces=spaces, sessions=shuffled_sessions, waiting=waiting)
        return build_items(
            picture, dict(sorted(folders.items(), reverse=bool(seed % 2))), shuffled_spans, CONFIG
        )

    first = build(0)
    assert {item.key for item in first} == {
        f"folder:{ALPHA}",
        f"folder:{BETA}",
        "app:com.example.browser",
    }
    assert [item.key for item in first] == sorted(item.key for item in first)
    for seed in range(1, 8):
        assert build(seed) == first
