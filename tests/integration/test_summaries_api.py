"""``GET`` and ``POST /summaries/{scope}/{ref}`` (WP8.1, ``PLAN-final-plan.md`` §4.2), the cached
``story`` in ``/now`` and ``summary.ready`` on ``/events``, over the ASGI app with a fake model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from api_support import BASE_URL, T0, nulls
from summary_support import (
    ARC,
    FOCUSED,
    NOW,
    PARAGRAPH,
    SECRET,
    SESSION_KEY,
    FakeRunner,
    Stream,
    activity,
    set_activity,
    stored,
    story_world,
    wait_until,
)

from fylgja.api.app import create_app
from fylgja.api.now_models import Now
from fylgja.core.daemon import Daemon
from fylgja.core.summaries import RunOutput
from fylgja.testing.fakes import FakeClock

SESSION_PATH = f"/summaries/session/{SESSION_KEY}"
WORKSPACE_PATH = f"/summaries/workspace/{FOCUSED}"
WRITE = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}
RESPONSE_FIELDS = {
    "scope",
    "ref",
    "state",
    "arc",
    "now",
    "text",
    "generated_at",
    "input_digest",
    "error_kind",
}


def http(daemon: Daemon) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(daemon, secret=SECRET)), base_url=BASE_URL
    )


async def post(api: httpx.AsyncClient, path: str) -> httpx.Response:
    return await api.post(path, headers=WRITE, content=b"{}")


def clock_of(daemon: Daemon) -> FakeClock:
    assert isinstance(daemon.clock, FakeClock)
    return daemon.clock


def checked(response: httpx.Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert set(body) <= RESPONSE_FIELDS
    assert list(nulls(body)) == []
    return body


@pytest.fixture
async def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
async def world(runner: FakeRunner) -> AsyncIterator[Daemon]:
    async with story_world(runner) as daemon:
        yield daemon


# --------------------------------------------------------------------------------------------
# GET
# --------------------------------------------------------------------------------------------


async def test_get_answers_each_state(world: Daemon, runner: FakeRunner) -> None:
    async with http(world) as api:
        missing = checked(await api.get(SESSION_PATH), 200)
        assert missing == {"scope": "session", "ref": SESSION_KEY, "state": "missing"}
        assert checked(await api.get(WORKSPACE_PATH), 200)["state"] == "missing"

        runner.hold()
        checked(await post(api, SESSION_PATH), 202)
        assert checked(await api.get(SESSION_PATH), 200)["state"] == "pending"
        runner.release()
        await world.summaries.drain()

        fresh = checked(await api.get(SESSION_PATH), 200)
        assert fresh["state"] == "fresh"
        assert (fresh["arc"], fresh["now"]) == (ARC, NOW)
        assert fresh["generated_at"] == T0.isoformat().replace("+00:00", "Z")
        assert len(fresh["input_digest"]) == 64
        assert "text" not in fresh
        assert "error_kind" not in fresh

        await set_activity(
            world, {SESSION_KEY: activity(turns=9, last_at=T0 + timedelta(seconds=10))}
        )
        stale = checked(await api.get(SESSION_PATH), 200)
        assert (stale["state"], stale["arc"]) == ("stale", ARC)
        assert stale["input_digest"] == fresh["input_digest"]
    assert len(runner.calls) == 1  # GET never starts a job


async def test_get_answers_failed_with_the_error_kind(world: Daemon, runner: FakeRunner) -> None:
    runner.script.append(RunOutput(1, ""))
    async with http(world) as api:
        checked(await post(api, SESSION_PATH), 202)
        await world.summaries.drain()
        failed = checked(await api.get(SESSION_PATH), 200)
    assert failed == {
        "scope": "session",
        "ref": SESSION_KEY,
        "state": "failed",
        "error_kind": "nonzero_exit",
    }


async def test_get_of_an_unknown_scope_or_ref_is_404(world: Daemon) -> None:
    async with http(world) as api:
        for path in (
            "/summaries/project/x",
            "/summaries/session/claude:nobody",
            "/summaries/workspace/99",
            "/summaries/day/2026-09-01",
        ):
            response = await api.get(path)
            assert response.status_code == 404, path
            assert set(response.json()) == {"detail"}


async def test_an_ended_session_keeps_its_story_as_stale(world: Daemon, runner: FakeRunner) -> None:
    async with http(world) as api:
        checked(await post(api, SESSION_PATH), 202)
        await world.summaries.drain()
        world.sink("agents").emit(_no_sessions(world))
        await world.settle()
        assert SESSION_KEY not in {s["key"] for s in (await api.get("/sessions")).json()}
        ended = checked(await api.get(SESSION_PATH), 200)
        assert (ended["state"], ended["arc"]) == ("stale", ARC)
        gone = await post(api, SESSION_PATH)
        assert gone.status_code == 404
        assert gone.json() == {"detail": "no live session has that key"}


def _no_sessions(daemon: Daemon) -> Any:
    from api_support import session_snapshot

    return session_snapshot("agents", [], daemon.clock.now())


# --------------------------------------------------------------------------------------------
# POST
# --------------------------------------------------------------------------------------------


async def test_post_answers_202_then_200_fresh_without_another_call(
    world: Daemon, runner: FakeRunner
) -> None:
    async with http(world) as api:
        first = checked(await post(api, SESSION_PATH), 202)
        assert first == {"scope": "session", "ref": SESSION_KEY, "state": "pending"}
        await world.summaries.drain()
        second = checked(await post(api, SESSION_PATH), 200)
        assert (second["state"], second["arc"], second["now"]) == ("fresh", ARC, NOW)
        assert checked(await post(api, SESSION_PATH), 200)["state"] == "fresh"
    assert len(runner.calls) == 1


async def test_concurrent_posts_share_one_job(world: Daemon, runner: FakeRunner) -> None:
    runner.hold()
    async with http(world) as api:
        answers = [checked(await post(api, SESSION_PATH), 202) for _ in range(4)]
        assert {a["state"] for a in answers} == {"pending"}
        await wait_until(lambda: len(runner.calls) == 1)
        runner.release()
        await world.summaries.drain()
        assert checked(await post(api, SESSION_PATH), 200)["state"] == "fresh"
    assert len(runner.calls) == 1


async def test_post_inside_the_ttl_answers_the_stale_row_with_200(
    world: Daemon, runner: FakeRunner
) -> None:
    async with http(world) as api:
        checked(await post(api, SESSION_PATH), 202)
        await world.summaries.drain()
        await set_activity(
            world, {SESSION_KEY: activity(turns=9, last_at=T0 + timedelta(seconds=10))}
        )
        stale = checked(await post(api, SESSION_PATH), 200)
        assert (stale["state"], stale["arc"]) == ("stale", ARC)
        await clock_of(world).advance(300)
        regenerating = checked(await post(api, SESSION_PATH), 202)
        assert (regenerating["state"], regenerating["arc"]) == ("pending", ARC)
        await world.summaries.drain()
    assert len(runner.calls) == 2


async def test_post_for_a_workspace_writes_its_paragraph(world: Daemon, runner: FakeRunner) -> None:
    runner.default = PARAGRAPH
    async with http(world) as api:
        checked(await post(api, WORKSPACE_PATH), 202)
        await world.summaries.drain()
        fresh = checked(await api.get(WORKSPACE_PATH), 200)
    assert (fresh["state"], fresh["text"]) == ("fresh", PARAGRAPH)
    assert "arc" not in fresh
    assert "now" not in fresh


async def test_post_while_disabled_is_409_with_disabled(runner: FakeRunner) -> None:
    async with story_world(runner, enabled=False) as daemon, http(daemon) as api:
        for scope, ref in (("session", SESSION_KEY), ("workspace", FOCUSED), ("day", "x")):
            response = await post(api, f"/summaries/{scope}/{ref}")
            assert checked(response, 409) == {"scope": scope, "ref": ref, "state": "disabled"}
        assert checked(await api.get(SESSION_PATH), 200)["state"] == "disabled"
        assert (await post(api, "/summaries/nonsense/x")).status_code == 404
    assert runner.calls == []


async def test_post_of_an_unknown_scope_ref_or_an_empty_day_is_404(
    world: Daemon, runner: FakeRunner
) -> None:
    async with http(world) as api:
        for path, detail in (
            ("/summaries/project/x", "unknown summary scope (session, workspace or day)"),
            ("/summaries/session/claude:nobody", "no live session has that key"),
            ("/summaries/workspace/99", "no workspace has that name"),
            ("/summaries/day/2026-09-01", "nothing was recorded on that day"),
        ):
            response = await post(api, path)
            assert response.status_code == 404, path
            assert response.json() == {"detail": detail}
    assert runner.calls == []


async def test_the_post_needs_the_bearer_and_an_allowed_origin(
    world: Daemon, runner: FakeRunner
) -> None:
    async with http(world) as api:
        assert (await api.post(SESSION_PATH, content=b"{}")).status_code == 401
        wrong = {**WRITE, "Authorization": "Bearer nope"}
        assert (await api.post(SESSION_PATH, headers=wrong, content=b"{}")).status_code == 401
        foreign = {**WRITE, "Origin": "http://evil.example"}
        assert (await api.post(SESSION_PATH, headers=foreign, content=b"{}")).status_code == 403
        form = {**WRITE, "Content-Type": "text/plain"}
        assert (await api.post(SESSION_PATH, headers=form, content=b"{}")).status_code == 415
        assert not world.summaries.pending("session", SESSION_KEY)
        assert (await api.get(SESSION_PATH)).status_code == 200  # the read needs nothing
        assert checked(await post(api, SESSION_PATH), 202)["state"] == "pending"
        await world.summaries.drain()
    assert len(runner.calls) == 1


# --------------------------------------------------------------------------------------------
# /now and /events
# --------------------------------------------------------------------------------------------


async def test_now_carries_the_cached_stories_only_once_cached(
    world: Daemon, runner: FakeRunner
) -> None:
    async with http(world) as api:
        before = (await api.get("/now")).json()
        assert all("story" not in s for s in before["sessions"])
        assert all("story" not in w for w in before["workspaces"])

        checked(await post(api, SESSION_PATH), 202)
        await world.summaries.drain()
        runner.default = PARAGRAPH
        checked(await post(api, WORKSPACE_PATH), 202)
        await world.summaries.drain()

        now = (await api.get("/now")).json()
        Now.model_validate(now)
        assert list(nulls(now)) == []
        session = next(s for s in now["sessions"] if s["key"] == SESSION_KEY)
        assert session["story"] == {
            "arc": ARC,
            "now": NOW,
            "generated_at": "2026-10-05T12:00:00Z",
            "state": "fresh",
        }
        workspace = next(w for w in now["workspaces"] if w["name"] == FOCUSED)
        assert workspace["story"] == {
            "text": PARAGRAPH,
            "generated_at": "2026-10-05T12:00:00Z",
            "state": "fresh",
        }
        assert all("story" not in w for w in now["workspaces"] if w["name"] != FOCUSED)

        # the picture moves on: the stories say so
        await set_activity(
            world,
            {
                SESSION_KEY: activity(
                    state="waiting_input", turns=9, last_at=T0 + timedelta(seconds=10)
                )
            },
        )
        moved = (await api.get("/now")).json()
        assert next(s for s in moved["sessions"] if s["key"] == SESSION_KEY)["story"]["state"] == (
            "stale"
        )
        assert next(w for w in moved["workspaces"] if w["name"] == FOCUSED)["story"]["state"] == (
            "stale"
        )
        runner.hold()
        await clock_of(world).advance(300)
        checked(await post(api, SESSION_PATH), 202)
        pending = (await api.get("/now")).json()
        story = next(s for s in pending["sessions"] if s["key"] == SESSION_KEY)["story"]
        assert (story["state"], story["arc"]) == ("pending", ARC)
        runner.release()
        await world.summaries.drain()


async def test_a_failure_without_text_puts_no_story_in_now(
    world: Daemon, runner: FakeRunner
) -> None:
    runner.script.append(TimeoutError())
    async with http(world) as api:
        checked(await post(api, SESSION_PATH), 202)
        await world.summaries.drain()
        now = (await api.get("/now")).json()
    assert all("story" not in s for s in now["sessions"])
    assert {r["error_kind"] for r in stored(world)} == {"timeout"}


async def test_summary_ready_is_published_on_the_event_stream(
    world: Daemon, runner: FakeRunner
) -> None:
    runner.hold()
    app = create_app(world, secret=SECRET)
    stream = await Stream(app).open()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as api:
            checked(await post(api, SESSION_PATH), 202)
            await wait_until(lambda: len(runner.calls) == 1)
            runner.release()
            name, data = await stream.event()
            while name != "summary.ready":
                name, data = await stream.event()
            assert data == {"scope": "session", "ref": SESSION_KEY, "state": "fresh"}
            following, _ = await stream.event()
            assert following == "now.changed"  # /now changed with it

            runner.script.append(RunOutput(0, ""))
            checked(await post(api, WORKSPACE_PATH), 202)
            name, data = await stream.event()
            while name != "summary.ready":
                name, data = await stream.event()
            assert data == {"scope": "workspace", "ref": FOCUSED, "state": "failed"}
    finally:
        await stream.close()
        await world.summaries.drain()


async def test_an_app_that_is_gone_is_not_told(world: Daemon, runner: FakeRunner) -> None:
    import gc

    create_app(world, secret=SECRET)  # its hub is collected with it
    gc.collect()
    async with http(world) as api:
        checked(await post(api, SESSION_PATH), 202)
        await world.summaries.drain()
        assert checked(await api.get(SESSION_PATH), 200)["state"] == "fresh"
