"""The person's actions on the journal (J18, J19; ``docs/api/journal.md``).

A real daemon over ASGI with the journal on, the model a :class:`FakeRunner` and ``git log``
scripted: the module reuses the world of ``test_journal_daemon``. A first Stop at monotonic time 0
gives one entry at 120 s whose reply adds the ``next`` item :data:`ITEM` (``n1``). Nothing calls a
real model.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from api_support import BASE_URL, T0
from summary_support import SECRET, wait_until
from test_journal_daemon import (
    ITEM,
    MAIN,
    MAIN_ID,
    NEW_PROMPT,
    REPO,
    A,
    World,
    reply,
    stored,
    world,
)

from fylgja.api.app import create_app
from fylgja.core.journal import item_seq_id
from fylgja.store import db, queries
from fylgja.testing.fakes import FakeClock

AUTH = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}
N1 = item_seq_id(MAIN_ID, 1)
CLOSE_N1 = reply(add=None, close=[{"item": "n1", "source": "S1"}])


@asynccontextmanager
async def acting(**options: Any) -> AsyncIterator[tuple[World, httpx.AsyncClient]]:
    """A journal world with its first entry written (at 120 s) and an HTTP client."""
    async with world(**options) as w:
        transport = httpx.ASGITransport(app=create_app(w.daemon, secret=SECRET))
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            await w.hook(A, "Stop")
            await w.until(120)
            assert len(stored(w.daemon).entries) == 1
            w.changed.clear()
            yield w, http


def item(w: World, item_id: str = N1) -> Any:
    found = next(row for row in stored(w.daemon).items if row.id == item_id)
    return found


async def entry_after(w: World, seconds: float = 10.0) -> None:
    """New work in the checkout, ``seconds`` after the last step, without a Stop."""
    await w.advance(seconds)
    await w.prompt(A, NEW_PROMPT)


# --------------------------------------------------------------------------------------------
# PATCH
# --------------------------------------------------------------------------------------------


async def test_done_closes_the_item_for_the_user_and_repeating_it_changes_nothing() -> None:
    async with acting() as (w, http):
        response = await http.patch(f"/journal/items/{N1}", headers=AUTH, json={"status": "done"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["id"], body["status"], body["closed_by"]) == (N1, "done", "user")
        assert body["shown"] is False
        assert "closed_at" in body
        assert w.changed == [MAIN_ID]
        row = item(w)
        assert (row.status, row.closed_by, row.closed_entry) == ("done", "user", None)
        brief = w.journal.brief(MAIN_ID)
        assert brief is not None
        assert brief.open_items == ()
        again = await http.patch(f"/journal/items/{N1}", headers=AUTH, json={"status": "done"})
        assert again.status_code == 200
        assert again.json() == body
        assert w.changed == [MAIN_ID]
        assert not w.journal.tracker.states[MAIN].dirty


async def test_patch_pins_and_drops_and_a_shown_open_item_says_so() -> None:
    async with acting() as (w, http):
        pinned = await http.patch(f"/journal/items/{N1}", headers=AUTH, json={"pinned": True})
        assert pinned.status_code == 200
        assert (pinned.json()["pinned"], pinned.json()["shown"]) == (True, True)
        assert item(w).pinned
        dropped = await http.patch(f"/journal/items/{N1}", headers=AUTH, json={"status": "dropped"})
        assert dropped.json()["status"] == "dropped"
        assert dropped.json()["closed_by"] == "user"
        assert w.changed == [MAIN_ID, MAIN_ID]


@pytest.mark.parametrize(
    "body",
    [{}, {"pinned": None}, {"status": None}, {"text": None}, {"text": ""}, {"text": " " * 5},
     {"text": "x" * 201}, {"status": "closed"}, {"pinned": True, "extra": 1}],
)  # fmt: skip
async def test_a_bad_body_is_422_and_changes_nothing(body: dict[str, Any]) -> None:
    async with acting() as (w, http):
        before = item(w)
        response = await http.patch(f"/journal/items/{N1}", headers=AUTH, json=body)
        assert response.status_code == 422, response.text
        assert "x" * 201 not in response.text
        assert item(w) == before
        assert w.changed == []


async def test_an_unknown_item_is_404() -> None:
    async with acting() as (_, http):
        response = await http.patch("/journal/items/nope", headers=AUTH, json={"pinned": True})
        assert response.status_code == 404


async def test_edited_text_is_redacted_and_locks_the_item_against_the_model() -> None:
    async with acting() as (w, http):
        response = await http.patch(
            f"/journal/items/{N1}", headers=AUTH, json={"text": "  write   the   NEW test "}
        )
        assert response.status_code == 200
        assert response.json()["text"] == "write the NEW test"
        assert item(w).edited
        w.runner.default = CLOSE_N1
        await entry_after(w)
        refresh = await http.post(f"/journal/threads/{MAIN_ID}/refresh", headers=AUTH, json={})
        assert refresh.status_code == 202
        await w.quiet()
        assert len(w.runner.calls) == 2
        assert "- n1 (kept by the person): write the NEW test" in w.runner.calls[1].prompt
        assert item(w).status == "open"
        assert len(stored(w.daemon).entries) == 2


async def test_reopening_a_model_closed_item_marks_it_edited() -> None:
    async with acting() as (w, http):
        w.runner.default = CLOSE_N1
        await entry_after(w)
        await http.post(f"/journal/threads/{MAIN_ID}/refresh", headers=AUTH)
        await w.quiet()
        closed = item(w)
        assert (closed.status, closed.closed_by) == ("done", "model")
        response = await http.patch(f"/journal/items/{N1}", headers=AUTH, json={"status": "open"})
        assert response.status_code == 200
        assert (response.json()["status"], response.json()["shown"]) == ("open", True)
        assert "closed_at" not in response.json()
        reopened = item(w)
        assert (reopened.status, reopened.edited, reopened.closed_by) == ("open", True, None)
        assert reopened.closed_at is None
        assert reopened.closed_entry is None


async def test_a_patch_during_a_call_wins_over_the_models_close() -> None:
    async with acting() as (w, http):
        await entry_after(w)
        w.runner.default = CLOSE_N1
        w.hold()
        started = asyncio.create_task(
            http.post(f"/journal/threads/{MAIN_ID}/refresh", headers=AUTH)
        )
        await wait_until(lambda: len(w.runner.calls) == 2)
        patched = await http.patch(f"/journal/items/{N1}", headers=AUTH, json={"status": "done"})
        assert patched.status_code == 200
        w.release()
        assert (await started).status_code == 202
        await w.quiet()
        assert len(stored(w.daemon).entries) == 2
        row = item(w)
        assert (row.status, row.closed_by, row.closed_entry) == ("done", "user", None)


# --------------------------------------------------------------------------------------------
# Add
# --------------------------------------------------------------------------------------------


async def test_the_person_adds_an_item_with_a_person_source() -> None:
    async with acting() as (w, http):
        url = f"/journal/threads/{MAIN_ID}/items"
        response = await http.post(url, headers=AUTH, json={"kind": "remember", "text": "x"})
        assert response.status_code == 201, response.text
        body = response.json()
        assert (body["kind"], body["text"], body["status"]) == ("remember", "x", "open")
        assert body["source"] == {"kind": "person", "ref": MAIN_ID, "label": "you"}
        assert body["id"] == item_seq_id(MAIN_ID, 2)
        assert body["shown"] is True
        assert item(w, body["id"]).edited
        assert w.changed == [MAIN_ID]
        brief = w.journal.brief(MAIN_ID)
        assert brief is not None
        assert [row.text for row in brief.open_items] == ["x", ITEM]
        assert not w.journal.tracker.states[MAIN].dirty


@pytest.mark.parametrize(
    ("thread", "body", "status"),
    [
        ("0" * 16, {"kind": "next", "text": "x"}, 404),
        ("nope", {"kind": "next", "text": "x"}, 404),
        (MAIN_ID, {"kind": "idea", "text": "x"}, 422),
        (MAIN_ID, {"kind": "next", "text": ""}, 422),
        (MAIN_ID, {"kind": "next"}, 422),
        (MAIN_ID, {"kind": "next", "text": "x", "pinned": True}, 422),
    ],
)
async def test_add_refuses_what_it_cannot_store(
    thread: str, body: dict[str, Any], status: int
) -> None:
    async with acting() as (w, http):
        response = await http.post(f"/journal/threads/{thread}/items", headers=AUTH, json=body)
        assert response.status_code == status, response.text
        assert len(stored(w.daemon).items) == 1
        assert w.changed == []


# --------------------------------------------------------------------------------------------
# The todo link (J19)
# --------------------------------------------------------------------------------------------


async def make_todo(http: httpx.AsyncClient) -> dict[str, Any]:
    response = await http.post(f"/journal/items/{N1}/todo", headers=AUTH, json={})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_make_it_a_todo_creates_one_scoped_to_the_checkout_and_repeats_as_200() -> None:
    async with acting() as (w, http):
        todo = await make_todo(http)
        assert (todo["scope_kind"], todo["scope_ref"], todo["text"]) == ("project", REPO, ITEM)
        assert todo["status"] == "open"
        assert item(w).todo_id == todo["id"]
        assert w.changed == [MAIN_ID]
        again = await http.post(f"/journal/items/{N1}/todo", headers=AUTH, json={})
        assert again.status_code == 200
        assert again.json()["id"] == todo["id"]
        assert w.changed == [MAIN_ID]
        listed = await http.get("/journal/threads")
        assert listed.json()["threads"][0]["next"][0]["todo_id"] == todo["id"]
        missing = await http.post("/journal/items/nope/todo", headers=AUTH, json={})
        assert missing.status_code == 404
        assert not w.journal.tracker.states[MAIN].dirty


async def test_finishing_the_todo_closes_the_item_for_the_user() -> None:
    async with acting() as (w, http):
        todo = await make_todo(http)
        w.changed.clear()
        done = await http.patch(f"/todos/{todo['id']}", headers=AUTH, json={"status": "done"})
        assert done.status_code == 200
        await wait_until(lambda: item(w).status == "done")
        row = item(w)
        assert (row.closed_by, row.closed_entry, row.todo_id) == ("user", None, todo["id"])
        assert row.closed_at is not None
        assert row.closed_at == w.clock.now()
        assert w.changed == [MAIN_ID]
        brief = w.journal.brief(MAIN_ID)
        assert brief is not None
        assert brief.open_items == ()
        assert not w.journal.tracker.states[MAIN].dirty


async def test_reopening_the_todo_changes_nothing() -> None:
    async with acting() as (w, http):
        todo = await make_todo(http)
        await http.patch(f"/todos/{todo['id']}", headers=AUTH, json={"status": "done"})
        await wait_until(lambda: item(w).status == "done")
        w.changed.clear()
        await http.patch(f"/todos/{todo['id']}", headers=AUTH, json={"status": "open"})
        await w.quiet()
        assert item(w).status == "done"
        assert w.changed == []


async def test_deleting_the_todo_unlinks_the_item_and_leaves_it_open() -> None:
    async with acting() as (w, http):
        todo = await make_todo(http)
        w.changed.clear()
        deleted = await http.delete(f"/todos/{todo['id']}", headers=AUTH)
        assert deleted.status_code == 204
        await wait_until(lambda: item(w).todo_id is None)
        row = item(w)
        assert (row.status, row.closed_by) == ("open", None)
        assert w.changed == [MAIN_ID]
        assert not w.journal.tracker.states[MAIN].dirty
        again = await make_todo(http)  # a new todo can be made
        assert again["id"] != todo["id"]


async def test_a_linked_todo_is_not_input_but_another_project_todo_is() -> None:
    async with acting() as (w, http):
        await w.advance(5)  # the window is open at the watermark
        todo = await make_todo(http)
        await http.patch(f"/todos/{todo['id']}", headers=AUTH, json={"status": "done"})
        await wait_until(lambda: item(w).status == "done")
        assert not w.journal.tracker.states[MAIN].dirty
        await w.advance(5)
        other = await http.post(
            "/todos", headers=AUTH, json={"text": "Update the changelog", "project": REPO}
        )
        assert other.status_code == 201
        await w.quiet()
        assert w.journal.tracker.states[MAIN].dirty  # only the unlinked todo made it dirty
        await w.advance(1)
        refresh = await http.post(f"/journal/threads/{MAIN_ID}/refresh", headers=AUTH)
        assert refresh.status_code == 202
        await w.quiet()
        prompt = w.runner.calls[1].prompt
        assert "todo opened: Update the changelog" in prompt
        assert ITEM not in prompt.split("todo opened")[1]
        assert "todo done" not in prompt


async def test_a_restart_reconciles_the_linked_items(tmp_path: Path) -> None:
    clock = FakeClock(T0)
    database = tmp_path / "fylgja.sqlite"
    async with acting(clock=clock, database=database) as (_, http):
        todo = await make_todo(http)
        other = await http.post(
            f"/journal/threads/{MAIN_ID}/items", headers=AUTH, json={"kind": "next", "text": "b"}
        )
        second = await http.post(f"/journal/items/{other.json()['id']}/todo", headers=AUTH, json={})
        assert second.status_code == 201
    stamp = db.to_db_time(clock.now())
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE todos SET status = 'done', done_at = ? WHERE id = ?", (stamp, todo["id"])
        )
        connection.execute(
            "UPDATE todos SET deleted_at = ?, deleted_reason = 'user' WHERE id = ?",
            (stamp, second.json()["id"]),
        )
        connection.commit()
    finally:
        connection.close()
    async with world(clock=clock, database=database, fresh=False) as again:
        rows = {row.id: row for row in stored(again.daemon).items}
        assert (rows[N1].status, rows[N1].closed_by) == ("done", "user")
        assert rows[N1].closed_at is not None
        assert (rows[other.json()["id"]].status, rows[other.json()["id"]].todo_id) == ("open", None)
        brief = again.journal.brief(MAIN_ID)
        assert brief is not None
        assert [row.text for row in brief.open_items] == ["b"]


# --------------------------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------------------------


async def test_refresh_of_a_dirty_thread_inside_min_interval_writes_a_manual_entry() -> None:
    async with acting() as (w, http):
        await entry_after(w)  # 130 s: inside min_interval of the entry at 120 s
        assert len(w.runner.calls) == 1
        response = await http.post(f"/journal/threads/{MAIN_ID}/refresh", headers=AUTH, json={})
        assert response.status_code == 202, response.text
        assert response.json() == {"thread": MAIN_ID, "state": "pending"}
        await w.quiet()
        assert len(w.runner.calls) == 2
        entries = stored(w.daemon).entries
        assert [entry.trigger for entry in entries] == ["turn_done", "manual"]
        assert entries[1].started_at == entries[0].ended_at
        assert w.changed == [MAIN_ID]
        assert not w.journal.pending(MAIN_ID)


async def test_refresh_with_nothing_new_makes_no_call() -> None:
    async with acting() as (w, http):
        response = await http.post(f"/journal/threads/{MAIN_ID}/refresh", headers=AUTH, json={})
        assert response.status_code == 200, response.text
        assert response.json() == {"thread": MAIN_ID, "state": "nothing_new"}
        await w.quiet()
        assert len(w.runner.calls) == 1
        assert w.changed == []
        assert not w.journal.pending(MAIN_ID)


async def test_refresh_during_an_attempt_joins_it_and_makes_one_call() -> None:
    async with acting() as (w, http):
        await entry_after(w)
        w.hold()
        first = asyncio.create_task(http.post(f"/journal/threads/{MAIN_ID}/refresh", headers=AUTH))
        await wait_until(lambda: len(w.runner.calls) == 2)
        joined = await http.post(f"/journal/threads/{MAIN_ID}/refresh", headers=AUTH, json={})
        assert joined.status_code == 202
        assert joined.json() == {"thread": MAIN_ID, "state": "pending"}
        assert (await first).status_code == 202
        assert w.journal.pending(MAIN_ID)
        assert len(w.runner.calls) == 2
        w.release()
        await w.quiet()
        assert len(w.runner.calls) == 2
        assert len(stored(w.daemon).entries) == 2


async def test_refresh_ignores_the_failure_wait() -> None:
    async with acting() as (w, http):
        await entry_after(w)
        w.runner.default = "not json"
        failed = await http.post(f"/journal/threads/{MAIN_ID}/refresh", headers=AUTH, json={})
        assert failed.status_code == 202
        await w.quiet()
        row = stored(w.daemon).thread
        assert row is not None
        assert row.error_kind == "bad_output"
        w.runner.default = reply()
        again = await http.post(f"/journal/threads/{MAIN_ID}/refresh", headers=AUTH, json={})
        assert again.status_code == 202
        await w.quiet()
        row = stored(w.daemon).thread
        assert row is not None
        assert row.error_kind is None
        assert [entry.trigger for entry in stored(w.daemon).entries] == ["turn_done", "manual"]


@pytest.mark.parametrize("thread", ["0" * 16, "nope", "../x"])
async def test_refresh_of_an_unknown_thread_is_404(thread: str) -> None:
    async with acting() as (_, http):
        response = await http.post(f"/journal/threads/{thread}/refresh", headers=AUTH, json={})
        assert response.status_code == 404


async def test_refresh_of_a_known_checkout_without_a_row_starts_its_thread() -> None:
    async with world() as w:
        transport = httpx.ASGITransport(app=create_app(w.daemon, secret=SECRET))
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            assert stored(w.daemon).thread is None
            await w.advance(10)
            await w.prompt(A, NEW_PROMPT)
            response = await http.post(f"/journal/threads/{MAIN_ID}/refresh", headers=AUTH, json={})
            assert response.status_code == 202, response.text
            await w.quiet()
            found = stored(w.daemon)
            assert [entry.trigger for entry in found.entries] == ["manual"]
            assert w.journal.thread_for_path(REPO) == MAIN


# --------------------------------------------------------------------------------------------
# Disabled
# --------------------------------------------------------------------------------------------


async def test_every_action_is_409_while_the_journal_is_off() -> None:
    async with world(user_config={}) as w:
        transport = httpx.ASGITransport(app=create_app(w.daemon, secret=SECRET))
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            calls = [
                http.patch("/journal/items/x", headers=AUTH, json={"pinned": True}),
                http.patch("/journal/items/x", headers=AUTH, json={}),
                http.post(f"/journal/threads/{MAIN_ID}/items", headers=AUTH, json={}),
                http.post("/journal/items/x/todo", headers=AUTH, json={}),
                http.post(f"/journal/threads/{MAIN_ID}/refresh", headers=AUTH, json={}),
            ]
            for response in await asyncio.gather(*calls):
                assert response.status_code == 409, response.text
        assert w.daemon.journal is None
        connection = db.connect_reader(w.daemon.writer.database)
        try:
            assert queries.load_journal_threads(connection) == []
        finally:
            connection.close()
