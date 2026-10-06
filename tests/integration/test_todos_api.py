"""``/todos`` through the real app over ASGI (WP4.1): CRUD, filters, auth on writes, and the
``/now`` fill of workspaces, sessions and projects."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from api_support import (
    BASE_URL,
    T0,
    Win,
    activity_snapshot,
    make_daemon,
    session,
    session_snapshot,
    sessions_collector,
    window_snapshot,
    windows_collector,
    workspace_snapshot,
)

from fylgja.api.app import create_app
from fylgja.api.now_models import Now
from fylgja.core.daemon import Daemon
from fylgja.models import SessionActivity

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"
AUTH = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}


@pytest.fixture
async def daemon() -> AsyncIterator[Daemon]:
    instance = make_daemon(
        windows_collector(),
        sessions_collector(),
        user_config={"privacy": {"redact_patterns": ["sk-[A-Za-z0-9]{8,}"]}},
    )
    await instance.start()
    instance.sink("wm").emit(workspace_snapshot("wm", ["1", "2", "10"], T0, focused="2"))
    instance.sink("wm").emit(window_snapshot("wm", [Win("100", "2", "Term", 500)], T0))
    instance.sink("agents").emit(
        session_snapshot(
            "agents", [session("claude", "s-1", pid=501, cwd="/work/alpha", host_pid=500)], T0
        )
    )
    instance.sink("agents").emit(
        activity_snapshot("agents", {"claude:s-1": SessionActivity(state="working")}, T0)
    )
    await instance.settle()
    try:
        yield instance
    finally:
        await instance.stop()


@pytest.fixture
async def http(daemon: Daemon) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(daemon, secret=SECRET))
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        yield client


async def add(http: httpx.AsyncClient, text: str, **extra: Any) -> dict[str, Any]:
    response = await http.post("/todos", headers=AUTH, json={"text": text, **extra})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def listing(http: httpx.AsyncClient, query: str = "") -> list[dict[str, Any]]:
    response = await http.get(f"/todos{query}")
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()
    return items


# --- create and read --------------------------------------------------------------------------


async def test_post_defaults_to_the_focused_workspace_and_get_lists_it(
    http: httpx.AsyncClient,
) -> None:
    assert await listing(http) == []
    created = await add(http, "write the docs")
    assert created["text"] == "write the docs"
    assert (created["scope_kind"], created["scope_ref"]) == ("workspace", "2")
    assert (created["status"], created["source"]) == ("open", "fylgja")
    assert created["created_at"].startswith("2026-10-05T12:00:00")
    assert "done_at" not in created
    assert [t["id"] for t in await listing(http)] == [created["id"]]
    assert not any(value is None for value in created.values())


async def test_post_with_an_explicit_scope_status_and_link(http: httpx.AsyncClient) -> None:
    project = await add(http, "p", project="/work/alpha", status="next", link="https://x.test/1")
    session_ = await add(http, "s", session="claude:s-1")
    workspace = await add(http, "w", ws="10")
    assert (project["scope_kind"], project["scope_ref"], project["status"]) == (
        "project",
        "/work/alpha",
        "next",
    )
    assert project["link"] == "https://x.test/1"
    assert (session_["scope_kind"], session_["scope_ref"]) == ("session", "claude:s-1")
    assert (workspace["scope_kind"], workspace["scope_ref"]) == ("workspace", "10")


async def test_the_text_is_redacted_before_it_is_stored(
    http: httpx.AsyncClient, daemon: Daemon
) -> None:
    created = await add(http, "rotate sk-ABCDEFGH12345 key")
    assert created["text"] == "rotate [redacted] key"
    assert "sk-ABCDEFGH12345" not in (await http.get("/now")).text


async def test_post_without_focus_is_a_409(isolated_env: Any) -> None:
    daemon = make_daemon(windows_collector())
    await daemon.start()
    try:
        transport = httpx.ASGITransport(app=create_app(daemon, secret=SECRET))
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            response = await client.post("/todos", headers=AUTH, json={"text": "x"})
            assert response.status_code == 409
            assert "no workspace is focused" in response.json()["detail"]
            assert (
                await client.post("/todos", headers=AUTH, json={"text": "x", "ws": "1"})
            ).status_code == 201
    finally:
        await daemon.stop()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"text": 3},
        {"text": "x", "status": "done"},
        {"text": "x", "status": "later"},
        {"text": "x", "ws": "1", "project": "/p"},
        {"text": "x", "unknown": 1},
        {"text": ""},
        {"text": "x" * 1001},
        {"text": "x", "ws": " "},
    ],
)
async def test_a_bad_body_is_a_422_that_does_not_repeat_the_text(
    http: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    response = await http.post("/todos", headers=AUTH, json=body)
    assert response.status_code == 422, response.text
    assert set(response.json()) == {"detail"}
    assert "xxxxxxxx" not in response.text
    assert await listing(http) == []


@pytest.mark.parametrize("raw", [b"not json", b"[]", b"null", b'"text"'])
async def test_a_body_that_is_not_an_object_is_a_422(http: httpx.AsyncClient, raw: bytes) -> None:
    assert (await http.post("/todos", headers=AUTH, content=raw)).status_code == 422


# --- filters ----------------------------------------------------------------------------------


async def test_the_filters(http: httpx.AsyncClient) -> None:
    a = await add(http, "a", ws="1")
    b = await add(http, "b", ws="2", status="next")
    c = await add(http, "c", project="/work/alpha")
    d = await add(http, "d", session="claude:s-1")
    done = await add(http, "e", ws="1")
    assert (
        await http.patch(f"/todos/{done['id']}", headers=AUTH, json={"status": "done"})
    ).status_code == 200

    async def ids(query: str) -> list[str]:
        return [t["id"] for t in await listing(http, query)]

    assert await ids("") == [a["id"], b["id"], c["id"], d["id"], done["id"]]
    assert await ids("?ws=1") == [a["id"], done["id"]]
    assert await ids("?ws=2") == [b["id"]]
    assert await ids("?project=/work/alpha") == [c["id"]]
    assert await ids("?project=%2Fwork%2Falpha") == [c["id"]]
    assert await ids("?session=claude:s-1") == [d["id"]]
    assert await ids("?status=next") == [b["id"]]
    assert await ids("?status=done") == [done["id"]]
    assert await ids("?status=open,next") == [a["id"], b["id"], c["id"], d["id"]]
    assert await ids("?ws=1&status=open") == [a["id"]]
    assert await ids("?ws=nope") == []


@pytest.mark.parametrize("query", ["?status=blocked", "?status=open,blocked", "?ws=1&project=/p"])
async def test_a_bad_filter_is_a_422(http: httpx.AsyncClient, query: str) -> None:
    assert (await http.get(f"/todos{query}")).status_code == 422


# --- patch and delete -------------------------------------------------------------------------


async def test_patch_changes_text_and_status(http: httpx.AsyncClient) -> None:
    created = await add(http, "first")
    path = f"/todos/{created['id']}"
    text = await http.patch(path, headers=AUTH, json={"text": "second"})
    assert (text.status_code, text.json()["text"]) == (200, "second")
    nxt = await http.patch(path, headers=AUTH, json={"status": "next"})
    assert nxt.json()["status"] == "next"
    done = await http.patch(path, headers=AUTH, json={"status": "done", "text": "third"})
    assert (done.json()["status"], done.json()["text"]) == ("done", "third")
    assert "done_at" in done.json()
    again = await http.patch(path, headers=AUTH, json={"status": "done"})
    assert again.json()["done_at"] == done.json()["done_at"]
    assert again.json()["updated_at"] == done.json()["updated_at"]
    reopened = (await http.patch(path, headers=AUTH, json={"status": "open"})).json()
    assert reopened["status"] == "open"
    assert "done_at" not in reopened


@pytest.mark.parametrize(
    "body", [{}, {"status": "weird"}, {"text": ""}, {"scope_ref": "3"}, {"id": "x"}, [], "x"]
)
async def test_a_bad_patch_is_a_422(http: httpx.AsyncClient, body: Any) -> None:
    created = await add(http, "keep")
    response = await http.patch(f"/todos/{created['id']}", headers=AUTH, json=body)
    assert response.status_code == 422
    assert (await listing(http))[0]["text"] == "keep"


async def test_delete_is_soft_and_a_second_delete_is_a_404(
    http: httpx.AsyncClient, daemon: Daemon
) -> None:
    created = await add(http, "bye")
    path = f"/todos/{created['id']}"
    response = await http.delete(path, headers=AUTH)
    assert (response.status_code, response.content) == (204, b"")
    assert await listing(http) == []
    assert await listing(http, "?status=open,next,done") == []
    assert (await http.delete(path, headers=AUTH)).status_code == 404
    assert (await http.patch(path, headers=AUTH, json={"status": "done"})).status_code == 404
    assert daemon.todos is not None
    assert (await http.get("/now")).json()["workspaces"][1].get("todos") is None


async def test_unknown_ids_are_404(http: httpx.AsyncClient) -> None:
    assert (await http.patch("/todos/nope", headers=AUTH, json={"text": "x"})).status_code == 404
    assert (await http.delete("/todos/nope", headers=AUTH)).status_code == 404


# --- auth -------------------------------------------------------------------------------------


async def test_writes_need_the_bearer_and_reads_do_not(http: httpx.AsyncClient) -> None:
    created = await add(http, "guarded")
    path = f"/todos/{created['id']}"
    plain = {"Content-Type": "application/json"}
    wrong = {"Authorization": "Bearer nope", "Content-Type": "application/json"}
    for headers in (plain, wrong):
        assert (await http.post("/todos", headers=headers, json={"text": "x"})).status_code == 401
        assert (await http.patch(path, headers=headers, json={"status": "done"})).status_code == 401
        assert (await http.delete(path, headers=headers)).status_code == 401
    foreign = {**AUTH, "Origin": "http://evil.example"}
    assert (await http.post("/todos", headers=foreign, json={"text": "x"})).status_code == 403
    assert (await http.get("/todos")).status_code == 200
    assert [t["id"] for t in await listing(http)] == [created["id"]]
    assert (await listing(http))[0]["status"] == "open"


# --- /now -------------------------------------------------------------------------------------


async def test_now_shows_open_and_next_todos_by_scope(
    http: httpx.AsyncClient, daemon: Daemon
) -> None:
    older = await add(http, "older open")
    nxt = await add(http, "important", status="next")
    other = await add(http, "elsewhere", ws="10")
    on_project = await add(http, "project work", project="/work/alpha")
    on_session = await add(http, "session work", session="claude:s-1")
    finished = await add(http, "finished")
    await http.patch(f"/todos/{finished['id']}", headers=AUTH, json={"status": "done"})
    removed = await add(http, "removed")
    await http.delete(f"/todos/{removed['id']}", headers=AUTH)

    now_json = (await http.get("/now")).json()
    now = Now.model_validate(now_json)
    assert now.schema_ == "fylgja.now/1"
    workspaces = {w.name: w for w in now.workspaces or []}
    assert [t.id for t in workspaces["2"].todos or []] == [nxt["id"], older["id"]]  # next first
    assert [t.status for t in workspaces["2"].todos or []] == ["next", "open"]
    assert [t.id for t in workspaces["10"].todos or []] == [other["id"]]
    assert workspaces["1"].todos is None
    assert "todos" not in next(w for w in now_json["workspaces"] if w["name"] == "1")
    sessions = {s.key: s for s in now.sessions or []}
    assert [t.id for t in sessions["claude:s-1"].todos or []] == [on_session["id"]]
    projects = {p.path: p for p in now.projects or []}
    assert [t.id for t in projects["/work/alpha"].todos or []] == [on_project["id"]]
    shown = workspaces["2"].todos or []
    assert (shown[0].scope_kind, shown[0].scope_ref, shown[0].source) == (
        "workspace",
        "2",
        "fylgja",
    )
    assert not list(_nulls(now_json))

    await http.patch(f"/todos/{nxt['id']}", headers=AUTH, json={"status": "done"})
    after = Now.model_validate((await http.get("/now")).json())
    assert [t.id for t in {w.name: w for w in after.workspaces or []}["2"].todos or []] == [
        older["id"]
    ]


def _nulls(value: Any, path: str = "$") -> list[str]:
    if value is None:
        return [path]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _nulls(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _nulls(v, f"{path}[{i}]")]
    return []


async def test_todos_survive_a_restart(isolated_env: Any) -> None:
    first = make_daemon(windows_collector())
    await first.start()
    created = await first.todos.add("remember", scope_kind="workspace", scope_ref="2")
    await first.stop()
    second = make_daemon(windows_collector(), database=first.writer.database)
    await second.start()
    try:
        assert set(second.view.todos) == {created.id}
        assert [t.id for t in await second.todos.list_todos()] == [created.id]
    finally:
        await second.stop()
