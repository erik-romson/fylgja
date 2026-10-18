"""``GET /items`` and ``GET /rankers`` over a fixture daemon (``docs/api/items.md``).

Synthetic collectors (``wm`` windows, ``agents`` sessions, ``pres`` presence and context items)
run on a fake clock and a fake filesystem with two git roots: a main checkout and a worktree. The
picture: workspace ``3`` holds an IDE window on the main checkout and the terminal of a Claude
session that works in it; workspace ``2`` holds an IDE window on the worktree. Focus moves from the
main IDE (10 min) to the worktree IDE (5 min) to the terminal. The real built-in rankers load from
the installed entry points; synthetic ones come through the ``ranker_entry_points`` seam.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib.metadata import entry_points
from typing import Any

import httpx
import pytest
from api_support import (
    BASE_URL,
    T0,
    Dist,
    EntryPoint,
    Synthetic,
    Win,
    activity_snapshot,
    client,
    focus_event,
    make_daemon,
    nulls,
    session,
    session_snapshot,
    sessions_collector,
    window_snapshot,
    windows_collector,
    workspace_snapshot,
)
from fastapi import Request
from fastapi.responses import Response
from summary_support import SECRET, FakeRunner, summaries_config

from fylgja.api.app import create_app
from fylgja.api.deps import current_items, item_builder, json_response
from fylgja.api.now import build_now
from fylgja.api.now_models import Now
from fylgja.api.summary_inputs import find_folder_inputs
from fylgja.collectors.base import Capability
from fylgja.collectors.registry import EntryPointLike
from fylgja.core.daemon import Daemon, floor_time
from fylgja.core.items import Item, item_id
from fylgja.core.journal import ThreadKey, thread_id
from fylgja.core.spans import Closed
from fylgja.models import (
    ContextItemEvent,
    ContextItemPayload,
    PresenceEvent,
    PresencePayload,
    ProjectEvent,
    ProjectPayload,
    SessionActivity,
)
from fylgja.rankers.base import Part, Ranked, RankerPlugin, RankInput
from fylgja.store import db, queries
from fylgja.store.rows import SpanRow
from fylgja.testing.fakes import FakeClock, FakeFS

ALPHA = "/work/alpha/main"
WORKTREE = "/work/alpha/CBS-1"
BETA = "/work/beta/main"
SESSION = "claude:abc-123"
ALPHA_ID = item_id("folder:" + ALPHA)
WORKTREE_ID = item_id("folder:" + WORKTREE)
TERM_ID = item_id("app:com.example.app")
BETA_ID = item_id("folder:" + BETA)
BUILT_INS = ("frecency", "recent", "workspace")


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Local time is UTC, so the day groups are the same everywhere."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


# --------------------------------------------------------------------------------------------
# The world
# --------------------------------------------------------------------------------------------


def folder_item(window_id: str, fields: dict[str, Any], when: datetime) -> ContextItemEvent:
    """An IDE context item that names the window's folder."""
    return ContextItemEvent(
        source="pres",
        entity_id=f"{window_id}:folder",
        observed_at=when,
        payload=ContextItemPayload(
            context_kind="ide_project",
            data={
                "window_id": window_id,
                "key": "folder",
                "tier": 2,
                "confidence": "high",
                "fields": fields,
            },
        ),
    )


def presence(when: datetime, state: str) -> PresenceEvent:
    return PresenceEvent(
        source="pres",
        entity_id="idle",
        observed_at=when,
        payload=PresencePayload(state=state),  # type: ignore[arg-type]
    )


def make_world(**options: Any) -> Daemon:
    fs = FakeFS()
    for root in (ALPHA, WORKTREE, BETA):
        fs.add_dir(f"{root}/.git")
    return make_daemon(
        windows_collector(),
        sessions_collector(),
        Synthetic("pres", frozenset({Capability.CONTEXT})),
        fs=fs,
        **options,
    )


async def advance_to(daemon: Daemon, seconds: float) -> None:
    clock = daemon.clock
    assert isinstance(clock, FakeClock)
    await clock.advance((at(seconds) - clock.now()).total_seconds())


