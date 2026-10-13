"""The minimal page at ``/`` over the real guarded app (WP4.4, A19; ``PLAN-final-plan.md`` §4.2).

The page is one static HTML file. These tests check what it serves (element ids, the secret in the
meta tag, the headers), that the Host check covers it, and a scripted HTTP flow with the same
requests the page's JavaScript makes: add, then done, with the meta secret as the bearer and the
page's own ``Origin``.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from html.parser import HTMLParser
from importlib import resources
from typing import Any

import httpx
import pytest
from api_support import (
    BASE_URL,
    PORT,
    T0,
    Win,
    make_daemon,
    window_snapshot,
    windows_collector,
    workspace_snapshot,
)

from fylgja.api.app import create_app
from fylgja.api.routes.page import PLACEHOLDER, render_page
from fylgja.core.daemon import Daemon

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"
ORIGIN = f"http://127.0.0.1:{PORT}"
OPEN = "status=open,next"


class _Page(HTMLParser):
    """The ids and the meta tags of a page."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: dict[str, str] = {}
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if "id" in values:
            self.ids[values["id"]] = tag
        if tag == "meta" and "name" in values:
            self.meta[values["name"]] = values.get("content", "")


def _parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    return page


@pytest.fixture
async def daemon() -> AsyncIterator[Daemon]:
    instance = make_daemon(windows_collector())
    await instance.start()
    instance.sink("wm").emit(workspace_snapshot("wm", ["1", "2"], T0, focused="2"))
    instance.sink("wm").emit(window_snapshot("wm", [Win("100", "2", "Term", 500)], T0))
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


def _page_headers(secret: str, *, bearer: bool = True) -> dict[str, str]:
    """The headers the page's ``call()`` sends on a write."""
    headers = {"Content-Type": "application/json", "Origin": ORIGIN}
    if bearer:
        headers["Authorization"] = f"Bearer {secret}"
    return headers


async def test_the_page_has_the_element_ids_and_the_meta_secret(http: httpx.AsyncClient) -> None:
    response = await http.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = _parse(response.text)
    assert page.meta["fylgja-secret"] == SECRET
    assert page.ids["todo-list"] == "ul"
    assert page.ids["todo-input"] == "input"
    assert page.ids["todo-add"] == "button"
    assert PLACEHOLDER not in response.text


async def test_the_page_is_never_cached_framed_or_loaded_from_elsewhere(
    http: httpx.AsyncClient,
) -> None:
    headers = (await http.get("/")).headers
    assert headers["cache-control"] == "no-store"
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    policy = headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "connect-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert not any(name.startswith("access-control-") for name in headers)
    assert "http://" not in policy
    assert "https://" not in policy


@pytest.mark.parametrize("host", ["evil.example", "evil.example:7319", "127.0.0.1", "localhost:80"])
async def test_a_foreign_host_never_gets_the_page_or_the_secret(
    http: httpx.AsyncClient, host: str
) -> None:
    response = await http.get("/", headers={"Host": host})
    assert response.status_code == 403
    assert SECRET not in response.text


async def test_localhost_is_accepted_as_a_host(http: httpx.AsyncClient) -> None:
    response = await http.get("/", headers={"Host": f"localhost:{PORT}"})
    assert response.status_code == 200


async def test_the_secret_is_escaped_in_the_tag(daemon: Daemon) -> None:
    nasty = 'x"><script>alert(1)</script>'
    transport = httpx.ASGITransport(app=create_app(daemon, secret=nasty))
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        html = (await client.get("/")).text
    assert nasty not in html
    assert _parse(html).meta["fylgja-secret"] == nasty


async def test_without_a_secret_the_tag_is_empty_and_writes_fail(daemon: Daemon) -> None:
    transport = httpx.ASGITransport(app=create_app(daemon, secret=""))
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        html = (await client.get("/")).text
        assert _parse(html).meta["fylgja-secret"] == ""
        response = await client.post(
            "/todos", headers=_page_headers("", bearer=False), json={"text": "x"}
        )
    assert response.status_code == 401


