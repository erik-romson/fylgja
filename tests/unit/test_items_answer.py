"""The ``fylgja.items/1`` and ``fylgja.rankers/1`` answers from hand-built rankings
(``docs/api/items.md``): pure builders, no daemon and no HTTP."""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest

from fylgja.api.item_inputs import build_items_answer, build_rankers_answer, item_matches
from fylgja.api.items_models import Items, Rankers
from fylgja.api.now_models import Now, Waiting
from fylgja.collectors.registry import Origin
from fylgja.core.activity import build_activity
from fylgja.core.daemon import RankResult
from fylgja.core.items import GoTarget, Item, Place, item_id
from fylgja.rankers.base import RankerPlugin, RankInput, natural_order
from fylgja.rankers.registry import RegisteredRanker
from fylgja.store.rows import SpanRow

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
ALPHA = "folder:/work/alpha/main"
BETA = "folder:/work/beta/main"
APP = "app:com.example.app"


@pytest.fixture(autouse=True)
def utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


def minutes(value: float) -> datetime:
    return T0 + timedelta(minutes=value)


def place(kind: str, ref: str, *, open: bool = True, last: datetime | None = None) -> Place:
    return Place(
        kind=kind,  # type: ignore[arg-type]
        ref=ref,
        role="agent" if kind == "session" else "app",
        label="claude" if kind == "session" else "Example",
        title=f"title of {ref}",
        workspace="1",
        open=open,
        waiting=False,
        last_focus_at=last,
    )


def item(key: str, places: Sequence[Place], *, last: datetime | None = None) -> Item:
    folder = key.startswith("folder:")
    path = key.removeprefix("folder:") if folder else None
    first = next((p for p in places if p.open), None)
    return Item(
        id=item_id(key),
        key=key,
        kind="folder" if folder else "app",
        name=path.rsplit("/", 2)[-2] + "/main" if path else "Example",
        path=path,
        group=None,
        tickets=(),
        places=tuple(places),
        state=frozenset(),
        last_focus_at=last,
        seconds_today=0.0,
        summary="",
        go=None if first is None else GoTarget(first.kind, first.ref),
        go_by_role={},
    )


def span(start: float, end: float, *, window: str | None = None, **fields: str) -> SpanRow:
    return SpanRow(
        window_id=window,
        workspace="1",
        app_bundle_id=fields.get("bundle"),
        title=None,
        project_path=fields.get("path"),
        session_key=None,
        started_at=minutes(start),
        ended_at=minutes(end),
        end_reason="focus",
    )


def result(items: Sequence[Item], spans: Sequence[SpanRow], *, now: datetime) -> RankResult:
    activity = build_activity(
        spans,
        now=now,
        suspended=True,
        idle_threshold=timedelta(minutes=5),
        session_break=timedelta(minutes=30),
    )
    data = RankInput(now=now, items=tuple(items), activity=activity, workspaces=("1",))
    ranked = natural_order(data)
    by_key = {i.key: i for i in items}
    return RankResult(
        ranker="natural",
        data=data,
        ranked=tuple(ranked),
        items=tuple(by_key[r.key] for r in ranked),
        workspace_order=("1",),
    )


def picture(*waiting: Waiting) -> Now:
    return Now(
        generated_at=T0,
        workspaces=[],
        sessions=[],
        projects=[],
        waiting=list(waiting) or None,
        collectors=[],
        degraded=[],
    )


def world() -> tuple[list[Item], list[SpanRow]]:
    """Alpha (a session and window 10) and an app (window 20) were worked on 08:00–09:00; beta
    only through a closed window's span, by folder, and a second app by bundle id."""
    alpha = item(
        ALPHA,
        [place("session", "claude:s1", last=minutes(-200)), place("window", "10")],
        last=minutes(-200),
    )
    app = item(APP, [place("window", "20")], last=minutes(-190))
    beta = item(BETA, [place("window", "30", open=False)], last=minutes(-185))
    other = item("app:com.example.other", [], last=minutes(-182))
    spans = [
        span(-240, -200, window="10"),
        span(-200, -190, window="20"),
        span(-190, -185, window="99", path="/work/beta/main"),
        span(-185, -182, window="98", bundle="com.example.other"),
        span(-182, -181, window="97", path="/work/gone/main"),  # an item no longer listed
        span(-181, -180),  # no window, no folder, no app
    ]
    return [alpha, app, beta, other], spans


def answer(*waiting: Waiting, now: datetime = T0, **options: object) -> Items:
    items, spans = world()
    return build_items_answer(result(items, spans, now=now), picture(*waiting), **options)  # type: ignore[arg-type]