async def populate(daemon: Daemon) -> None:
    """The picture of the module docstring; the clock ends at T0 + 960 s, focus on the terminal."""
    wm, agents, pres = daemon.sink("wm"), daemon.sink("agents"), daemon.sink("pres")
    wm.emit(workspace_snapshot("wm", ["1", "2", "3"], T0, focused="3"))
    wm.emit(
        window_snapshot(
            "wm",
            [
                Win("100", "3", "PyCharm", 800, "main.py"),
                Win("101", "3", "Term", 500, "zsh"),
                Win("102", "2", "PyCharm", 801, "CBS-1 notes.md"),
            ],
            T0,
        )
    )
    pres.emit(folder_item("100", {"path": f"{ALPHA}/src"}, T0))
    pres.emit(folder_item("102", {"path": WORKTREE}, T0))
    agents.emit(
        session_snapshot(
            "agents", [session("claude", "abc-123", pid=501, cwd=ALPHA, host_pid=500)], T0
        )
    )
    await daemon.settle()
    assert daemon.view.sessions[SESSION].window_id == "101"
    wm.emit(focus_event("wm", "100", "3", T0))
    await daemon.settle()
    await advance_to(daemon, 600)
    wm.emit(focus_event("wm", "102", "2", at(600)))
    await daemon.settle()
    await advance_to(daemon, 900)
    wm.emit(focus_event("wm", "101", "3", at(900)))
    await daemon.settle()
    await advance_to(daemon, 960)


@asynccontextmanager
async def world(**options: Any) -> AsyncIterator[Daemon]:
    daemon = make_world(**options)
    await daemon.start()
    try:
        await populate(daemon)
        yield daemon
    finally:
        await daemon.stop()


async def get(daemon: Daemon, path: str, status: int = 200) -> Any:
    async with client(daemon) as http:
        response = await http.get(path)
    assert response.status_code == status, response.text
    if status == 200:
        assert response.headers["content-type"] == "application/json"
        assert response.headers["cache-control"] == "no-store"
    return response.json()


async def raw(daemon: Daemon, path: str) -> bytes:
    async with client(daemon) as http:
        response = await http.get(path)
    assert response.status_code == 200, response.text
    return response.content


def ids(body: dict[str, Any]) -> list[str]:
    return [item["id"] for item in body["items"]]


def by_id(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in body["items"]}


# --------------------------------------------------------------------------------------------
# Synthetic rankers through the seam
# --------------------------------------------------------------------------------------------


def _boom(data: RankInput, settings: Any) -> list[Ranked]:
    raise RuntimeError(f"cannot rank {[item.name for item in data.items]}")


def _short(data: RankInput, settings: Any) -> list[Ranked]:
    return [Ranked(item.key, 1.0, (Part("one", 1.0),)) for item in data.items[1:]]


def _nan(data: RankInput, settings: Any) -> list[Ranked]:
    return [Ranked(item.key, float("nan"), (Part("nan", 1.0),)) for item in data.items]


def _bad_workspaces(data: RankInput, ranked: Sequence[Ranked]) -> list[str]:
    return ["nowhere"]


def _ok(data: RankInput, settings: Any) -> list[Ranked]:
    return [Ranked(item.key, 1.0, (Part("one", 1.0),)) for item in data.items]


SYNTHETIC = {
    "boom": RankerPlugin(name="boom", description="Raises", rank=_boom),
    "short": RankerPlugin(name="short", description="Drops an item", rank=_short),
    "nan": RankerPlugin(name="nan", description="Not a number", rank=_nan),
    "lost": RankerPlugin(
        name="lost", description="Loses workspaces", rank=_ok, order_workspaces=_bad_workspaces
    ),
}


def rankers_with(*names: str) -> Callable[[], list[EntryPointLike]]:
    """The installed built-in rankers plus the named synthetic external ones."""
    installed = [
        ep
        for ep in entry_points(group="fylgja.rankers")
        if ep.dist is not None and ep.dist.name == "fylgja"
    ]
    extra: list[EntryPointLike] = [
        EntryPoint(name, f"synthetic.{name}:plugin", Dist("fylgja-synthetic"), SYNTHETIC[name])
        for name in names
    ]
    return lambda: [*installed, *extra]


def enabled(*names: str) -> dict[str, Any]:
    return {"rankers": {name: {"enabled": True} for name in names}}


# --------------------------------------------------------------------------------------------
# /items
# --------------------------------------------------------------------------------------------


async def test_items_answer_with_their_schema_and_no_null() -> None:
    async with world() as daemon:
        body = await get(daemon, "/items")
    assert body["schema"] == "fylgja.items/1"
    assert list(nulls(body)) == []
    assert body["ranker"] == "frecency"  # [items] default_ranker
    assert "ranker_fallback" not in body
    assert body["generated_at"] == iso(at(960))
    assert body["summaries_enabled"] is False
    assert (body["total"], body["shown"]) == (3, 3)
    assert "query" not in body
    assert body["activity"] == {"working": True, "active_seconds_session": 960.0} | {
        "session_started_at": iso(T0)
    }
    assert body["while_away"] == []
    assert "last_session" not in body


