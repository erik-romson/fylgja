"""``GET /history`` and ``fylgja history`` (WP5.2) against an in-process daemon.

Synthetic collectors push a morning of focus, an idle period and a few Claude prompt markers
through the daemon's ingest gate, so the spans are the ones its span engine derives and its writer
stores. The API is read through HTTPX's ASGI transport; the CLI runs against a real Uvicorn server
on a free loopback port. The resolver's use of focus time is checked on the same daemon.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from api_support import (
    T0,
    Synthetic,
    Win,
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
from typer.testing import CliRunner

from fylgja import paths
from fylgja.api.app import create_app, server_config
from fylgja.api.routes import history as history_route
from fylgja.cli import client as cli_client
from fylgja.cli.app import app as cli
from fylgja.cli.when import parse_when
from fylgja.collectors.base import Capability
from fylgja.core.activity import Away, WorkSession
from fylgja.core.daemon import Daemon
from fylgja.core.span_window import WINDOW as SPAN_WINDOW
from fylgja.models import (
    ContextItemEvent,
    ContextItemPayload,
    FocusEvent,
    FocusPayload,
    PresenceEvent,
    PresencePayload,
)
from fylgja.store import db, queries
from fylgja.testing.fakes import FakeFS

DAY = datetime(2026, 10, 5, tzinfo=UTC)


def hm(hours: int, minutes: int = 0) -> datetime:
    return DAY + timedelta(hours=hours, minutes=minutes)


def iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def pres_event(at: datetime, entity: str, state: str) -> PresenceEvent:
    return PresenceEvent(
        source="pres",
        entity_id=entity,
        observed_at=at,
        payload=PresencePayload(state=state),  # type: ignore[arg-type]
    )


def marker(at: datetime, project: str) -> ContextItemEvent:
    return ContextItemEvent(
        source="pres",
        entity_id=f"worked_on:{project}",
        observed_at=at,
        payload=ContextItemPayload(context_kind="worked_on", data={"project": project}),
    )


@pytest.fixture(autouse=True)
def utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Local time is UTC, so a date or a time without an offset means the same everywhere."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def populate(daemon: Daemon) -> None:
    """Window 100 (workspace 3) holds a session in /work/alpha; window 200 (workspace 4) none.

    09:00 focus 100 | 09:30 focus 200 | idle from 09:55 (the event says 10:00) | active 10:10 |
    10:40 focus 100, still open at the daemon's now (12:00).
    """
    wm, agents, pres = daemon.sink("wm"), daemon.sink("agents"), daemon.sink("pres")
    wm.emit(workspace_snapshot("wm", ["3", "4"], T0, focused="3"))
    wm.emit(
        window_snapshot("wm", [Win("100", "3", "Term", 500), Win("200", "4", "Safari", 600)], T0)
    )
    agents.emit(
        session_snapshot(
            "agents", [session("claude", "abc-123", pid=501, cwd="/work/alpha", host_pid=500)], T0
        )
    )
    await daemon.settle()
    assert daemon.view.sessions["claude:abc-123"].window_id == "100"
    wm.emit(focus_event("wm", "100", "3", hm(9)))
    wm.emit(focus_event("wm", "200", "4", hm(9, 30)))
    pres.emit(pres_event(hm(10), "idle", "idle"))
    pres.emit(pres_event(hm(10, 10), "idle", "active"))
    wm.emit(focus_event("wm", "100", "3", hm(10, 40)))
    pres.emit(marker(hm(9, 10), "/work/alpha"))
    pres.emit(marker(hm(9, 20), "/work/alpha"))
    pres.emit(marker(hm(8, 0), "/work/beta"))
    await daemon.settle()


def make_world(database: Path | None = None, port: int | None = None) -> Daemon:
    options: dict[str, Any] = {} if database is None else {"database": database}
    if port is not None:
        options["user_config"] = {"api": {"port": port}}
    return make_daemon(
        windows_collector(),
        sessions_collector(),
        Synthetic("pres", frozenset({Capability.CONTEXT})),
        **options,
    )


@pytest.fixture
async def daemon() -> AsyncIterator[Daemon]:
    world = make_world()
    await world.start()
    await populate(world)
    try:
        yield world
    finally:
        await world.stop()


@pytest.fixture
async def http(daemon: Daemon) -> AsyncIterator[httpx.AsyncClient]:
    async with client(daemon) as session_client:
        yield session_client


async def history(http: httpx.AsyncClient, query: str = "") -> dict[str, Any]:
    response = await http.get("/history" + query)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def brief(span: dict[str, Any]) -> tuple[Any, ...]:
    return (
        span["window_id"],
        span["start"][11:16],
        span["end"][11:16],
        span.get("end_reason"),
        span["seconds"],
        span["open"],
        span["clipped"],
    )


# --- the whole morning -----------------------------------------------------------------------


async def test_history_lists_the_spans_and_their_totals(http: httpx.AsyncClient) -> None:
    body = await history(http, f"?since={iso(hm(0))}&until={iso(hm(12))}")
    assert body["schema"] == "fylgja.history/1"
    assert (body["since"], body["until"], body["generated_at"]) == (
        iso(hm(0)),
        iso(hm(12)),
        iso(T0),
    )
    assert [brief(s) for s in body["spans"]] == [
        ("100", "09:00", "09:30", "focus", 1800.0, False, False),
        ("200", "09:30", "09:55", "idle", 1500.0, False, False),
        ("200", "10:10", "10:40", "focus", 1800.0, False, False),
        ("100", "10:40", "12:00", None, 4800.0, True, False),
    ]
    assert body["total_seconds"] == 9900.0
    assert body["workspaces"] == [
        {"workspace": "3", "seconds": 6600.0},
        {"workspace": "4", "seconds": 3300.0},
    ]
    assert body["apps"] == [{"seconds": 9900.0}]  # the focus events carry no app
    assert body["spans_truncated"] is False


async def test_spans_are_attributed_to_the_project_and_session_through_the_resolver(
    http: httpx.AsyncClient,
) -> None:
    body = await history(http, f"?since={iso(hm(0))}&until={iso(hm(12))}")
    by_window = {s["window_id"]: s for s in body["spans"]}
    assert by_window["100"]["project_path"] == "/work/alpha"
    assert by_window["100"]["session_key"] == "claude:abc-123"
    assert "project_path" not in by_window["200"]  # no session is placed in that window
    assert body["projects"] == [
        {"project_path": "/work/alpha", "name": "alpha", "seconds": 6600.0},
        {"seconds": 3300.0},
    ]


def folder_item(
    window_id: str, kind: str, fields: dict[str, Any], at: datetime
) -> ContextItemEvent:
    """A context item that names the window's folder, as an IDE or terminal source emits it."""
    return ContextItemEvent(
        source="pres",
        entity_id=f"{window_id}:folder",
        observed_at=at,
        payload=ContextItemPayload(
            context_kind=kind,
            data={
                "window_id": window_id,
                "key": "folder",
                "tier": 2,
                "confidence": "high",
                "fields": fields,
            },
        ),
    )


