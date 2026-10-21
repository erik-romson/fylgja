"""The journal's read routes over a fixture daemon (J16, J17; ``docs/api/journal.md``).

Synthetic collectors (``wm`` windows, ``agents`` sessions, ``proj`` projects) run on a fake clock
and a fake filesystem. Window ``100`` hosts a session in the git checkout ``/w/repo`` (on ``main``),
window ``200`` one in ``/w/plain`` (no git root). Thread rows, entries and items are inserted
through the store helpers; no model is called.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from api_support import (
    T0,
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
from summary_support import FakeRunner

from fylgja.collectors.base import Capability
from fylgja.core.daemon import Daemon
from fylgja.core.journal import ThreadKey, thread_id
from fylgja.models import ProjectEvent, ProjectPayload, SessionActivity, SessionRecap
from fylgja.store import db, queries
from fylgja.testing.fakes import FakeFS, FakeProcessRunner

REPO = "/w/repo"
PLAIN = "/w/plain"
MAIN = thread_id(ThreadKey(REPO, "main"))
OLD = thread_id(ThreadKey(REPO, "old"))
JOURNAL_ON: dict[str, Any] = {"journal": {"enabled": True}}


@pytest.fixture(autouse=True)
def utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Local time is UTC, so "today" is the same everywhere."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


def stamp(seconds: float) -> str:
    return db.to_db_time(T0 + timedelta(seconds=seconds))


def thread_row(tid: str = MAIN, branch: str = "main", **over: object) -> dict[str, object]:
    return {
        "thread_id": tid,
        "repo_root": REPO,
        "branch": branch,
        "name": "repo",
        "first_at": stamp(-7200),
        "last_at": stamp(-60),
        "watermark": stamp(-60),
        "where_text": "You are halfway through the form.",
        "where_at": stamp(-60),
        "last_attempt_at": stamp(-60),
        "error_kind": None,
        "error_at": None,
        **over,
    }


def entry_row(seconds: float, tid: str = MAIN, **over: object) -> dict[str, object]:
    return {
        "thread_id": tid,
        "started_at": stamp(seconds - 300),
        "ended_at": stamp(seconds),
        "trigger": "turn_done",
        "headline": f"entry at {seconds:g}",
        "did": json.dumps(["You did a thing."]),
        "sources": json.dumps([{"id": "S1", "kind": "thread", "ref": tid, "label": "repo · main"}]),
        "input_digest": "d" * 64,
        "model": "haiku",
        "generated_at": stamp(seconds),
        **over,
    }


def item_row(seq: int, tid: str = MAIN, **over: object) -> dict[str, object]:
    return {
        "id": f"{tid[:8]}{seq:08d}",
        "thread_id": tid,
        "seq": seq,
        "kind": "next",
        "text": f"do {seq}",
        "status": "open",
        "pinned": 0,
        "edited": 0,
        "source": json.dumps({"kind": "thread", "ref": tid, "label": "repo · main"}),
        "created_at": stamp(-3600 + seq * 60),
        "created_entry": None,
        "closed_at": None,
        "closed_by": None,
        "closed_entry": None,
        "todo_id": None,
        "how_text": None,
        "how_digest": None,
        "how_at": None,
        "how_error_kind": None,
        **over,
    }


def fs() -> FakeFS:
    found = FakeFS()
    found.add_dir(f"{REPO}/.git")
    return found


@asynccontextmanager
async def world(
    user_config: Mapping[str, Any] | None = JOURNAL_ON, *, focus: str | None = "100"
) -> AsyncIterator[Daemon]:
    daemon = make_daemon(
        windows_collector(),
        sessions_collector(),
        Synthetic("proj", frozenset({Capability.PROJECT_STATE})),
        user_config=user_config,
        fs=fs(),
        runner=FakeProcessRunner(),
        summary_runner=FakeRunner(),
    )
    await daemon.start()
    try:
        wm, agents, proj = daemon.sink("wm"), daemon.sink("agents"), daemon.sink("proj")
        wm.emit(workspace_snapshot("wm", ["1", "2"], T0, focused="1"))
        wm.emit(
            window_snapshot(
                "wm", [Win("100", "1", "Term", 500, "zsh"), Win("200", "2", "Term", 600)], T0
            )
        )
        proj.emit(
            ProjectEvent(
                source="proj",
                entity_id=REPO,
                observed_at=T0,
                payload=ProjectPayload(path=REPO, repo_root=REPO, branch="main"),
            )
        )
        agents.emit(
            session_snapshot(
                "agents",
                [
                    session("claude", "a", pid=501, cwd=REPO, host_pid=500),
                    session("claude", "b", pid=601, cwd=PLAIN, host_pid=600),
                ],
                T0,
            )
        )
        agents.emit(
            activity_snapshot(
                "agents",
                {
                    "claude:a": SessionActivity(state="idle", title="Form"),
                    "claude:b": SessionActivity(state="idle", title="Notes"),
                },
                T0,
            )
        )
        await daemon.settle()
        if focus is not None:
            wm.emit(focus_event("wm", focus, "1", T0))
            await daemon.settle()
        yield daemon
    finally:
        await daemon.stop()


async def store(
    daemon: Daemon,
    *,
    threads: tuple[dict[str, object], ...] = (),
    entries: tuple[dict[str, object], ...] = (),
    items: tuple[dict[str, object], ...] = (),
) -> None:
    """Insert rows, then let the journal service read them into its cache."""

    def job(tx: Any) -> None:
        for row in threads:
            queries.put_journal_thread(tx.connection, row)
        for found in entries:
            queries.insert_journal_entry(tx.connection, found)
        for found in items:
            queries.put_journal_item(tx.connection, found)

    await daemon.writer.execute(job)
    assert daemon.journal is not None
    await daemon.journal.load()


async def get(daemon: Daemon, path: str, status: int = 200) -> Any:
    async with client(daemon) as http:
        response = await http.get(path)
    assert response.status_code == status, response.text
    if status == 200:
        assert response.headers["content-type"] == "application/json"
        assert response.headers["cache-control"] == "no-store"
    return response.json()


# --------------------------------------------------------------------------------------------
# Off and empty
# --------------------------------------------------------------------------------------------


async def test_while_the_journal_is_off_the_list_is_empty_and_the_rest_is_409() -> None:
    async with world(None) as daemon:
        assert daemon.journal is None
        body = await get(daemon, "/journal/threads")
        assert body == {"schema": "fylgja.journal/1", "enabled": False, "threads": []}
        for path in (
            "/journal/focused",
            f"/journal/threads/{MAIN}",
            "/journal/threads/0123456789abcdef",
            f"/journal/checkout?path={REPO}",
        ):
            assert (await get(daemon, path, 409))["detail"] == "the journal is off"


async def test_enabled_with_no_threads_the_list_is_empty() -> None:
    async with world() as daemon:
        body = await get(daemon, "/journal/threads")
        assert body == {"schema": "fylgja.journal/1", "enabled": True, "threads": []}


async def test_the_focused_checkout_of_a_new_thread_answers_the_empty_journal() -> None:
    async with world() as daemon:
        body = await get(daemon, "/journal/focused")
        assert body["schema"] == "fylgja.journal/1"
        assert body["enabled"] is True
        assert (body["id"], body["branch"], body["repo_root"]) == (MAIN, "main", REPO)
        assert body["folder"] == "w/repo"
        assert body["where"] == {"state": "missing"}
        assert body["entries"] == []
        assert body["done_recent"] == []
        assert body["open"] == {"next": [], "remember": [], "blocked": []}
        assert list(nulls(body)) == []
        assert await get(daemon, f"/journal/threads/{MAIN}") == body  # the id reads it too


async def recap(daemon: Daemon, seconds: float, text: str, branch: str) -> None:
    """Session ``a`` (in the repo) writes a recap."""
    written = SessionRecap(text=text, at=T0 + timedelta(seconds=seconds), branch=branch)
    daemon.sink("agents").emit(
        activity_snapshot(
            "agents",
            {
                "claude:a": SessionActivity(state="idle", title="Form", recap=written),
                "claude:b": SessionActivity(state="idle", title="Notes"),
            },
            T0 + timedelta(seconds=seconds + 1),
        )
    )
    await daemon.settle()


async def test_the_thread_carries_the_newest_recap_of_its_sessions_on_its_branch() -> None:
    async with world() as daemon:
        await recap(daemon, 10, "Writing the form; next, test it.", "main")
        await recap(daemon, 20, "Elsewhere on another branch.", "topic")
        body = await get(daemon, "/journal/focused")  # a thread without a row has it too
        assert body["recap"] == {
            "text": "Writing the form; next, test it.",
            "at": "2026-10-05T12:00:10Z",
            "session": "claude:a",
        }
        assert list(nulls(body)) == []
        await fresh(daemon)
        assert (await get(daemon, f"/journal/threads/{MAIN}"))["recap"] == body["recap"]


async def test_a_focused_folder_with_no_git_root_has_no_journal() -> None:
    async with world(focus="200") as daemon:
        body = await get(daemon, "/journal/focused", 404)
        assert body["detail"] == "no journal for this checkout"


async def test_nothing_focused_is_404() -> None:
    async with world(focus=None) as daemon:
        assert (await get(daemon, "/journal/focused", 404))["detail"] == "nothing is focused"


# --------------------------------------------------------------------------------------------
# A thread with a journal
# --------------------------------------------------------------------------------------------


async def fresh(daemon: Daemon) -> None:
    items = tuple(
        item_row(seq, pinned=int(seq in (1, 2)))
        for seq in range(1, 6)  # 5 next, 1 and 2 pinned
    )
    await store(
        daemon,
        threads=(thread_row(),),
        entries=(entry_row(-3000), entry_row(-2000), entry_row(-1000)),
        items=(
            *items,
            item_row(6, kind="remember", text="keep the form small"),
            item_row(
                7,
                text="a finished one",
                status="done",
                closed_at=stamp(-30),
                closed_by="user",
            ),
        ),
    )


async def test_the_detail_orders_open_items_and_marks_the_first_three_shown() -> None:
    async with world() as daemon:
        await fresh(daemon)
        body = await get(daemon, f"/journal/threads/{MAIN}")
        nexts = body["open"]["next"]
        assert [item["text"] for item in nexts] == ["do 2", "do 1", "do 5", "do 4", "do 3"]
        assert [item["shown"] for item in nexts] == [True, True, True, False, False]
        assert [item["pinned"] for item in nexts] == [True, True, False, False, False]
        assert nexts[0]["source"] == {"kind": "thread", "ref": MAIN, "label": "repo · main"}
        assert nexts[0]["has_how"] is False
        assert [item["text"] for item in body["open"]["remember"]] == ["keep the form small"]
        assert body["where"]["state"] == "fresh"
        assert body["where"]["text"] == "You are halfway through the form."
        assert [entry["headline"] for entry in body["entries"]] == [
            "entry at -1000",
            "entry at -2000",
            "entry at -3000",
        ]
        assert body["entries"][0]["sources"][0]["id"] == "S1"
        assert list(nulls(body)) == []


async def test_done_lately_lists_the_days_headlines_and_closed_items() -> None:
    async with world() as daemon:
        await fresh(daemon)
        body = await get(daemon, f"/journal/threads/{MAIN}")
        assert [day["day"] for day in body["done_recent"]] == ["2026-10-05"]
        today = body["done_recent"][0]
        assert today["relative"] == "today"
        assert today["headlines"] == ["entry at -1000", "entry at -2000", "entry at -3000"]
        assert [item["text"] for item in today["items"]] == ["a finished one"]
        assert today["items"][0]["closed_by"] == "user"


async def test_entries_limits_the_newest_entries() -> None:
    async with world() as daemon:
        await fresh(daemon)
        body = await get(daemon, f"/journal/threads/{MAIN}?entries=2")
        assert [entry["headline"] for entry in body["entries"]] == [
            "entry at -1000",
            "entry at -2000",
        ]


async def test_the_list_carries_the_shown_items_and_the_open_count() -> None:
    async with world() as daemon:
        await fresh(daemon)
        body = await get(daemon, "/journal/threads")
        assert [thread["id"] for thread in body["threads"]] == [MAIN]
        brief = body["threads"][0]
        assert [item["text"] for item in brief["next"]] == ["do 2", "do 1", "do 5"]
        assert [item["text"] for item in brief["remember"]] == ["keep the form small"]
        assert brief["blocked"] == []
        assert brief["open"] == 6
        assert (brief["name"], brief["folder"], brief["branch"]) == ("repo", "w/repo", "main")
        assert brief["where"]["state"] == "fresh"
        assert list(nulls(body)) == []


async def test_a_failed_attempt_keeps_the_text_and_says_failed() -> None:
    async with world() as daemon:
        await store(daemon, threads=(thread_row(error_kind="timeout", error_at=stamp(-5)),))
        body = await get(daemon, f"/journal/threads/{MAIN}")
        assert body["where"]["state"] == "failed"
        assert body["where"]["text"] == "You are halfway through the form."
        assert (
            body["error_kind"],
            (await get(daemon, "/journal/threads"))["threads"][0]["error_kind"],
        ) == (
            "timeout",
            "timeout",
        )


async def test_a_thread_91_days_old_is_not_listed_but_is_still_readable() -> None:
    async with world() as daemon:
        await store(
            daemon,
            threads=(
                thread_row(),
                thread_row(OLD, "old", last_at=stamp(-91 * 86400), where_text="Back then."),
            ),
        )
        recent = await get(daemon, "/journal/threads?days=90")
        assert [thread["id"] for thread in recent["threads"]] == [MAIN]
        old = await get(daemon, f"/journal/threads/{OLD}")
        assert old["where"]["text"] == "Back then."


@pytest.mark.parametrize(
    "path",
    [
        "/journal/threads?days=0",
        "/journal/threads?days=91",
        f"/journal/threads/{MAIN}?entries=0",
        f"/journal/threads/{MAIN}?entries=101",
        "/journal/checkout?path=relative/dir",
    ],
)
async def test_a_parameter_out_of_range_is_422(path: str) -> None:
    async with world() as daemon:
        await get(daemon, path, 422)


@pytest.mark.parametrize(
    "thread",
    ["nope", "0123456789ABCDEF", "0123456789abcde", "0123456789abcdef0", "0123456789abcdef"],
)
async def test_a_malformed_or_unknown_thread_id_is_404(thread: str) -> None:
    async with world() as daemon:
        await get(daemon, f"/journal/threads/{thread}", 404)


async def test_checkout_resolves_a_path_below_the_checkout() -> None:
    async with world() as daemon:
        await fresh(daemon)
        body = await get(daemon, f"/journal/checkout?path={REPO}/src")
        assert body["id"] == MAIN
        assert body["where"]["text"] == "You are halfway through the form."
        outside = await get(daemon, f"/journal/checkout?path={PLAIN}", 404)
        assert outside["detail"] == "no journal for this checkout"


async def test_checkout_matches_known_checkouts_and_never_looks_on_disk() -> None:
    async with world() as daemon:
        await fresh(daemon)
        remembered = dict(daemon.locator._cache)  # noqa: SLF001
        body = await get(daemon, "/journal/checkout?path=/elsewhere/customer/repo/src", 404)
        assert body["detail"] == "no journal for this checkout"
        assert daemon.locator._cache == remembered  # noqa: SLF001