async def test_an_ide_window_and_a_session_in_one_folder_are_one_item() -> None:
    async with world() as daemon:
        body = await get(daemon, "/items?rank=workspace")
    alpha = by_id(body)[ALPHA_ID]
    assert (alpha["key"], alpha["kind"], alpha["name"], alpha["path"]) == (
        f"folder:{ALPHA}",
        "folder",
        "alpha/main",
        ALPHA,
    )
    assert [(p["kind"], p["ref"], p["role"]) for p in alpha["places"]] == [
        ("session", SESSION, "agent"),
        ("window", "100", "ide"),
    ]
    assert all(p["open"] for p in alpha["places"])
    assert alpha["go"] == {"kind": "session", "ref": SESSION}
    assert alpha["go_agent"] == {"kind": "session", "ref": SESSION}
    assert alpha["go_ide"] == {"kind": "window", "ref": "100"}
    assert "go_terminal" not in alpha
    # 10 min in the IDE; the minute in the session's terminal counts for the terminal's item,
    # but it is the session's last focus
    assert alpha["seconds_today"] == 600.0
    assert alpha["places"][0]["last_focus_at"] == alpha["last_focus_at"] == iso(at(960))


async def test_two_worktrees_are_two_items_and_the_terminal_is_an_app_item() -> None:
    async with world() as daemon:
        body = await get(daemon, "/items?rank=workspace")
    items = by_id(body)
    assert set(items) == {ALPHA_ID, WORKTREE_ID, TERM_ID}
    worktree = items[WORKTREE_ID]
    assert (worktree["name"], worktree["tickets"]) == ("alpha/CBS-1", ["CBS-1"])
    assert worktree["summary"] == "CBS-1"
    assert worktree["go"] == worktree["go_ide"] == {"kind": "window", "ref": "102"}
    term = items[TERM_ID]
    assert (term["kind"], term["name"]) == ("app", "Term")
    assert "path" not in term


async def test_every_ranker_returns_a_permutation_of_the_same_items() -> None:
    async with world() as daemon:
        orders = {name: ids(await get(daemon, f"/items?rank={name}")) for name in BUILT_INS}
    assert {len(order) for order in orders.values()} == {3}
    assert {frozenset(order) for order in orders.values()} == {
        frozenset({ALPHA_ID, WORKTREE_ID, TERM_ID})
    }
    # workspace: by the natural order of the workspace, then key ("app:" before "folder:")
    assert orders["workspace"] == [WORKTREE_ID, TERM_ID, ALPHA_ID]
    # recent: the terminal and the session's folder hold the latest focus, then the worktree
    assert orders["recent"][-1] == WORKTREE_ID


async def test_the_answer_carries_scores_parts_and_an_explanation() -> None:
    async with world() as daemon:
        body = await get(daemon, "/items?rank=workspace")
    first = body["items"][0]
    assert (first["score"], first["explanation"]) == (2.0, "workspace 2")
    assert first["parts"] == [{"name": "workspace", "value": 2.0}]


@pytest.mark.parametrize(
    "query",
    [
        "rank=nope",
        "rank=natural2",
        "rank=" + "x" * 201,
        "days=0",
        "days=15",
        "limit=0",
        "limit=501",
        "q=" + "x" * 201,
    ],
)
async def test_a_bad_parameter_is_422(query: str) -> None:
    async with world() as daemon:
        body = await get(daemon, f"/items?{query}", status=422)
    if query.startswith("rank="):
        assert body["detail"] == (
            "rank must name a loaded ranker (frecency, recent, workspace) or the natural floor"
        )


async def test_a_disabled_ranker_is_422_too() -> None:
    async with world(user_config={"rankers": {"recent": {"enabled": False}}}) as daemon:
        body = await get(daemon, "/items?rank=recent", status=422)
    assert (
        body["detail"]
        == "rank must name a loaded ranker (frecency, workspace) or the natural floor"
    )


async def test_the_natural_floor_can_always_be_asked_for() -> None:
    async with world() as daemon:
        natural = await get(daemon, "/items?rank=natural")
        workspace = await get(daemon, "/items?rank=workspace")
    assert natural["ranker"] == "natural"
    assert "ranker_fallback" not in natural
    assert natural["items"] == workspace["items"]  # the workspace ranker is the floor


