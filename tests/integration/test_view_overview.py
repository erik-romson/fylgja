"""``GET /view/overview`` over the fixture daemon (``docs/api/views.md``).

The daemon runs over synthetic collectors on a fake clock (the "what next" world: workspace ``3``
focused, a Claude session in it waiting for input, a todo marked ``next`` there); the API is called
over ASGI. The answer is ``fylgja.view.overview/1`` with a card for the busy workspace, and its Next
part is exactly what ``GET /next`` answers for the same state, without logging a second list.
"""

from __future__ import annotations

from typing import Any

import httpx
from api_support import BASE_URL, nulls
from next_support import FOCUSED, SECRET, SESSION_KEY, TODO_TEXT, log_rows, world

from fylgja.api.app import create_app
from fylgja.core.daemon import Daemon


def http(daemon: Daemon) -> httpx.AsyncClient:
    app = create_app(daemon, secret=SECRET)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


async def get_json(daemon: Daemon, path: str) -> Any:
    async with http(daemon) as client:
        response = await client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def test_the_overview_answers_with_its_schema_and_a_card_for_the_busy_workspace() -> None:
    async with world() as daemon:
        body = await get_json(daemon, "/view/overview")
    assert body["schema"] == "fylgja.view.overview/1"
    assert list(nulls(body)) == []
    assert body["focused_workspace"] == FOCUSED
    (card,) = body["cards"]  # workspaces 1 and 2 are idle: no card
    assert (card["name"], card["focused"], card["windows"]) == (FOCUSED, True, 1)
    assert card["go"] == {"kind": "window", "ref": "100"}
    (shown,) = card["sessions"]
    assert (shown["key"], shown["state"], shown["waiting"]) == (SESSION_KEY, "waiting_input", True)
    assert shown["go"] == {"kind": "session", "ref": SESSION_KEY}
    assert card["todos"] == [{"text": TODO_TEXT, "next": True}]
    assert (card["sessions_more"], card["context_more"], card["todos_more"]) == (0, 0, 0)


async def test_the_next_part_is_what_get_next_answers() -> None:
    async with world() as daemon:
        shown = await get_json(daemon, "/next")
        body = await get_json(daemon, "/view/overview")
        again = await get_json(daemon, "/next")
        await daemon.writer.flush()
        logged = log_rows(daemon)
    assert body["next_list_id"] == shown["list_id"] == again["list_id"]
    assert [(e["rank"], e["kind"], e["reason"]) for e in body["next"]] == [
        (i["rank"], i["kind"], i["reason"]) for i in shown["items"]
    ]
    first, second = body["next"]
    assert first["go"] == {"kind": "session", "ref": SESSION_KEY}
    assert second["go"] == {"kind": "window", "ref": "100"}  # the todo's workspace window
    assert len(logged) == len(shown["items"])  # one list, logged once


async def test_an_empty_next_list_matches_get_next_too() -> None:
    async with world(waiting=False, todo=False) as daemon:
        shown = await get_json(daemon, "/next")
        body = await get_json(daemon, "/view/overview")
    assert body["next_list_id"] == shown["list_id"]
    assert body["next"] == shown["items"] == []


async def test_the_todos_are_grouped_for_the_focused_workspace() -> None:
    async with world() as daemon:
        await daemon.todos.add("elsewhere", scope_kind="workspace", scope_ref="1")
        await daemon.settle()
        body = await get_json(daemon, "/view/overview")
    focused, everything, other = body["todo_groups"]
    assert (focused["kind"], focused["title"]) == ("focused", f"Focused workspace {FOCUSED}")
    assert [(t["text"], t["next"], t["can_finish"]) for t in focused["todos"]] == [
        (TODO_TEXT, True, True)
    ]
    assert (everything["kind"], everything["todos"]) == ("all", [])
    assert other["kind"] == "scope"
    assert other["title"].startswith("Workspace 1")
    assert [t["text"] for t in other["todos"]] == ["elsewhere"]
    # a card exists for workspace 1 now that it has a todo
    assert [c["name"] for c in body["cards"]] == ["1", FOCUSED]


async def test_the_overview_shows_no_done_todo() -> None:
    async with world() as daemon:
        row = await daemon.todos.add("finished", scope_kind="workspace", scope_ref=FOCUSED)
        await daemon.todos.update(row.id, status="done")
        await daemon.settle()
        body = await get_json(daemon, "/view/overview")
    texts = [t["text"] for g in body["todo_groups"] for t in g["todos"]]
    assert texts == [TODO_TEXT]