def test_the_last_session_lists_items_by_window_folder_and_app_in_order_of_first_focus() -> None:
    body = answer()
    assert body.last_session is not None
    assert (body.last_session.start, body.last_session.end) == (minutes(-240), minutes(-180))
    assert body.last_session.items == [
        item_id(ALPHA),
        item_id(APP),
        item_id(BETA),
        item_id("app:com.example.other"),
    ]
    assert body.activity.away_since == minutes(-180)
    assert body.activity.away_until is None
    assert body.activity.working is False


def test_while_away_needs_a_since_in_the_absence_and_a_known_target() -> None:
    body = answer(
        Waiting(kind="input", target="claude:s1", reason="asks", since=minutes(-100)),
        Waiting(kind="permission", target="claude:s1", reason="no time"),
        Waiting(kind="turn_done", target="claude:s9", reason="unknown", since=minutes(-90)),
        Waiting(kind="input", target="/work/alpha/main", reason="before", since=minutes(-190)),
        Waiting(kind="build_failed", target="/work/beta/main", reason="looked", since=minutes(-60)),
        Waiting(kind="unread", target="20", reason="window", since=minutes(-30)),
    )
    # beta was last focused at -185 (before the absence began at -180): it is listed too
    assert [(c.item, c.kind, c.reason) for c in body.while_away] == [
        (item_id(ALPHA), "input", "asks"),
        (item_id(BETA), "build_failed", "looked"),
        (item_id(APP), "unread", "window"),
    ]
    assert body.while_away[0].go is not None
    assert body.while_away[1].go is None  # nothing of beta is open


def test_an_item_focused_after_the_absence_began_is_not_listed() -> None:
    items, spans = world()
    items[0] = item(ALPHA, items[0].places, last=minutes(-10))
    ranking = result(items, spans, now=T0)
    waiting = Waiting(kind="input", target="claude:s1", reason="asks", since=minutes(-100))
    assert build_items_answer(ranking, picture(waiting)).while_away == []


def test_without_an_absence_there_is_no_while_away_and_no_last_session() -> None:
    ranking = result([], [], now=T0)
    waiting = Waiting(kind="input", target="claude:s1", reason="asks", since=minutes(-100))
    body = build_items_answer(ranking, picture(waiting), query="  ", limit=5)
    assert (body.while_away, body.last_session, body.items, body.days) == ([], None, [], [])
    assert (body.total, body.shown, body.query) == (0, 0, "  ")


def test_a_blank_query_filters_nothing() -> None:
    body = answer(query="   ")
    assert (body.shown, body.total) == (4, 4)


def test_item_matches_name_tickets_path_summary_and_titles_only() -> None:
    alpha = world()[0][0]
    assert item_matches(alpha, "alpha/main")
    assert item_matches(alpha, "title of 10")
    assert item_matches(alpha, "/work/alpha")
    assert not item_matches(alpha, "example")  # the window label is not searched


def _registered(name: str, *, loaded: bool, enabled: bool = True) -> RegisteredRanker:
    plugin = RankerPlugin(name, f"{name} order", lambda data, settings: []) if loaded else None
    return RegisteredRanker(
        name=name,
        origin=Origin.BUILT_IN,
        distribution="fylgja",
        version="0.1.0",
        enabled=enabled,
        plugin=plugin,
        settings=plugin.config_model() if plugin else None,
        error=None if loaded or not enabled else "import: ImportError: gone",
    )


def rankers(rank: str | None = None, *, default: str = "beta") -> Rankers:
    return build_rankers_answer(
        [
            _registered("alpha", loaded=True),
            _registered("beta", loaded=True),
            _registered("broken", loaded=False),
            _registered("off", loaded=False, enabled=False),
        ],
        default=default,
        configured_default="beta",
        rank=rank,
    )


def test_the_choices_are_the_loaded_rankers_with_the_default_marked() -> None:
    body = rankers()
    assert [r.name for r in body.rankers] == ["alpha", "beta", "broken", "off"]
    assert [(c.name, c.description, c.default) for c in body.choices] == [
        ("alpha", "alpha order", False),
        ("beta", "beta order", True),
    ]
    assert body.rankers[2].error == "import: ImportError: gone"
    assert body.rankers[3].description is None
    assert (body.selected, body.stale_choice) == ("beta", False)


@pytest.mark.parametrize(
    ("rank", "selected", "stale"),
    [
        ("alpha", "alpha", False),
        ("broken", "beta", True),
        ("off", "beta", True),
        (None, "beta", False),
    ],
)
def test_a_stored_choice_is_selected_or_stale(rank: str | None, selected: str, stale: bool) -> None:
    body = rankers(rank)
    assert (body.selected, body.stale_choice) == (selected, stale)


def test_with_the_floor_as_default_no_choice_is_the_default() -> None:
    body = rankers(default="natural")
    assert not any(choice.default for choice in body.choices)
    assert body.selected == "natural"
