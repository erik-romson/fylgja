"""The ``day`` scope of the summary service (WP8.2): the inputs assembled for a recorded day, the
cache of a past day (judged by its digest alone) and of today (the TTL), and date validation.

The pure assembly replays step 38's day-replay fixture through the real writer and adds the
sessions and todos of that day (``tests/fixtures/summaries/day-input.json`` is the expected
normalized input). The service tests run a daemon with a fake model runner and a fake clock.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from api_support import BASE_URL, T0, focus_event
from summary_support import (
    SECRET,
    FakeRunner,
    stored,
    story_world,
)
from test_day_replay import history_of, moment, replay

from fylgja import paths
from fylgja.api.app import create_app
from fylgja.api.summary_inputs import day_inputs, find_day_inputs
from fylgja.core.daemon import Daemon
from fylgja.core.summaries import (
    RunOutput,
    SummaryInputs,
    day_bounds,
    day_prompt,
    parse_day,
)
from fylgja.core.writer import Writer
from fylgja.store import db, queries
from fylgja.store.rows import DaySessionRow, TodoRow
from fylgja.testing.fakes import FakeClock

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures"
DAY = "2026-10-05"
DAY_PATH = f"/summaries/day/{DAY}"
STORY = "A morning in alpha went to the login form; the afternoon was spent on a release."
WRITE = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}
LATE_TODO = "check the late import"


@pytest.fixture(autouse=True)
def utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Local time is UTC, so a day starts at the same instant everywhere."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


# --------------------------------------------------------------------------------------------
# Input assembly (pure)
# --------------------------------------------------------------------------------------------


def _todo(text: str, created: str, done: str | None = None) -> TodoRow:
    return TodoRow(
        id=text,
        scope_kind="workspace",
        scope_ref="ws1",
        text=text,
        status="done" if done else "open",
        created_at=moment(created),
        done_at=None if done is None else moment(done),
        source="native",
        source_ref=None,
        link=None,
        updated_at=moment(done or created),
    )


def _session(key: str, project: str | None, one_liner: str | None, seen: str) -> DaySessionRow:
    kind, _, _ = key.partition(":")
    return DaySessionRow(
        session_key=key,
        kind=kind,
        project_path=project,
        workspace="ws1",
        title=None,
        one_liner=one_liner,
        first_seen=moment(seen),
    )


SESSIONS = [
    _session("claude:aaaa", "/code/project-one", "Editing the parser", "2026-10-05T08:00:00Z"),
    _session("claude:bbbb", "/code/project-two", "Waiting for your answer", "2026-10-05T10:20:00Z"),
    _session("codex:cccc", None, None, "2026-10-05T13:00:00Z"),
]
TODOS = [
    _todo("write the parser tests", "2026-10-05T08:10:00Z", done="2026-10-05T11:00:00Z"),
    _todo("review the release notes", "2026-10-04T17:00:00Z", done="2026-10-05T14:30:00Z"),
    _todo("plan tomorrow", "2026-10-05T15:50:00Z"),
]


@pytest.fixture
async def replayed() -> Path:
    spec = json.loads((FIXTURE / "history" / "day-replay" / "events.json").read_text("utf-8"))
    path = paths.database_file()
    writer = Writer(path, clock=FakeClock(moment(spec["now"])))
    await writer.start()
    try:
        await replay(spec, writer)
    finally:
        await writer.stop()
    return path


async def test_the_inputs_of_a_recorded_day_are_the_expected_normalized_input(
    replayed: Path,
) -> None:
    since, until = day_bounds(date(2026, 10, 5))
    history = history_of(replayed, since, until, moment("2026-10-06T08:00:00Z"))
    inputs = day_inputs(date(2026, 10, 5), history, SESSIONS, TODOS, settled=True)
    assert inputs is not None
    expected = json.loads((FIXTURE / "summaries" / "day-input.json").read_text("utf-8"))
    assert dict(inputs.data) == expected
    assert (inputs.scope, inputs.ref, inputs.settled) == ("day", DAY, True)
    assert inputs.activity_at is None
    # a day is its digest: the same picture gives the same digest, a late todo another
    again = day_inputs(date(2026, 10, 5), history, SESSIONS, TODOS, settled=True)
    assert again is not None
    assert again.digest == inputs.digest
    more = day_inputs(
        date(2026, 10, 5),
        history,
        SESSIONS,
        [*TODOS, _todo(LATE_TODO, "2026-10-05T16:30:00Z")],
        settled=True,
    )
    assert more is not None
    assert more.digest != inputs.digest


async def test_a_day_with_nothing_recorded_has_no_inputs(replayed: Path) -> None:
    since, until = day_bounds(date(2026, 10, 4))
    history = history_of(replayed, since, until, moment("2026-10-07T08:00:00Z"))
    assert day_inputs(date(2026, 10, 4), history, [], [], settled=True) is None
    assert day_inputs(date(2026, 10, 4), history, SESSIONS[:1], [], settled=True) is not None


async def test_the_spans_are_capped_and_the_rest_is_counted(replayed: Path) -> None:
    since, until = day_bounds(date(2026, 10, 5))
    history = history_of(replayed, since, until, moment("2026-10-06T08:00:00Z"))
    spans = history.summary.spans
    many = history.__class__(
        history.summary.__class__(
            since=since,
            until=until,
            spans=spans * 20,
            workspaces=history.summary.workspaces,
            projects=history.summary.projects,
            apps=history.summary.apps,
            total_seconds=history.summary.total_seconds,
        ),
        (),
    )
    inputs = day_inputs(date(2026, 10, 5), many, [], [], settled=True)
    assert inputs is not None
    assert len(inputs.data["spans"]) == 150
    assert inputs.data["spans_omitted"] == len(spans) * 20 - 150
    assert "(and " in day_prompt(inputs.data)


def test_the_day_prompt_lists_the_totals_the_spans_the_sessions_and_the_todos() -> None:
    data = SummaryInputs.create(
        "day",
        DAY,
        {
            "date": DAY,
            "total_seconds": 5400,
            "workspaces": [{"workspace": "3", "seconds": 3600}, {"seconds": 1800}],
            "projects": [{"project_name": "alpha", "seconds": 3600}],
            "apps": [{"app": "com.example.app", "seconds": 5400}],
            "worked_on": ["alpha"],
            "spans": [
                {"start": "09:00", "end": "09:30", "minutes": 30, "workspace": "3", "title": "t"}
            ],
            "sessions": [{"agent": "claude", "project_name": "alpha", "one_liner": "Editing it"}],
            "todos_added": [{"text": "write the notes"}],
            "todos_done": [{"text": "send the mail"}],
        },
    ).data
    prompt = day_prompt(data)
    for line in (
        "story of 2026-10-05.",
        "Time at the screen: 1 h 30 min",
        "Per workspace:\n  - 3: 1 h 00 min\n  - (unknown): 30 min",
        "Per project:\n  - alpha: 1 h 00 min",
        "Per app:\n  - com.example.app: 1 h 30 min",
        "Projects Claude was prompted in: alpha",
        "  - 09:00–09:30 (30 min) 3 — t",
        "Agent sessions active that day:\n  - claude, alpha: Editing it",
        "Todos added:\n  - write the notes",
        "Todos finished:\n  - send the mail",
        "one short paragraph",
    ):
        assert line in prompt
    assert "Per app:" not in day_prompt({"date": DAY})


# --------------------------------------------------------------------------------------------
# Date validation
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["2026-10-05", "2024-02-29", "1999-01-01"])
def test_a_day_is_a_local_date(ref: str) -> None:
    assert parse_day(ref) == date.fromisoformat(ref)


@pytest.mark.parametrize(
    "ref", ["", "x", "2026-13-01", "2026-02-30", "2025-02-29", "20261005", "2026-1-5", "today"]
)
def test_anything_else_is_not_a_day(ref: str) -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_day(ref)


def test_a_day_ends_where_the_next_begins() -> None:
    start, stop = day_bounds(date(2026, 10, 5))
    assert (start, stop) == (
        datetime(2026, 10, 5, tzinfo=UTC),
        datetime(2026, 10, 6, tzinfo=UTC),
    )


# --------------------------------------------------------------------------------------------
# The service and the API
# --------------------------------------------------------------------------------------------


def http(daemon: Daemon) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(daemon, secret=SECRET)), base_url=BASE_URL
    )


async def post(api: httpx.AsyncClient, path: str = DAY_PATH) -> httpx.Response:
    return await api.post(path, headers=WRITE, content=b"{}")


def clock_of(daemon: Daemon) -> FakeClock:
    assert isinstance(daemon.clock, FakeClock)
    return daemon.clock


@pytest.fixture
async def runner() -> FakeRunner:
    return FakeRunner(default=STORY)


async def record_focus(daemon: Daemon) -> None:
    daemon.sink("wm").emit(focus_event("wm", "100", "3", T0 - timedelta(hours=3)))
    await daemon.settle()


@pytest.fixture
async def world(runner: FakeRunner) -> AsyncIterator[Daemon]:
    """The summary world, 2026-10-05 12:00 UTC: today is the day that was recorded."""
    async with story_world(runner) as daemon:
        await record_focus(daemon)
        yield daemon


@pytest.fixture
async def past(runner: FakeRunner) -> AsyncIterator[Daemon]:
    """The same world recorded at 12:00, but the daemon runs at 01:00 the next day: 2026-10-05
    is a past day."""
    async with story_world(runner, clock=FakeClock(T0 + timedelta(hours=13))) as daemon:
        await record_focus(daemon)
        yield daemon


async def insert_todo(
    daemon: Daemon, text: str, *, created: datetime, done: datetime | None = None
) -> None:
    """A todo the store takes with its own times (an import that arrives late)."""

    def job(tx: Any) -> None:
        queries.insert_todo(
            tx.connection,
            todo_id=text,
            scope_kind="workspace",
            scope_ref="3",
            text=text,
            status="done" if done else "open",
            created_at=db.to_db_time(created),
            done_at=None if done is None else db.to_db_time(done),
            source="import",
            source_ref=f"late#{text}",
            link=None,
        )

    await daemon.writer.execute(job)


async def late_import(daemon: Daemon, text: str = LATE_TODO) -> None:
    await insert_todo(daemon, text, created=T0 - timedelta(hours=2))


async def test_the_inputs_of_a_day_come_from_the_store(world: Daemon) -> None:
    inputs = await find_day_inputs(world, date(2026, 10, 5))
    assert inputs is not None
    data = inputs.data
    assert data["date"] == DAY
    assert data["spans"][0]["workspace"] == "3"
    assert data["spans"][0]["start"] == "09:00"
    assert data["sessions"][0]["agent"] == "claude"
    assert data["sessions"][0]["project_name"] == "alpha"
    assert data["sessions"][0]["one_liner"]
    assert data["todos_added"] == [{"text": "write the release notes"}]
    assert "todos_done" not in data
    assert inputs.settled is False  # it is today
    assert await find_day_inputs(world, date(2026, 9, 1)) is None  # nothing recorded
    assert await find_day_inputs(world, date(2026, 10, 6)) is None  # has not begun


async def test_a_past_day_is_settled_and_a_todo_done_that_day_is_listed(past: Daemon) -> None:
    await insert_todo(past, "ship it", created=T0 - timedelta(days=2), done=T0 - timedelta(hours=1))
    inputs = await find_day_inputs(past, date(2026, 10, 5))
    assert inputs is not None
    assert inputs.settled is True
    assert inputs.data["sessions"][0]["agent"] == "claude"
    assert inputs.data["todos_done"] == [{"text": "ship it"}]
    assert "todos_added" not in inputs.data
    later = await find_day_inputs(past, date(2026, 10, 6))
    assert later is not None
    assert later.settled is False
    assert "sessions" not in later.data
    assert later.data["todos_added"] == [{"text": "write the release notes"}]


async def test_the_story_of_a_past_day_is_fresh_while_the_digest_holds(
    past: Daemon, runner: FakeRunner
) -> None:
    async with http(past) as api:
        assert (await api.get(DAY_PATH)).json()["state"] == "missing"
        first = await post(api)
        assert first.status_code == 202
        assert first.json()["state"] == "pending"
        await past.summaries.drain()
        assert len(runner.calls) == 1
        assert "story of 2026-10-05." in runner.calls[0].prompt
        assert "Fix the login form" in runner.calls[0].prompt

        # no TTL: much later it is the same text, with no model call
        for waited in (60, 3600):
            await clock_of(past).advance(waited)
            again = await post(api)
            assert again.status_code == 200
            body = again.json()
            assert (body["state"], body["text"]) == ("fresh", STORY)
            assert (await api.get(DAY_PATH)).json()["state"] == "fresh"
    assert len(runner.calls) == 1
    assert [(r["scope_kind"], r["scope_ref"], r["kind"]) for r in stored(past)] == [
        ("day", DAY, "text")
    ]


async def test_a_late_import_regenerates_a_past_day_once_without_waiting_for_the_ttl(
    past: Daemon, runner: FakeRunner
) -> None:
    async with http(past) as api:
        assert (await post(api)).status_code == 202
        await past.summaries.drain()
        assert len(runner.calls) == 1

        await late_import(past)
        stale = (await api.get(DAY_PATH)).json()
        assert (stale["state"], stale["text"]) == ("stale", STORY)

        runner.default = "The day also held a late import."
        again = await post(api)  # seconds after the first story: the TTL does not apply
        assert again.status_code == 202
        await past.summaries.drain()
        assert len(runner.calls) == 2
        assert LATE_TODO in runner.calls[1].prompt
        body = (await api.get(DAY_PATH)).json()
        assert (body["state"], body["text"]) == ("fresh", "The day also held a late import.")

        assert (await post(api)).status_code == 200  # and again it is cached
    assert len(runner.calls) == 2


async def test_a_failed_attempt_on_a_past_day_waits_out_the_ttl(
    past: Daemon, runner: FakeRunner
) -> None:
    runner.script.append(RunOutput(1, ""))
    async with http(past) as api:
        assert (await post(api)).status_code == 202
        await past.summaries.drain()
        failed = await post(api)
        assert failed.status_code == 200
        assert (failed.json()["state"], failed.json()["error_kind"]) == ("failed", "nonzero_exit")
        assert len(runner.calls) == 1

        await clock_of(past).advance(301)
        assert (await post(api)).status_code == 202
        await past.summaries.drain()
        assert len(runner.calls) == 2
        assert (await api.get(DAY_PATH)).json()["state"] == "fresh"


async def test_today_follows_the_ttl_rules(world: Daemon, runner: FakeRunner) -> None:
    async with http(world) as api:
        assert (await post(api)).status_code == 202
        await world.summaries.drain()
        await late_import(world)  # inputs changed, inside the TTL: the old text is answered stale
        held = await post(api)
        assert held.status_code == 200
        assert (held.json()["state"], held.json()["text"]) == ("stale", STORY)
        assert len(runner.calls) == 1

        await clock_of(world).advance(301)
        assert (await post(api)).status_code == 202
        await world.summaries.drain()
        assert len(runner.calls) == 2
        assert (await api.get(DAY_PATH)).json()["state"] == "fresh"


async def test_each_day_has_its_own_story(past: Daemon, runner: FakeRunner) -> None:
    async with http(past) as api:
        await post(api)
        await past.summaries.drain()
        assert (await api.get("/summaries/day/2026-10-04")).status_code == 404
        # the focus that carried over midnight is a story of its own on the day after
        after = await post(api, "/summaries/day/2026-10-06")
        assert after.status_code == 202
        await past.summaries.drain()
        assert (await api.get(DAY_PATH)).json()["state"] == "fresh"
        assert (await api.get("/summaries/day/2026-10-06")).json()["state"] == "fresh"
    assert len(runner.calls) == 2
    assert past.summaries.cached("day", DAY) is not past.summaries.cached("day", "2026-10-06")


async def test_a_day_that_is_not_a_date_is_422_and_one_without_activity_is_404(
    world: Daemon, runner: FakeRunner
) -> None:
    async with http(world) as api:
        for ref in ("2026-13-01", "20261005", "yesterday", "2026-1-5"):
            for response in (
                await api.get(f"/summaries/day/{ref}"),
                await post(api, f"/summaries/day/{ref}"),
            ):
                assert response.status_code == 422, ref
                assert response.json() == {"detail": "a day is written YYYY-MM-DD"}
        for ref in ("2026-09-01", "2026-10-06", "2999-01-01"):
            for response in (
                await api.get(f"/summaries/day/{ref}"),
                await post(api, f"/summaries/day/{ref}"),
            ):
                assert response.status_code == 404, ref
                assert response.json() == {"detail": "nothing was recorded on that day"}
    assert runner.calls == []


async def test_the_day_is_409_while_summaries_are_off(runner: FakeRunner) -> None:
    async with story_world(runner, enabled=False) as daemon, http(daemon) as api:
        response = await post(api)
        assert response.status_code == 409
        assert response.json() == {"scope": "day", "ref": DAY, "state": "disabled"}
        assert (await api.get(DAY_PATH)).json()["state"] == "disabled"
    assert runner.calls == []
