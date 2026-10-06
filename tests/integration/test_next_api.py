"""``GET /next``, ``POST /next/feedback`` and ``/now`` ``next[]`` (WP6.2).

The daemon runs over synthetic collectors on a fake clock; the API is called over ASGI. Every list
the API returned must be in ``suggestions_log`` with its reasons, feedback lands on the right row,
an unknown list is 404, and the feedback endpoint sits behind the mutation guard.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from api_support import BASE_URL, T0
from next_support import (
    FOCUSED,
    SECRET,
    SESSION_KEY,
    TODO_REASON,
    TODO_TEXT,
    WAITING_REASON,
    log_rows,
    world,
)

from fylgja.api.app import create_app
from fylgja.core.daemon import Daemon

JSON = "application/json"
AUTH = {"Authorization": f"Bearer {SECRET}", "Content-Type": JSON}


def http(daemon: Daemon) -> httpx.AsyncClient:
    app = create_app(daemon, secret=SECRET)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


async def get_next(daemon: Daemon) -> dict[str, Any]:
    async with http(daemon) as client:
        response = await client.get("/next")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def rate(daemon: Daemon, body: str, headers: dict[str, str] | None = None) -> httpx.Response:
    async with http(daemon) as client:
        return await client.post(
            "/next/feedback", content=body, headers=AUTH if headers is None else headers
        )


async def test_the_shape_and_the_order_of_the_list() -> None:
    async with world() as daemon:
        body = await get_next(daemon)
    assert set(body) == {"list_id", "items"}
    assert body["list_id"].startswith("nl-")
    first, second = body["items"]
    assert first == {
        "suggestion_id": f"{body['list_id']}.1",
        "kind": "input",
        "target_ref": SESSION_KEY,
        "score": 100.0,
        "reason": WAITING_REASON,
        "rank": 1,
    }
    assert second["kind"] == "todo"
    assert second["reason"] == TODO_REASON
    assert second["score"] == 80.0
    assert (second["rank"], second["suggestion_id"]) == (2, f"{body['list_id']}.2")


async def test_now_carries_the_same_list_and_its_id() -> None:
    async with world() as daemon:
        shown = await get_next(daemon)
        async with http(daemon) as client:
            now = (await client.get("/now")).json()
    assert now["next_list_id"] == shown["list_id"]
    assert now["next"] == shown["items"]
    assert [w["kind"] for w in now["waiting"]] == ["input"]


async def test_every_list_is_logged_with_its_reasons() -> None:
    async with world() as daemon:
        body = await get_next(daemon)
        await daemon.writer.flush()
        rows = log_rows(daemon, body["list_id"])
        assert [r["id"] for r in rows] == [i["suggestion_id"] for i in body["items"]]
        for row, item in zip(rows, body["items"], strict=True):
            assert (row["rank"], row["kind"], row["target_ref"]) == (
                item["rank"],
                item["kind"],
                item["target_ref"],
            )
            assert (row["score"], row["reason"]) == (item["score"], item["reason"])
            assert row["generated_at"] == "2026-10-05T12:00:00.000000Z"
            assert (row["feedback"], row["feedback_at"], row["acted_at"]) == (None, None, None)


async def test_an_unchanged_situation_is_one_list_however_often_it_is_read() -> None:
    async with world() as daemon:
        first = await get_next(daemon)
        again = await get_next(daemon)
        async with http(daemon) as client:
            for _ in range(3):
                await client.get("/now")
        await daemon.writer.flush()
        assert again == first
        assert len(log_rows(daemon)) == 2  # one list of two items


async def test_a_changed_situation_is_a_new_list_and_both_are_kept() -> None:
    async with world() as daemon:
        first = await get_next(daemon)
        await daemon.todos.add(
            "review the plan", scope_kind="workspace", scope_ref=FOCUSED, status="next"
        )
        await daemon.settle()
        second = await get_next(daemon)
        await daemon.writer.flush()
    assert second["list_id"] != first["list_id"]
    assert [i["kind"] for i in second["items"]] == ["input", "todo", "todo"]
    assert len(second["items"]) == 3
    assert {i["reason"] for i in second["items"][1:]} == {
        TODO_REASON,
        "todo marked next in workspace 3: review the plan",
    }
    assert {i["suggestion_id"] for i in first["items"]}.isdisjoint(
        i["suggestion_id"] for i in second["items"]
    )


async def test_the_same_picture_on_the_same_clock_has_the_same_ids() -> None:
    async with world(todo=False) as one:  # a todo's id is random, a session's key is not
        first = await get_next(one)
    async with world(todo=False) as two:
        second = await get_next(two)
    assert first == second
    assert len(first["items"]) == 1


async def test_an_empty_list_has_an_id_and_nothing_to_rate() -> None:
    async with world(waiting=False, todo=False) as daemon:
        body = await get_next(daemon)
        assert body["items"] == []
        assert body["list_id"].startswith("nl-")
        await daemon.writer.flush()
        assert log_rows(daemon) == []
        response = await rate(daemon, f'{{"list_id": "{body["list_id"]}", "rating": "good"}}')
    assert response.status_code == 404


async def test_next_without_any_collector_is_an_empty_list() -> None:
    from api_support import make_daemon

    daemon = make_daemon()
    await daemon.start()
    try:
        body = await get_next(daemon)
        async with http(daemon) as client:
            now = (await client.get("/now")).json()
    finally:
        await daemon.stop()
    assert body["items"] == []
    assert "next" not in now
    assert "next_list_id" not in now


# --- feedback ---------------------------------------------------------------------------------


async def test_feedback_without_a_suggestion_rates_the_top_item_of_that_list() -> None:
    async with world() as daemon:
        body = await get_next(daemon)
        response = await rate(daemon, f'{{"list_id": "{body["list_id"]}", "rating": "good"}}')
        assert response.status_code == 200, response.text
        answer = response.json()
        assert answer["list_id"] == body["list_id"]
        assert answer["suggestion_id"] == body["items"][0]["suggestion_id"]
        assert (answer["rank"], answer["rating"]) == (1, "good")
        assert answer["feedback_at"] == "2026-10-05T12:00:00Z"
        rows = log_rows(daemon, body["list_id"])
    assert [(r["rank"], r["feedback"]) for r in rows] == [(1, "good"), (2, None)]
    assert rows[0]["feedback_at"] == "2026-10-05T12:00:00.000000Z"
    assert rows[1]["feedback_at"] is None


async def test_feedback_with_a_suggestion_id_rates_that_item() -> None:
    async with world() as daemon:
        body = await get_next(daemon)
        second = body["items"][1]["suggestion_id"]
        response = await rate(
            daemon,
            f'{{"list_id": "{body["list_id"]}", "rating": "bad", "suggestion_id": "{second}"}}',
        )
        assert response.status_code == 200, response.text
        assert response.json()["rank"] == 2
        rows = log_rows(daemon, body["list_id"])
    assert [(r["rank"], r["feedback"]) for r in rows] == [(1, None), (2, "bad")]


async def test_feedback_lands_on_its_own_list_only_and_a_second_rating_replaces_the_first() -> None:
    async with world() as daemon:
        old = await get_next(daemon)
        await daemon.todos.add("later", scope_kind="workspace", scope_ref=FOCUSED, status="next")
        await daemon.settle()
        new = await get_next(daemon)
        assert new["list_id"] != old["list_id"]
        await rate(daemon, f'{{"list_id": "{old["list_id"]}", "rating": "bad"}}')
        await rate(daemon, f'{{"list_id": "{old["list_id"]}", "rating": "good"}}')
        old_rows = log_rows(daemon, old["list_id"])
        new_rows = log_rows(daemon, new["list_id"])
    assert [r["feedback"] for r in old_rows] == ["good", None]
    assert [r["feedback"] for r in new_rows] == [None, None, None]


async def test_an_unknown_list_is_404_and_nothing_is_recorded() -> None:
    async with world() as daemon:
        body = await get_next(daemon)
        response = await rate(daemon, '{"list_id": "nl-doesnotexist", "rating": "good"}')
        assert response.status_code == 404
        assert set(response.json()) == {"detail"}
        await daemon.writer.flush()
        assert {r["feedback"] for r in log_rows(daemon, body["list_id"])} == {None}


async def test_a_suggestion_of_another_list_is_404() -> None:
    async with world() as daemon:
        old = await get_next(daemon)
        await daemon.todos.add("later", scope_kind="workspace", scope_ref=FOCUSED, status="next")
        await daemon.settle()
        new = await get_next(daemon)
        foreign = new["items"][0]["suggestion_id"]
        response = await rate(
            daemon,
            f'{{"list_id": "{old["list_id"]}", "rating": "good", "suggestion_id": "{foreign}"}}',
        )
        assert response.status_code == 404
        unknown = await rate(
            daemon,
            f'{{"list_id": "{old["list_id"]}", "rating": "good", "suggestion_id": "nope"}}',
        )
        assert unknown.status_code == 404
        assert {r["feedback"] for r in log_rows(daemon)} == {None}


@pytest.mark.parametrize(
    "body",
    [
        "{}",
        '{"list_id": "nl-x"}',
        '{"rating": "good"}',
        '{"list_id": "nl-x", "rating": "great"}',
        '{"list_id": "nl-x", "rating": "good", "extra": 1}',
        '{"list_id": 7, "rating": "good"}',
        "[1]",
        "not json",
    ],
)
async def test_a_malformed_feedback_body_is_422_and_never_echoed(body: str) -> None:
    async with world() as daemon:
        response = await rate(daemon, body)
    assert response.status_code == 422
    assert "great" not in response.text
    assert "not json" not in response.text


# --- auth -------------------------------------------------------------------------------------


async def test_the_feedback_endpoint_needs_the_bearer_and_a_trusted_origin() -> None:
    async with world() as daemon:
        body = await get_next(daemon)
        payload = f'{{"list_id": "{body["list_id"]}", "rating": "good"}}'
        no_bearer = await rate(daemon, payload, {"Content-Type": JSON})
        wrong = await rate(daemon, payload, {"Authorization": "Bearer nope", "Content-Type": JSON})
        foreign = await rate(daemon, payload, AUTH | {"Origin": "http://evil.example"})
        form = await rate(daemon, payload, AUTH | {"Content-Type": "text/plain"})
        assert (no_bearer.status_code, wrong.status_code) == (401, 401)
        assert foreign.status_code == 403
        assert form.status_code == 415
        await daemon.writer.flush()
        assert {r["feedback"] for r in log_rows(daemon, body["list_id"])} == {None}
        assert (await rate(daemon, payload)).status_code == 200


async def test_reading_the_list_needs_no_authentication() -> None:
    async with world() as daemon, http(daemon) as client:
        assert (await client.get("/next")).status_code == 200


async def test_the_time_of_the_feedback_is_the_clock_of_the_daemon() -> None:
    async with world() as daemon:
        body = await get_next(daemon)
        await daemon.writer.flush()
        assert T0.isoformat().startswith("2026-10-05T12:00")
        response = await rate(daemon, f'{{"list_id": "{body["list_id"]}", "rating": "good"}}')
    assert response.json()["feedback_at"].startswith("2026-10-05T12:00:00")
    assert TODO_TEXT not in response.text