def folder_world(*repositories: str) -> Daemon:
    fs = FakeFS()
    for root in repositories:
        fs.add_dir(f"{root}/.git")
    return make_daemon(
        windows_collector(),
        sessions_collector(),
        Synthetic("pres", frozenset({Capability.CONTEXT})),
        fs=fs,
    )


async def focus_for_ten_minutes(world: Daemon, window_id: str, workspace: str) -> None:
    """Focus ``window_id`` at the daemon's now, then window 200 ten minutes later."""
    wm = world.sink("wm")
    wm.emit(focus_event("wm", window_id, workspace, T0))
    await world.settle()
    clock: Any = world.clock
    await clock.advance(600)
    wm.emit(focus_event("wm", "200", "4", T0 + timedelta(seconds=600)))
    await world.settle()


async def history_of(world: Daemon) -> dict[str, Any]:
    async with client(world) as http:
        return await history(http, f"?since={iso(T0)}&until={iso(T0 + timedelta(seconds=600))}")


async def test_ide_focus_time_is_attributed_to_the_git_root_of_the_ide_project() -> None:
    world = folder_world("/work/alpha")
    await world.start()
    try:
        wm, ctx = world.sink("wm"), world.sink("pres")
        wm.emit(workspace_snapshot("wm", ["3", "4"], T0, focused="3"))
        ide = Win("300", "3", "PyCharm", 800, bundle="com.jetbrains.pycharm")
        wm.emit(window_snapshot("wm", [ide, Win("200", "4", "Safari", 600)], T0))
        ctx.emit(folder_item("300", "ide_project", {"path": "/work/alpha/src"}, T0))
        await world.settle()
        await focus_for_ten_minutes(world, "300", "3")
        body = await history_of(world)
    finally:
        await world.stop()
    span = next(s for s in body["spans"] if s["window_id"] == "300")
    assert span["project_path"] == "/work/alpha"  # the git root, not the IDE's subdirectory
    assert "session_key" not in span  # no session is placed in an IDE window
    assert body["projects"][0]["project_path"] == "/work/alpha"
    assert body["projects"][0]["seconds"] == 600.0


