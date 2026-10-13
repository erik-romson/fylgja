"""The navigator page at ``/``: a Playwright-free smoke check (WP7.1, A19; §4.2).

``/`` renders over the real guarded app with every element id the page and its tests use, the meta
secret and no outside asset. The JavaScript is checked statically: titles and other daemon text go
in with ``textContent``, never as markup; the page listens on ``/events`` and has no polling loop;
and it computes nothing (``docs/decisions/0047``): the daemon sorts, filters, caps, counts and picks
targets, and the page renders.
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

# What the page may not do (principle 1: the daemon computes, the page renders): sort, filter,
# reduce, search, slice, flatten, cap or subtract times. Each match the rule allows is listed in
# ALLOWED_COMPUTATION, verbatim, and removed from the script before the scan.
COMPUTATION = (
    ".sort(",
    ".filter(",
    ".reduce(",
    ".find(",
    ".some(",
    ".every(",
    ".slice(",
    ".flatMap(",
    "const MAX_",
    "Date.now() -",
    "- new Date(",
)
ALLOWED_COMPUTATION: tuple[str, ...] = ()

STATIC_IDS = {
    "next-strip": "ul",
    "workspace-cards": "div",
    "todo-list": "ul",
    "todo-input": "input",
    "todo-add": "button",
    "status": "p",
    "tabs": "nav",
    "tab-overview": "section",
    "tab-live": "section",
    "ranker": "select",
    "tab-recall": "section",
    "away-banner": "p",
    "while-away": "ul",
    "last-session": "div",
    "recall-search": "input",
    "recall-list": "div",
    "session-table": "table",
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
    # a tab link is an in-page anchor ("#live"), not an asset
    assert [asset for asset in page.assets if not asset[1].startswith("#")] == []
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
    assert '"card-" + card.name' in script
    assert '"session-" + cs.key' in script
    assert 'call("GET", "/view/overview" + rankQuery())' in script
    # every click target is the daemon's: a session or a window, never looked up here
    assert 'ft.kind === "session" ? focusSession(ft.ref) : focusWindow(ft.ref)' in script
    for target in ("go(ne.go)", "go(card.go)", "go(cs.go)"):
        assert target in script, target


def test_todos_are_grouped_by_scope_and_imported_ones_name_their_source() -> None:
    script = _script(_source())
    # the daemon groups the todos and titles each group (api/views.py); the page renders them
    assert "for (const tg of ov.todo_groups)" in script
    assert 'el("li", tg.kind === "scope" ? "group sub" : "group", tg.title)' in script
    assert "byScope" not in script
    assert "scopeTitle" not in script
    # an imported todo is finished where it came from: it gets a label, never a Done button
    assert "if (!tr.can_finish)" in script
    assert '"from " + sourceLabel(tr.source)' in script
    assert "disabled" not in script


def test_the_page_computes_nothing() -> None:
    script = _script(_source())
    for allowed in ALLOWED_COMPUTATION:
        assert allowed in script, f"stale allow-list entry: {allowed!r}"
        script = script.replace(allowed, "")
    found = [banned for banned in COMPUTATION if banned in script]
    assert not found, f"the page computes; move it into the daemon: {found}"


def test_the_overview_is_one_call_to_its_view_model() -> None:
    script = _script(_source())
    assert 'const ov = await call("GET", "/view/overview" + rankQuery());' in script
    for gone in ('"/todos?"', 'call("GET", "/next")', "nextAction", "windowOf", "WAITING"):
        assert gone not in script, gone
    # the Live data tab keeps its own /now call
    assert script.count('call("GET", "/now")') == 1


def test_times_and_durations_are_only_formatted() -> None:
    script = _script(_source())
    assert "function fmtTime(iso)" in script
    assert "function fmtDuration(seconds)" in script
    assert 'return hours + " h " + minutes + " m";' in script
    assert 'return minutes + " m";' in script
    assert 'return total + " s";' in script


def test_tabs_route_by_hash_and_load_only_the_visible_tab() -> None:
    source = _source()
    script = _script(source)
    assert 'addEventListener("hashchange"' in script
    assert "location.hash.substring(1)" in script
    assert "Object.hasOwn(TABS, name)" in script
    assert "await TABS[name]()" in script
    literal = re.search(r"const TABS = \{(.*?)\};", script, re.DOTALL)
    assert literal is not None
    keys = set(re.findall(r"(\w+)\s*:", literal.group(1)))
    views = set(re.findall(r'data-tab="(\w+)"', source))
    assert views
    assert views <= keys
    assert keys == {"overview", "recall", "live"}


def test_the_sessions_tab_never_renders_prompt_text() -> None:
    assert "facts" not in _script(_source())


def test_recall_reads_items_and_rankers() -> None:
    script = _script(_source())
    assert 'call("GET", "/items" + itemsQuery(' in script
    assert 'call("GET", "/rankers" + rankQuery())' in script
    assert "{ q: search.value }" in script  # the server searches; the page sends the text
    assert "localStorage.getItem(RANK_KEY)" in script
    assert "searchLater = setTimeout(refresh, 250)" in script
    # the page renders the daemon's page-ready fields: the choices, the one selected, whether the
    # stored choice is stale, and the counts
    for read in ("rks.choices", "rks.selected", "rks.stale_choice", "itms.shown", "itms.total"):
        assert read in script, read
    # it neither filters the rankers nor counts the items
    for banned in ("rks.rankers", "rk.loaded", "itms.items.length"):
        assert banned not in script, banned
    assert script.count("setTimeout(connect, delay)") == 1


def test_recall_shows_folder_summaries_when_enabled() -> None:
    script = _script(_source())
    assert 'call("POST", "/summaries/folder/" + encodeURIComponent(id), {})' in script
    assert "it.story && it.story.text ? it.story.text : it.summary" in script
    assert "if (summariesEnabled)" in script
    assert "itemRow(byId.get(id), itms.summaries_enabled)" in script
    assert '"/summaries/day/" + encodeURIComponent(grp.day)' in script
    assert "if (itms.summaries_enabled)" in script