@pytest.mark.parametrize("name", ["boom", "short", "nan", "lost"])
async def test_a_failing_ranker_is_answered_by_the_floor(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    async with world(ranker_entry_points=rankers_with(name), user_config=enabled(name)) as daemon:
        assert name in daemon.loaded_rankers
        body = await get(daemon, f"/items?rank={name}")
        floor = await get(daemon, "/items?rank=natural")
    assert (body["ranker"], body["ranker_fallback"]) == (name, "natural")
    assert body["items"] == floor["items"]
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert f"ranker {name} failed" in logged
    for private in ("alpha/main", ALPHA, "CBS-1", "main.py"):
        assert private not in logged  # logged by name only


async def test_q_keeps_the_matching_items_in_rank_order_and_filters_the_days() -> None:
    async with world() as daemon:
        everything = await get(daemon, "/items?rank=recent")
        found = await get(daemon, "/items?rank=recent&q=cbs-1")
        by_title = await get(daemon, "/items?rank=recent&q=MAIN.PY")
        by_path = await get(daemon, "/items?rank=recent&q=/work/alpha")
        nothing = await get(daemon, "/items?rank=recent&q=absent")
    assert ids(found) == [WORKTREE_ID]
    assert (found["query"], found["total"], found["shown"]) == ("cbs-1", 3, 1)
    assert found["days"] == [{"day": "2026-10-05", "items": [WORKTREE_ID]}]
    assert ids(by_title) == [ALPHA_ID]  # a place title matches
    assert ids(by_path) == [i for i in ids(everything) if i in {ALPHA_ID, WORKTREE_ID}]
    assert (nothing["items"], nothing["days"], nothing["shown"], nothing["total"]) == ([], [], 0, 3)
    assert everything["days"] == [{"day": "2026-10-05", "items": ids(everything)}]


async def test_limit_keeps_the_first_items_and_leaves_the_total() -> None:
    async with world() as daemon:
        everything = await get(daemon, "/items?rank=workspace")
        two = await get(daemon, "/items?rank=workspace&limit=2")
        searched = await get(daemon, "/items?rank=workspace&q=alpha&limit=1")
    assert ids(two) == ids(everything)[:2]
    assert (two["shown"], two["total"]) == (2, 3)
    assert two["days"] == [{"day": "2026-10-05", "items": ids(two)}]
    assert (searched["shown"], searched["total"]) == (1, 3)
    assert ids(searched) == [WORKTREE_ID]


async def test_shown_is_the_number_of_items_returned() -> None:
    async with world() as daemon:
        bodies = [
            await get(daemon, f"/items?{query}")
            for query in ("", "q=alpha", "q=term", "limit=1", "q=nothing")
        ]
    for body in bodies:
        assert body["shown"] == len(body["items"])
        assert body["total"] == 3


async def test_days_group_the_items_by_their_last_focus_newest_day_first() -> None:
    async with world() as daemon:
        two_days_ago = SpanRow(
            window_id="900",
            workspace="1",
            app_bundle_id=None,
            title=None,
            project_path=BETA,
            session_key=None,
            started_at=at(-2 * 86400),
            ended_at=at(-2 * 86400 + 1800),
            end_reason="focus",
        )
        daemon.span_window.apply(Closed(two_days_ago))
        body = await get(daemon, "/items?rank=workspace")
        recent = await get(daemon, "/items?rank=workspace&days=1")
        beta = await get(daemon, "/items?rank=workspace&q=beta")
    assert ids(body)[-1] == BETA_ID  # no open place: after every workspace
    assert body["days"] == [
        {"day": "2026-10-05", "items": [WORKTREE_ID, TERM_ID, ALPHA_ID]},
        {"day": "2026-10-03", "items": [BETA_ID]},
    ]
    closed = by_id(body)[BETA_ID]
    assert closed["places"] == [
        {
            "kind": "window",
            "ref": "900",
            "role": "app",
            "label": "window",
            "workspace": "1",
            "open": False,
            "waiting": False,
            "last_focus_at": iso(at(-2 * 86400 + 1800)),
        }
    ]
    assert "go" not in closed  # nothing open: no button
    assert BETA_ID not in ids(recent)  # outside one day
    assert beta["days"] == [{"day": "2026-10-03", "items": [BETA_ID]}]


async def test_two_calls_within_five_seconds_give_identical_bodies() -> None:
    async with world() as daemon:
        first = await raw(daemon, "/items")
        await advance_to(daemon, 962)
        second = await raw(daemon, "/items")
        await advance_to(daemon, 966)
        third = await raw(daemon, "/items")
    assert first == second
    assert third != first  # a new 5 s step: the open span grew


async def test_the_ranker_gets_the_floored_time() -> None:
    async with world() as daemon:
        await advance_to(daemon, 963)
        body = await get(daemon, "/items")
        result = daemon.ranked("frecency", lambda moment: ((), ()), days=14)
        with pytest.raises(LookupError, match="not loaded"):
            daemon.ranked("nope", lambda moment: ((), ()), days=14)
    assert body["generated_at"] == iso(at(960))
    assert result.now == floor_time(at(963)) == at(960)
    assert result.items  # the memo's answer: the builder above was not called


async def test_while_idle_an_hour_changes_no_order_and_no_score() -> None:
    """Ages are active time. The one wall-time event is the work-session break (30 min): it
    makes the session just left the last one, which frecency's ``last session`` part reads
    (docs/decisions/0050). After that, an hour more changes nothing for any ranker."""
    async with world() as daemon:
        await advance_to(daemon, 1500)
        daemon.sink("pres").emit(presence(at(1500), "idle"))  # idle since 1200
        await daemon.settle()
        idle = {name: await get(daemon, f"/items?rank={name}") for name in BUILT_INS}
        await advance_to(daemon, 1200 + 31 * 60)  # past the work-session break
        before = {name: await get(daemon, f"/items?rank={name}") for name in BUILT_INS}
        await advance_to(daemon, 1200 + 31 * 60 + 3600)
        after = {name: await get(daemon, f"/items?rank={name}") for name in BUILT_INS}
    for name in BUILT_INS:
        assert after[name]["items"] == before[name]["items"], name
        assert after[name]["generated_at"] != before[name]["generated_at"]
    for name in ("workspace", "recent"):
        assert after[name]["items"] == idle[name]["items"], name
    assert idle["frecency"]["activity"]["working"] is False
    assert idle["frecency"]["activity"]["away_since"] == iso(at(1200))
    assert "away_until" not in idle["frecency"]["activity"]
    assert "last_session" not in idle["frecency"]
    assert before["frecency"]["last_session"]["end"] == iso(at(1200))


async def test_while_away_lists_a_session_that_started_waiting_during_an_idle_gap() -> None:
    async with world() as daemon:
        wm, agents, pres = daemon.sink("wm"), daemon.sink("agents"), daemon.sink("pres")
        wm.emit(focus_event("wm", "102", "2", at(960)))  # the worktree, not the session's folder
        await daemon.settle()
        await advance_to(daemon, 1560)
        pres.emit(presence(at(1560), "idle"))  # idle since 1260
        await daemon.settle()
        await advance_to(daemon, 1800)
        waiting = SessionActivity(state="waiting_input", waiting_reason="a question")
        agents.emit(activity_snapshot("agents", {SESSION: waiting}, at(1800)))
        await daemon.settle()
        await advance_to(daemon, 2400)
        pres.emit(presence(at(2400), "active"))  # back: the span on the worktree reopens
        await daemon.settle()
        await advance_to(daemon, 2410)
        bodies = {name: await get(daemon, f"/items?rank={name}") for name in BUILT_INS}
        await advance_to(daemon, 2420)
        wm.emit(focus_event("wm", "100", "3", at(2420)))  # the folder is looked at
        await daemon.settle()
        await advance_to(daemon, 2430)
        looked = await get(daemon, "/items")
    body = bodies["frecency"]
    assert body["activity"]["away_since"] == iso(at(1260))
    assert body["activity"]["away_until"] == iso(at(2400))
    assert body["activity"]["working"] is True
    (change,) = body["while_away"]
    assert (change["item"], change["name"], change["kind"]) == (ALPHA_ID, "alpha/main", "input")
    assert change["since"] == iso(at(1800))
    assert change["go"] == {"kind": "session", "ref": SESSION}
    assert "a question" in change["reason"]
    assert bodies["recent"]["while_away"] == bodies["workspace"]["while_away"] == [change]
    assert ids(body)[0] == ALPHA_ID  # frecency's while-away boost
    assert looked["while_away"] == []


async def test_the_last_work_session_lists_its_items_in_order_of_first_focus() -> None:
    async with world() as daemon:
        await advance_to(daemon, 1500)
        daemon.sink("pres").emit(presence(at(1500), "idle"))  # idle since 1200
        await daemon.settle()
        await advance_to(daemon, 1200 + 45 * 60)
        daemon.sink("pres").emit(presence(at(1200 + 45 * 60), "active"))
        await daemon.settle()
        await advance_to(daemon, 1200 + 46 * 60)
        body = await get(daemon, "/items")
    assert body["last_session"] == {
        "start": iso(T0),
        "end": iso(at(1200)),
        "active_seconds": 1200.0,
        "items": [ALPHA_ID, WORKTREE_ID, TERM_ID],
    }
    assert body["activity"]["session_started_at"] == iso(at(1200 + 45 * 60))
    assert body["activity"]["active_seconds_session"] == 60.0


async def test_current_items_are_the_ranker_input_of_this_moment() -> None:
    """``current_items`` (what the summary routes look items up in) holds the same items as
    ``/items``, sorted by key, and shares the daemon's memo."""
    built: list[int] = []

    async def probe(request: Request) -> Response:
        items = current_items(request)
        built.append(id(items))
        return json_response(json.dumps([item.key for item in items]).encode())

    async with world() as daemon:
        app = create_app(daemon)
        app.add_api_route("/probe", probe)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            keys = (await http.get("/probe")).json()
            again = (await http.get("/probe")).json()
            body = (await http.get("/items")).json()
    assert keys == again == sorted(item["key"] for item in body["items"])
    assert built[0] == built[1]  # the second call is the memo's answer


async def test_items_need_no_authentication_and_are_never_cached() -> None:
    async with world() as daemon, client(daemon) as http:
        response = await http.get("/items")
        rankers = await http.get("/rankers")
    assert response.status_code == rankers.status_code == 200
    assert response.headers["cache-control"] == rankers.headers["cache-control"] == "no-store"


# --------------------------------------------------------------------------------------------
# story: the cached folder summary (docs/decisions/0053)
# --------------------------------------------------------------------------------------------

STORY = "Alpha main gets an email validator for its login form. It is half done."
WRITE = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}