async def test_a_terminal_folder_that_arrives_after_the_focus_is_filled_in_at_close() -> None:
    world = folder_world("/work/beta")
    await world.start()
    try:
        wm, ctx = world.sink("wm"), world.sink("pres")
        wm.emit(workspace_snapshot("wm", ["3", "4"], T0, focused="3"))
        wm.emit(
            window_snapshot("wm", [Win("100", "3", "Term", 500), Win("200", "4", "S", 600)], T0)
        )
        await world.settle()
        wm.emit(focus_event("wm", "100", "3", T0))
        await world.settle()
        # the second tier lands a moment after the focus event
        ctx.emit(folder_item("100", "cmux_tab", {"cwd": "/work/beta/docs"}, T0))
        await world.settle()
        clock: Any = world.clock
        await clock.advance(600)
        wm.emit(focus_event("wm", "200", "4", T0 + timedelta(seconds=600)))
        await world.settle()
        body = await history_of(world)
    finally:
        await world.stop()
    span = next(s for s in body["spans"] if s["window_id"] == "100")
    assert span["project_path"] == "/work/beta"
    assert body["projects"][0]["project_path"] == "/work/beta"


async def test_a_session_in_the_window_wins_over_the_window_folder() -> None:
    world = folder_world("/work/alpha", "/work/beta")
    await world.start()
    try:
        wm, agents, ctx = world.sink("wm"), world.sink("agents"), world.sink("pres")
        wm.emit(workspace_snapshot("wm", ["3", "4"], T0, focused="3"))
        wm.emit(
            window_snapshot("wm", [Win("100", "3", "Term", 500), Win("200", "4", "S", 600)], T0)
        )
        agents.emit(
            session_snapshot(
                "agents",
                [session("claude", "abc-123", pid=501, cwd="/work/alpha", host_pid=500)],
                T0,
            )
        )
        ctx.emit(folder_item("100", "cmux_tab", {"cwd": "/work/beta"}, T0))
        await world.settle()
        assert world.view.sessions["claude:abc-123"].window_id == "100"
        assert world.window_folders.folder_of("100") == "/work/beta"
        await focus_for_ten_minutes(world, "100", "3")
        body = await history_of(world)
    finally:
        await world.stop()
    span = next(s for s in body["spans"] if s["window_id"] == "100")
    assert (span["project_path"], span["session_key"]) == ("/work/alpha", "claude:abc-123")
    assert [p.get("project_path") for p in body["projects"]] == ["/work/alpha"]


async def test_the_answer_follows_the_serialization_rule_and_is_not_cached(
    http: httpx.AsyncClient,
) -> None:
    response = await http.get(f"/history?since={iso(hm(0))}&until={iso(hm(12))}")
    assert list(nulls(response.json())) == []
    assert response.headers["cache-control"] == "no-store"


async def test_the_prompt_history_is_a_secondary_signal_of_worked_on_projects(
    http: httpx.AsyncClient,
) -> None:
    body = await history(http, f"?since={iso(hm(9))}&until={iso(hm(12))}")
    assert body["worked_on"] == [
        {"project_path": "/work/alpha", "name": "alpha", "markers": 2, "last_at": iso(hm(9, 20))}
    ]
    earlier = await history(http, f"?since={iso(hm(0))}&until={iso(hm(12))}")
    assert [(w["name"], w["markers"]) for w in earlier["worked_on"]] == [("alpha", 2), ("beta", 1)]
    # a marker never changes a span or a total
    assert earlier["total_seconds"] == 9900.0