async def test_the_scripted_flow_adds_and_finishes_a_todo(http: httpx.AsyncClient) -> None:
    secret = _parse((await http.get("/")).text).meta["fylgja-secret"]
    now = (await http.get("/now")).json()
    assert [w["name"] for w in now["workspaces"] if w["focused"]] == ["2"]
    assert (await http.get(f"/todos?{OPEN}")).json() == []

    added = await http.post("/todos", headers=_page_headers(secret), json={"text": "write docs"})
    assert added.status_code == 201, added.text
    todo = added.json()
    assert (todo["scope_kind"], todo["scope_ref"], todo["source"]) == ("workspace", "2", "fylgja")

    # what the page lists: the focused workspace's todos and all open todos
    here = (await http.get(f"/todos?ws=2&{OPEN}")).json()
    everywhere = (await http.get(f"/todos?{OPEN}")).json()
    assert [t["id"] for t in here] == [t["id"] for t in everywhere] == [todo["id"]]
    # the page reads them grouped from its view model: the focused workspace first
    view = (await http.get("/view/overview")).json()
    assert view["focused_workspace"] == "2"
    focused, rest = view["todo_groups"]
    assert (focused["kind"], focused["title"]) == ("focused", "Focused workspace 2")
    assert [(t["id"], t["can_finish"]) for t in focused["todos"]] == [(todo["id"], True)]
    assert (rest["kind"], rest["empty"], rest["todos"]) == ("all", "Nothing else open", [])

    done = await http.patch(
        f"/todos/{todo['id']}", headers=_page_headers(secret), json={"status": "done"}
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "done"
    assert (await http.get(f"/todos?{OPEN}")).json() == []
    view = (await http.get("/view/overview")).json()
    assert [t for g in view["todo_groups"] for t in g["todos"]] == []


async def test_the_same_calls_without_the_bearer_fail(http: httpx.AsyncClient) -> None:
    secret = _parse((await http.get("/")).text).meta["fylgja-secret"]
    added = await http.post("/todos", headers=_page_headers(secret), json={"text": "keep"})
    todo_id = added.json()["id"]
    bare = _page_headers(secret, bearer=False)
    assert (await http.post("/todos", headers=bare, json={"text": "no"})).status_code == 401
    wrong = {**bare, "Authorization": "Bearer not-the-secret"}
    assert (await http.post("/todos", headers=wrong, json={"text": "no"})).status_code == 401
    patched = await http.patch(f"/todos/{todo_id}", headers=bare, json={"status": "done"})
    assert patched.status_code == 401
    assert [t["id"] for t in (await http.get(f"/todos?{OPEN}")).json()] == [todo_id]


async def test_the_page_origin_is_allowed_and_a_foreign_origin_is_not(
    http: httpx.AsyncClient,
) -> None:
    headers = _page_headers(SECRET)
    ok = await http.post("/todos", headers=headers, json={"text": "a"})
    assert ok.status_code == 201
    foreign = await http.post(
        "/todos", headers={**headers, "Origin": "https://evil.example"}, json={"text": "b"}
    )
    assert foreign.status_code == 403
    form = await http.post(
        "/todos", headers={**headers, "Content-Type": "text/plain"}, content='{"text":"c"}'
    )
    assert form.status_code == 415


async def test_the_page_script_makes_the_calls_the_flow_above_mirrors() -> None:
    source = resources.files("fylgja").joinpath("api/static/index.html").read_text(encoding="utf-8")
    assert PLACEHOLDER in source
    assert "document.querySelector('meta[name=\"fylgja-secret\"]').content" in source
    assert '"Authorization"] = "Bearer " + secret' in source
    assert '"Content-Type"] = "application/json"' in source
    assert 'call("POST", "/todos", { text: text })' in source
    assert 'call("PATCH", "/todos/" + encodeURIComponent(id), { status: "done" })' in source
    assert 'call("GET", "/view/overview")' in source
    assert "card.focused" in source
    assert "ov.focused_workspace" in source
    for element in ("todo-list", "todo-input", "todo-add"):
        assert f'id="{element}"' in source
    assert '"todo-done-" + tr.id' in source
    # the page writes text with textContent, never as markup
    assert "innerHTML" not in source
    assert not re.search(r"https?://", source)


def test_render_page_replaces_the_placeholder_once() -> None:
    html = render_page("abc")
    assert 'content="abc"' in html
    assert render_page(None).count('content=""') == 1
    assert PLACEHOLDER not in render_page("abc")


async def test_the_page_leaves_the_other_read_routes_alone(http: httpx.AsyncClient) -> None:
    for path in ("/now", "/todos", "/health"):
        response = await http.get(path)
        assert response.status_code == 200, path
        assert "text/html" not in response.headers["content-type"]
    body: Any = (await http.get("/now")).json()
    assert body["schema"] == "fylgja.now/1"
