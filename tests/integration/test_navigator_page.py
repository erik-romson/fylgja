"""The navigator page at ``/``: a Playwright-free smoke check (WP7.1, A19; §4.2).

``/`` renders over the real guarded app with every element id the page and its tests use, the meta
secret and no outside asset. The JavaScript is checked statically: titles and other daemon text go
in with ``textContent``, never as markup; the page listens on ``/events`` and has no polling loop.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from html.parser import HTMLParser
from importlib import resources

import httpx
import pytest
from api_support import (
    BASE_URL,
    T0,
    Win,
    make_daemon,
    window_snapshot,
    windows_collector,
    workspace_snapshot,
)

from fylgja.api.app import create_app
from fylgja.api.routes.page import PLACEHOLDER
from fylgja.core.daemon import Daemon

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"
STATIC_IDS = {
    "next-strip": "ul",
    "workspace-cards": "div",
    "todo-list": "ul",
    "todo-input": "input",
    "todo-add": "button",
    "status": "p",
}


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: dict[str, str] = {}
        self.meta: dict[str, str] = {}
        self.assets: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if "id" in values:
            self.ids[values["id"]] = tag
        if tag == "meta" and "name" in values:
            self.meta[values["name"]] = values.get("content", "")
        if tag == "script" and "src" in values:
            self.assets.append((tag, values["src"]))
        if tag == "link" and "href" in values:
            self.assets.append((tag, values["href"]))
        for name in ("src", "href", "srcset", "data"):
            if name in values and tag not in ("script", "link"):
                self.assets.append((tag, values[name]))


def _source() -> str:
    return resources.files("fylgja").joinpath("api/static/index.html").read_text(encoding="utf-8")


def _script(source: str) -> str:
    match = re.search(r"<script>(.*)</script>", source, re.DOTALL)
    assert match is not None
    return match.group(1)


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


async def test_the_page_renders_with_every_element_id_and_the_secret(
    http: httpx.AsyncClient,
) -> None:
    response = await http.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = _Page()
    page.feed(response.text)
    assert page.meta["fylgja-secret"] == SECRET
    for element, tag in STATIC_IDS.items():
        assert page.ids.get(element) == tag, element
    assert PLACEHOLDER not in response.text


async def test_the_page_is_cached_by_nobody(http: httpx.AsyncClient) -> None:
    headers = (await http.get("/")).headers
    assert headers["cache-control"] == "no-store"
    assert headers["pragma"] == "no-cache"
    assert not any(name.startswith("access-control-") for name in headers)


def test_the_page_loads_nothing_from_outside() -> None:
    source = _source()
    page = _Page()
    page.feed(source)
    assert page.assets == []
    assert not re.search(r"https?://", source)
    assert not re.search(r"@import|url\(", source)
    assert "document.cookie" not in source


def test_titles_and_text_go_in_as_text_never_as_markup() -> None:
    script = _script(_source())
    for banned in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "createContextualFragment",
        "DOMParser",
        "eval(",
        "new Function",
    ):
        assert banned not in script, banned
    assert "node.textContent = text" in script  # the one helper every card line goes through
    assert "el(" in script


def test_the_page_updates_over_sse_and_never_polls() -> None:
    script = _script(_source())
    assert 'new EventSource("/events")' in script
    assert 'addEventListener("now.changed"' in script
    assert "setInterval" not in script
    assert script.count("setTimeout(connect, delay)") == 1  # the reconnect backoff
    # the first paint waits for /now only, not for the stream
    assert script.rstrip().endswith("refresh();\nconnect();")


def test_the_page_focuses_through_the_guarded_focus_api() -> None:
    script = _script(_source())
    assert '"/focus/window/" + encodeURIComponent(id)' in script
    assert '"/focus/session/" + encodeURIComponent(key)' in script
    assert 'call("POST", path, {})' in script
    assert '"Authorization"] = "Bearer " + secret' in script
    assert '"Content-Type"] = "application/json"' in script
    assert "document.querySelector('meta[name=\"fylgja-secret\"]').content" in script
    assert '"card-" + w.name' in script
    assert '"session-" + s.key' in script
    assert 'call("GET", "/next")' in script


def test_todos_are_grouped_by_scope_and_imported_ones_name_their_source() -> None:
    script = _script(_source())
    assert "for (const scope of byScope(rest))" in script
    assert "scopeTitle(scope.kind, scope.ref, sessions, projects)" in script
    assert "s.title || s.one_liner || s.name" in script
    # an imported todo is finished where it came from: it gets a label, never a Done button
    assert '"from " + sourceLabel(todo.source)' in script
    assert "disabled" not in script