async def test_a_suspended_machine_is_not_focus_time() -> None:
    world = make_daemon(
        windows_collector(),
        sessions_collector(),
        Synthetic("pres", frozenset({Capability.CONTEXT})),
        tick=30,
    )
    await world.start()
    try:
        wm = world.sink("wm")
        wm.emit(workspace_snapshot("wm", ["3"], T0, focused="3"))
        wm.emit(window_snapshot("wm", [Win("100", "3", "Term", 500)], T0))
        await world.settle()
        wm.emit(focus_event("wm", "100", "3", T0))
        await world.settle()
        clock: Any = world.clock
        await clock.advance(60)
        clock.suspend(3600)  # the machine sleeps for an hour
        await clock.advance(30)  # the first tick after the jump reopens the span
        await clock.advance(30)
        await world.settle()
        async with client(world) as http:
            body = await history(http, f"?since={iso(T0)}&until={iso(T0 + timedelta(hours=2))}")
    finally:
        await world.stop()
    spans = body["spans"]
    assert [brief(s) for s in spans] == [
        ("100", "12:00", "12:01", "idle", 60.0, False, False),
        ("100", "13:01", "13:02", None, 30.0, True, False),
    ]
    assert spans[1]["start"] == iso(T0 + timedelta(seconds=3690))
    assert body["total_seconds"] == 90.0  # the hour of sleep is not counted


# --- since and until -------------------------------------------------------------------------


async def test_a_span_that_crosses_a_boundary_is_clipped_to_it(http: httpx.AsyncClient) -> None:
    body = await history(http, f"?since={iso(hm(9, 15))}&until={iso(hm(10, 20))}")
    assert [brief(s) for s in body["spans"]] == [
        ("100", "09:15", "09:30", "focus", 900.0, False, True),
        ("200", "09:30", "09:55", "idle", 1500.0, False, False),
        ("200", "10:10", "10:20", "focus", 600.0, False, True),
    ]
    assert body["total_seconds"] == 3000.0
    assert body["workspaces"] == [
        {"workspace": "4", "seconds": 2100.0},
        {"workspace": "3", "seconds": 900.0},
    ]


async def test_a_span_that_only_touches_the_window_is_left_out(http: httpx.AsyncClient) -> None:
    after_first = await history(http, f"?since={iso(hm(9, 30))}&until={iso(hm(9, 31))}")
    assert [s["window_id"] for s in after_first["spans"]] == ["200"]
    before_second = await history(http, f"?since={iso(hm(9))}&until={iso(hm(9, 30))}")
    assert [s["window_id"] for s in before_second["spans"]] == ["100"]
    gap = await history(http, f"?since={iso(hm(9, 55))}&until={iso(hm(10, 10))}")
    assert gap["spans"] == []
    assert (gap["total_seconds"], gap["workspaces"], gap["projects"], gap["apps"]) == (
        0,
        [],
        [],
        [],
    )


async def test_an_open_span_runs_to_now_and_to_the_end_of_an_earlier_window(
    daemon: Daemon, http: httpx.AsyncClient
) -> None:
    ended = await history(http, f"?since={iso(hm(11))}&until={iso(hm(11, 30))}")
    assert [brief(s) for s in ended["spans"]] == [
        ("100", "11:00", "11:30", None, 1800.0, True, True)
    ]
    await daemon.clock.advance(600)  # type: ignore[attr-defined]
    live = await history(http, f"?since={iso(hm(11))}")
    assert [brief(s)[1:] for s in live["spans"]] == [("11:00", "12:10", None, 4200.0, True, True)]
    assert live["until"] == iso(hm(12, 10))  # until defaults to now


async def test_since_defaults_to_the_start_of_the_local_day_and_a_date_is_local_midnight(
    http: httpx.AsyncClient,
) -> None:
    default = await history(http)
    assert (default["since"], default["until"]) == (iso(hm(0)), iso(T0))
    assert default["total_seconds"] == 9900.0
    dated = await history(http, "?since=2026-10-05&until=2026-10-05T10:00:00")
    assert (dated["since"], dated["until"]) == (iso(hm(0)), iso(hm(10)))
    offset = await history(http, "?since=2026-10-05T11:00:00%2B02:00&until=2026-10-05T12:00:00Z")
    assert offset["since"] == iso(hm(9))