def summarized(runner: FakeRunner, *, enabled: bool = True) -> dict[str, Any]:
    return {"user_config": summaries_config(enabled=enabled), "summary_runner": runner}


def stories(body: dict[str, Any]) -> dict[str, Any]:
    return {item["id"]: item["story"] for item in body["items"] if "story" in item}


async def test_an_item_carries_its_folder_summary_only_once_it_is_cached() -> None:
    runner = FakeRunner(default=STORY)
    async with world(**summarized(runner)) as daemon:
        app = create_app(daemon, secret=SECRET)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            before = (await http.get("/items")).json()
            assert before["summaries_enabled"] is True
            assert stories(before) == {}
            assert runner.calls == []  # a read asks for nothing

            posted = await http.post(f"/summaries/folder/{ALPHA_ID}", headers=WRITE, content=b"{}")
            assert posted.status_code == 202, posted.text
            await daemon.summaries.drain()
            after = (await http.get("/items?rank=workspace")).json()
            assert list(nulls(after)) == []
            fresh = {"text": STORY, "generated_at": iso(at(960)), "state": "fresh"}
            assert stories(after) == {ALPHA_ID: fresh}
            assert stories((await http.get("/items?days=3")).json()) == {ALPHA_ID: fresh}
            assert ids((await http.get("/items?q=VALIDATOR")).json()) == [ALPHA_ID]

            # focus moves and time passes, the folder's content does not change
            daemon.sink("wm").emit(focus_event("wm", "100", "3", at(1000)))
            await daemon.settle()
            await advance_to(daemon, 4600)
            assert stories((await http.get("/items")).json()) == {ALPHA_ID: fresh}

            # the IDE window of alpha shows another file: the story is stale
            daemon.sink("wm").emit(
                window_snapshot(
                    "wm",
                    [
                        Win("100", "3", "PyCharm", 800, "test_main.py"),
                        Win("101", "3", "Term", 500, "zsh"),
                        Win("102", "2", "PyCharm", 801, "CBS-1 notes.md"),
                    ],
                    at(4600),
                )
            )
            await daemon.settle()
            moved = stories((await http.get("/items")).json())
            assert moved == {ALPHA_ID: {**fresh, "state": "stale"}}
    assert len(runner.calls) == 1