@pytest.mark.parametrize(
    ("query", "complaint"),
    [
        ("?since=yesterday", "since is not an ISO 8601"),
        ("?since=2026-10-05T10:00:00Z&until=soon", "until is not an ISO 8601"),
        (f"?since={iso(hm(10))}&until={iso(hm(10))}", "since must be before until"),
        (f"?since={iso(hm(11))}&until={iso(hm(10))}", "since must be before until"),
        ("?since=2020-01-01&until=2026-10-05", "longer than a year"),
    ],
)
async def test_a_window_that_cannot_be_used_is_422(
    http: httpx.AsyncClient, query: str, complaint: str
) -> None:
    response = await http.get("/history" + query)
    assert response.status_code == 422
    assert complaint in response.json()["detail"]


async def test_a_long_span_list_is_cut_but_the_totals_are_not(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(history_route, "MAX_SPANS", 2)
    body = await history(http, f"?since={iso(hm(0))}&until={iso(hm(12))}")
    assert [s["window_id"] for s in body["spans"]] == ["100", "200"]
    assert body["spans_truncated"] is True
    assert body["total_seconds"] == 9900.0


async def test_history_needs_no_authentication(http: httpx.AsyncClient) -> None:
    assert (await http.get("/history")).status_code == 200
    assert (await http.get("/history", headers={"host": "evil.example"})).status_code >= 400


# --- the daemon's own part -------------------------------------------------------------------


async def test_time_in_an_ignored_app_is_counted_for_no_window(daemon: Daemon) -> None:
    """The privacy filter drops the focus event of an ignored app; the span before it ends there
    and nothing about the app is stored."""
    wm = daemon.sink("wm")
    private = FocusEvent(
        source="wm",
        entity_id="focus",
        observed_at=hm(11),
        payload=FocusPayload(
            window_id="300", workspace="4", app_bundle_id="com.1password.1password", title="vault"
        ),
    )
    wm.emit(private)
    await daemon.settle()
    wm.emit(focus_event("wm", "100", "3", hm(11, 30)))
    await daemon.settle()
    async with client(daemon) as http:
        body = await history(http, f"?since={iso(hm(10, 30))}&until={iso(hm(12))}")
    assert [brief(s)[:5] for s in body["spans"]] == [
        ("200", "10:30", "10:40", "focus", 600.0),
        ("100", "10:40", "11:00", "focus", 1200.0),
        ("100", "11:30", "12:00", None, 1800.0),
    ]
    assert '"300"' not in json.dumps(body)
    assert "1password" not in json.dumps(body)


async def test_stopping_the_daemon_closes_the_open_span_with_shutdown(tmp_path: Path) -> None:
    database = tmp_path / "fylgja.db"
    first = make_world(database)
    await first.start()
    await populate(first)
    await first.stop()
    connection = db.connect_reader(database)
    try:
        rows = queries.load_spans(
            connection, since=db.to_db_time(hm(0)), until=db.to_db_time(hm(23))
        )
    finally:
        connection.close()
    assert [(r.window_id, r.ended_at, r.end_reason) for r in rows][-1] == ("100", T0, "shutdown")
    assert all(r.ended_at is not None for r in rows)


async def test_a_daemon_that_died_leaves_a_span_that_the_next_start_closes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "fylgja.db"
    connection = db.open_store(database)
    queries.insert_open_span(
        connection,
        window_id="100",
        workspace="3",
        app_bundle_id=None,
        title=None,
        project_path=None,
        session_key=None,
        started_at=db.to_db_time(hm(8)),
    )
    queries.insert_event(
        connection,
        dedup_key="k",
        source="wm",
        record_type="event",
        entity_kind="focus",
        entity_id="focus",
        observed_at=db.to_db_time(hm(8, 45)),
        received_at=db.to_db_time(hm(8, 45)),
        payload_json="{}",
    )
    connection.close()
    world = make_world(database)
    await world.start()
    try:
        async with client(world) as http:
            body = await history(http, f"?since={iso(hm(0))}&until={iso(hm(12))}")
    finally:
        await world.stop()
    assert [brief(s) for s in body["spans"]] == [
        ("100", "08:00", "08:45", "shutdown", 2700.0, False, False)
    ]


async def test_the_span_window_matches_the_store_after_a_day_of_focus(daemon: Daemon) -> None:
    wm, pres = daemon.sink("wm"), daemon.sink("pres")
    pres.emit(pres_event(hm(11), "lock", "locked"))
    pres.emit(pres_event(hm(11, 5), "lock", "active"))
    wm.emit(focus_event("wm", "200", "4", hm(11, 10)))
    wm.emit(focus_event("wm", "100", "3", hm(11, 10)))  # window 200 had no time: dropped
    wm.emit(focus_event("wm", "200", "4", hm(11, 30)))
    await daemon.settle()
    now = daemon.clock.now()
    held = daemon.span_window.view(now)
    connection = db.connect_reader(daemon.writer.database)
    try:
        stored = queries.load_spans(
            connection,
            since=db.to_db_time(now - SPAN_WINDOW),
            until=db.to_db_time(now + timedelta(days=1)),
        )
    finally:
        connection.close()
    assert held == tuple(stored)
    assert [(s.window_id, s.started_at, s.end_reason) for s in held][3:] == [
        ("100", hm(10, 40), "lock"),
        ("100", hm(11, 5), "focus"),
        ("100", hm(11, 10), "focus"),
        ("200", hm(11, 30), None),
    ]
    assert held[0].project_path == "/work/alpha"


async def test_working_time_comes_from_the_span_window_and_freezes_while_away(
    daemon: Daemon,
) -> None:
    working = daemon.activity()
    assert working.working
    assert working.sessions == (WorkSession(hm(9), hm(12), 9900.0),)  # 15 idle minutes: one
    assert working.current == working.sessions[0]
    assert working.previous is None
    assert working.away == Away(since=hm(9, 55), until=hm(10, 10))
    assert working.active_since(hm(10, 40)) == 4800.0

    daemon.sink("pres").emit(pres_event(hm(12), "idle", "idle"))  # idle since 11:55
    await daemon.settle()
    clock: Any = daemon.clock
    await clock.advance(40 * 60)
    away = daemon.activity()
    assert not away.working
    assert away.away == Away(since=hm(11, 55), until=None)
    assert away.current is None  # 45 minutes away: past the session break
    assert away.previous == WorkSession(hm(9), hm(11, 55), 9600.0)
    assert away.active_since(hm(10, 40)) == 4500.0
    await clock.advance(600)
    assert daemon.activity().active_since(hm(10, 40)) == 4500.0  # nothing moves while away


async def test_the_resolver_places_a_project_by_its_focus_time(daemon: Daemon) -> None:
    wm, agents = daemon.sink("wm"), daemon.sink("agents")
    windows = [
        Win("100", "3", "Term", 500),
        Win("101", "5", "Term", 700),
        Win("200", "4", "Safari", 600),
    ]
    wm.emit(workspace_snapshot("wm", ["3", "4", "5"], T0, focused="3"))
    wm.emit(window_snapshot("wm", windows, T0))
    agents.emit(
        session_snapshot(
            "agents",
            [
                session("claude", "abc-123", pid=501, cwd="/work/alpha", host_pid=500),
                session("claude", "def-456", pid=701, cwd="/work/alpha", host_pid=700),
            ],
            T0,
        )
    )
    await daemon.settle()
    assert daemon.resolution is not None
    assert await daemon.focus_time() == {"/work/alpha": {"3": 6600.0}}
    before = daemon.resolution.projects["/work/alpha"]
    assert (before.via, before.workspace, before.confidence) == ("focus-time", "3", "high")

    # from 11:00 the project is worked on in workspace 5: 3600 s there against 3000 s in
    # workspace 3, though both hold one of its windows (by window count that is a tie)
    wm.emit(focus_event("wm", "101", "5", hm(11)))
    await daemon.settle()
    assert await daemon.focus_time() == {"/work/alpha": {"3": 3000.0, "5": 3600.0}}
    changed = [*windows[:2], Win("200", "4", "Safari", 600, title="a new title")]
    wm.emit(window_snapshot("wm", changed, T0 + timedelta(seconds=1)))
    await daemon.settle()
    placed = daemon.resolution.projects["/work/alpha"]
    assert (placed.via, placed.workspace, placed.confidence) == ("focus-time", "5", "medium")


# --- the CLI ---------------------------------------------------------------------------------


class Served:
    def __init__(self, daemon: Daemon, port: int) -> None:
        self.daemon = daemon
        self.port = port
        self.runner = CliRunner()

    async def fylgja(self, *args: str, code: int = 0) -> str:
        result = await asyncio.to_thread(self.runner.invoke, cli, list(args))
        assert result.exit_code == code, (args, result.output)
        return str(result.output)


@pytest.fixture
async def served() -> AsyncIterator[Served]:
    port = free_port()
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(f"[api]\nport = {port}\n", encoding="utf-8")
    daemon = make_world(port=port)
    await daemon.start()
    await populate(daemon)
    server = uvicorn.Server(server_config(create_app(daemon, secret="s" * 40), daemon.config))
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - Uvicorn offers no readiness event
        await asyncio.sleep(0.01)
    try:
        yield Served(daemon, port)
    finally:
        server.should_exit = True
        await task
        await daemon.stop()


WINDOW = ("--since", "2026-10-05T00:00:00Z", "--until", "2026-10-05T12:00:00Z")


async def test_fylgja_history_prints_the_totals_and_the_spans(served: Served) -> None:
    out = await served.fylgja("history", *WINDOW)
    assert "2026-10-05 00:00 to 2026-10-05 12:00: 2h45m of focus" in out
    for part in ("WORKSPACE", "PROJECT", "APP", "WHEN", "alpha", "1h50m", "55m", "100%"):
        assert part in out
    assert "10:40:00-12:00:00+" in out  # the open span
    assert "idle" in out
    assert "worked on" in out
    assert out.index("WORKSPACE") < out.index("PROJECT") < out.index("APP") < out.index("WHEN")


async def test_fylgja_history_json_is_the_api_answer(served: Served) -> None:
    payload = json.loads(await served.fylgja("history", *WINDOW, "--json"))
    over_http = await asyncio.to_thread(
        cli_client.get_json, f"/history?since={iso(hm(0))}&until={iso(hm(12))}"
    )
    assert payload == over_http
    assert payload["schema"] == "fylgja.history/1"


async def test_fylgja_history_can_leave_the_spans_out(served: Served) -> None:
    out = await served.fylgja("history", *WINDOW, "--no-spans")
    assert "WORKSPACE" in out
    assert "WHEN" not in out


async def test_fylgja_history_says_so_when_nothing_was_focused(served: Served) -> None:
    out = await served.fylgja(
        "history", "--since", "2026-10-04T00:00:00Z", "--until", "2026-10-04T12:00:00Z"
    )
    assert "no focus spans in this window" in out


async def test_fylgja_history_reports_a_bad_time_and_a_bad_window(served: Served) -> None:
    out = await served.fylgja("history", "--since", "someday", code=2)
    assert "cannot read 'someday'" in out
    out = await served.fylgja("history", "--since", "2026-10-06T00:00:00Z", code=1)
    assert "422" in out


async def test_fylgja_history_exits_3_when_the_daemon_is_down() -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(f"[api]\nport = {free_port()}\n", encoding="utf-8")
    result = await asyncio.to_thread(CliRunner().invoke, cli, ["history"])
    assert result.exit_code == 3
    assert "daemon not running" in result.output


# --- the times the command line takes --------------------------------------------------------


NOW = datetime(2026, 10, 5, 15, 30, 20, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("today", datetime(2026, 10, 5, 0, 0, tzinfo=UTC)),
        ("Yesterday", datetime(2026, 10, 4, 0, 0, tzinfo=UTC)),
        ("2h", datetime(2026, 10, 5, 13, 30, 20, tzinfo=UTC)),
        ("90m", datetime(2026, 10, 5, 14, 0, 20, tzinfo=UTC)),
        ("3d", datetime(2026, 10, 2, 15, 30, 20, tzinfo=UTC)),
        ("1w", datetime(2026, 9, 28, 15, 30, 20, tzinfo=UTC)),
        ("30s", datetime(2026, 10, 5, 15, 29, 50, tzinfo=UTC)),
        ("2026-10-01", datetime(2026, 10, 1, 0, 0, tzinfo=UTC)),
        ("2026-10-01T08:15:00", datetime(2026, 10, 1, 8, 15, tzinfo=UTC)),
        ("2026-10-01T08:15:00+02:00", datetime(2026, 10, 1, 6, 15, tzinfo=UTC)),
    ],
)
def test_the_command_line_takes_words_counts_and_iso_times(text: str, expected: datetime) -> None:
    assert parse_when(text, NOW) == expected


def test_a_time_that_is_none_of_the_forms_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot read 'last tuesday'"):
        parse_when("last tuesday", NOW)
    assert parse_when("today").tzinfo is not None  # the real clock is the default