async def test_no_story_while_summaries_are_off() -> None:
    runner = FakeRunner(default=STORY)
    async with world(**summarized(runner)) as daemon:
        now = build_now(daemon.state())
        inputs = find_folder_inputs(current_items_of(daemon, now), now, ALPHA_ID)
        assert inputs is not None
        daemon.summaries.request(inputs)
        await daemon.summaries.drain()
        assert daemon.summaries.cached("folder", ALPHA_ID) is not None
    off = FakeRunner(default=STORY)
    async with world(**summarized(off, enabled=False)) as daemon:  # the same database
        assert daemon.summaries.cached("folder", ALPHA_ID) is not None  # cached, not shown
        body = await get(daemon, "/items")
        folder = await get(daemon, f"/summaries/folder/{ALPHA_ID}")
    assert body["summaries_enabled"] is False
    assert stories(body) == {}
    assert folder == {"scope": "folder", "ref": ALPHA_ID, "state": "disabled"}
    assert off.calls == []


def current_items_of(daemon: Daemon, now: Now) -> Sequence[Item]:
    """The items the summary route looks an id up in, for the picture ``now``."""
    return daemon.rank_input(item_builder(daemon, lambda: now, days=14), days=14).items


# --------------------------------------------------------------------------------------------
# ?rank= on /now and /view/overview
# --------------------------------------------------------------------------------------------


def workspace_names(body: dict[str, Any]) -> list[str]:
    return [workspace["name"] for workspace in body["workspaces"]]


def card_names(body: dict[str, Any]) -> list[str]:
    return [card["name"] for card in body["cards"]]


async def test_now_orders_workspaces_by_the_ranker() -> None:
    async with world() as daemon:
        natural = await get(daemon, "/now?rank=workspace")
        ranked = await get(daemon, "/now?rank=frecency")
    assert workspace_names(natural) == ["1", "2", "3"]
    # the focused workspace of the latest working time comes first
    assert workspace_names(ranked) == ["3", "2", "1"]
    assert {k: v for k, v in natural.items() if k != "workspaces"} == {
        k: v for k, v in ranked.items() if k != "workspaces"
    }


async def test_now_without_rank_uses_the_default_ranker() -> None:
    async with world() as daemon:
        bare = await get(daemon, "/now")
        named = await get(daemon, f"/now?rank={daemon.effective_default_ranker}")
        floor = await get(daemon, "/now?rank=natural")
    assert bare == named
    assert workspace_names(bare) == ["3", "2", "1"]
    assert workspace_names(floor) == ["1", "2", "3"]


@pytest.mark.parametrize("path", ["/now", "/view/overview"])
async def test_now_with_an_unknown_rank_is_422(path: str) -> None:
    async with world() as daemon:
        body = await get(daemon, f"{path}?rank=nope", status=422)
    assert "nope" not in json.dumps(body) or "rank must name a loaded ranker" in json.dumps(body)


async def test_overview_view_orders_cards_by_the_ranker() -> None:
    async with world() as daemon:
        natural = await get(daemon, "/view/overview?rank=workspace")
        ranked = await get(daemon, "/view/overview?rank=frecency")
        bare = await get(daemon, "/view/overview")
    assert card_names(natural) == ["2", "3"]
    assert card_names(ranked) == ["3", "2"]
    assert card_names(bare) == card_names(ranked)


# --------------------------------------------------------------------------------------------
# /rankers
# --------------------------------------------------------------------------------------------


async def test_rankers_list_every_discovered_ranker_and_the_defaults() -> None:
    async with world() as daemon:
        body = await get(daemon, "/rankers")
    assert body["schema"] == "fylgja.rankers/1"
    assert list(nulls(body)) == []
    assert (body["default"], body["configured_default"]) == ("frecency", "frecency")
    rows = {row["name"]: row for row in body["rankers"]}
    assert [row["name"] for row in body["rankers"]] == sorted(rows)
    assert set(BUILT_INS) <= set(rows)
    for name in BUILT_INS:
        row = rows[name]
        assert (row["origin"], row["distribution"], row["enabled"], row["loaded"]) == (
            "built-in",
            "fylgja",
            True,
            True,
        )
        assert row["description"]
        assert "error" not in row
    fake = rows["fake_ranker"]
    assert (fake["origin"], fake["enabled"], fake["loaded"]) == ("external", False, False)
    assert "description" not in fake  # never imported


async def test_choices_are_exactly_the_loaded_rankers_with_one_default() -> None:
    async with world() as daemon:
        body = await get(daemon, "/rankers")
    loaded = [row["name"] for row in body["rankers"] if row["loaded"]]
    assert [choice["name"] for choice in body["choices"]] == loaded == list(BUILT_INS)
    assert [choice["name"] for choice in body["choices"] if choice["default"]] == ["frecency"]
    descriptions = {row["name"]: row["description"] for row in body["rankers"] if row["loaded"]}
    assert {c["name"]: c["description"] for c in body["choices"]} == descriptions
    assert (body["selected"], body["stale_choice"]) == ("frecency", False)


async def test_a_stored_choice_of_a_disabled_ranker_is_stale() -> None:
    async with world(user_config={"rankers": {"recent": {"enabled": False}}}) as daemon:
        body = await get(daemon, "/rankers?rank=recent")
    assert (body["selected"], body["stale_choice"]) == (body["default"], True)
    assert body["default"] == "frecency"
    recent = next(row for row in body["rankers"] if row["name"] == "recent")
    assert (recent["enabled"], recent["loaded"]) == (False, False)
    assert "recent" not in [choice["name"] for choice in body["choices"]]


async def test_a_stored_choice_of_a_loaded_ranker_is_selected() -> None:
    async with world() as daemon:
        body = await get(daemon, "/rankers?rank=workspace")
        empty = await get(daemon, "/rankers?rank=")
        unknown = await get(daemon, "/rankers?rank=Not%20A%20Name")
    assert (body["selected"], body["stale_choice"]) == ("workspace", False)
    assert (empty["selected"], empty["stale_choice"]) == ("frecency", False)
    assert (unknown["selected"], unknown["stale_choice"]) == ("frecency", True)


async def test_rankers_without_rank_select_the_default_and_a_long_rank_is_422() -> None:
    async with world() as daemon:
        body = await get(daemon, "/rankers")
        await get(daemon, "/rankers?rank=" + "x" * 201, status=422)
        fits = await get(daemon, "/rankers?rank=" + "x" * 200)
    assert (body["selected"], body["stale_choice"]) == (body["default"], False)
    assert fits["stale_choice"] is True


async def test_a_failing_ranker_is_listed_and_offered_like_any_loaded_one() -> None:
    async with world(
        ranker_entry_points=rankers_with("boom"), user_config=enabled("boom")
    ) as daemon:
        body = await get(daemon, "/rankers?rank=boom")
    boom = next(row for row in body["rankers"] if row["name"] == "boom")
    assert (boom["origin"], boom["distribution"], boom["loaded"]) == (
        "external",
        "fylgja-synthetic",
        True,
    )
    assert (body["selected"], body["stale_choice"]) == ("boom", False)


# --------------------------------------------------------------------------------------------
# The journal field (decision 0057, D6)
# --------------------------------------------------------------------------------------------

ALPHA_THREAD = thread_id(ThreadKey(ALPHA, "main"))


@asynccontextmanager
async def journal_world(*, enabled: bool) -> AsyncIterator[Daemon]:
    """The picture of the module, with a project collector: ALPHA is a checkout on ``main`` with a
    thread row (where you are, five open ``next`` items)."""
    fs = FakeFS()
    for root in (ALPHA, WORKTREE, BETA):
        fs.add_dir(f"{root}/.git")
    daemon = make_daemon(
        windows_collector(),
        sessions_collector(),
        Synthetic("pres", frozenset({Capability.CONTEXT})),
        Synthetic("proj", frozenset({Capability.PROJECT_STATE})),
        user_config={"journal": {"enabled": enabled}},
        fs=fs,
    )
    await daemon.start()
    try:
        await populate(daemon)
        daemon.sink("proj").emit(
            ProjectEvent(
                source="proj",
                entity_id=ALPHA,
                observed_at=daemon.clock.now(),
                payload=ProjectPayload(path=ALPHA, repo_root=ALPHA, branch="main"),
            )
        )
        await daemon.settle()
        if enabled:
            stamp = db.to_db_time(daemon.clock.now() - timedelta(minutes=5))

            def job(tx: Any) -> None:
                queries.put_journal_thread(
                    tx.connection,
                    {
                        "thread_id": ALPHA_THREAD,
                        "repo_root": ALPHA,
                        "branch": "main",
                        "name": "alpha",
                        "first_at": stamp,
                        "last_at": stamp,
                        "watermark": stamp,
                        "where_text": "You are in the middle of the form.",
                        "where_at": stamp,
                        "last_attempt_at": stamp,
                        "error_kind": None,
                        "error_at": None,
                    },
                )
                for seq in range(1, 6):
                    queries.put_journal_item(
                        tx.connection,
                        {
                            "id": f"{seq:016x}",
                            "thread_id": ALPHA_THREAD,
                            "seq": seq,
                            "kind": "next",
                            "text": f"do {seq}",
                            "status": "open",
                            "pinned": 0,
                            "edited": 0,
                            "source": json.dumps(
                                {"kind": "thread", "ref": ALPHA_THREAD, "label": "alpha · main"}
                            ),
                            "created_at": db.to_db_time(
                                daemon.clock.now() - timedelta(hours=1, minutes=-seq)
                            ),
                            "created_entry": None,
                            "closed_at": None,
                            "closed_by": None,
                            "closed_entry": None,
                            "todo_id": None,
                            "how_text": None,
                            "how_digest": None,
                            "how_at": None,
                            "how_error_kind": None,
                        },
                    )

            await daemon.writer.execute(job)
            assert daemon.journal is not None
            await daemon.journal.load()
        yield daemon
    finally:
        await daemon.stop()


async def test_a_checkout_folder_item_carries_the_threads_journal() -> None:
    async with journal_world(enabled=True) as daemon:
        body = await get(daemon, "/items")
    items = by_id(body)
    assert items[ALPHA_ID]["journal"] == {
        "thread": ALPHA_THREAD,
        "where": "You are in the middle of the form.",
        "next": [
            {"id": f"{5:016x}", "text": "do 5"},
            {"id": f"{4:016x}", "text": "do 4"},
            {"id": f"{3:016x}", "text": "do 3"},
        ],
        "next_more": 2,
    }
    assert "journal" not in items[WORKTREE_ID]  # a checkout without a thread row
    assert "journal" not in items[TERM_ID]  # an app
    assert list(nulls(body)) == []


async def test_while_the_journal_is_off_no_item_has_a_journal_field() -> None:
    async with journal_world(enabled=False) as daemon:
        body = await get(daemon, "/items")
    assert body["items"]
    assert all("journal" not in item for item in body["items"])
