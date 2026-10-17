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
from fylgja.rankers import base, frecency, recent

# The part names the frecency ranker gives, by constant name.
FRECENCY_PARTS = (
    "FOCUS",
    "LAST_SESSION",
    "WAITING",
    "TURN_DONE_PART",
    "BUILD",
    "TODO_PART",
    "UNFINISHED",
    "FORGOTTEN",
    "WHILE_AWAY",
    "HERE_NOW",
    "STALE",
)

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
# Date.now() only spaces the reloads of the diagnostic tabs; it measures no data the daemon sent.
ALLOWED_COMPUTATION: tuple[str, ...] = ("Date.now() - lastLoad",)

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
    "focus-panel": "div",
    "window-list": "div",
    "project-table": "table",
    "usage-table": "table",
    "tab-todos": "section",
    "all-todo-list": "table",
    "tab-collectors": "section",
    "collector-table": "table",
    "switch-table": "table",
    "notify-status": "p",
    "not-installed": "ul",
    "collectors-refresh": "button",
    "tab-flow": "section",
    "flow-stages": "ol",
    "flow-collectors": "li",
    "flow-gate": "li",
    "flow-privacy": "li",
    "flow-writer": "li",
    "flow-store": "li",
    "flow-live": "li",
    "flow-api": "li",
    "flow-side": "div",
    "flow-matrix": "table",
    "tab-explain": "section",
    "label-table": "table",
    "placement-table": "table",
    "ranking-table": "table",
    "score-table": "table",
    "waiting-table": "table",
    "item-ranking-table": "table",
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
    # a tab link is an in-page anchor ("#live"); the one asset is the icon from the page's origin
    assets = [asset for asset in page.assets if not asset[1].startswith("#")]
    assert assets == [("link", "/favicon.png")]
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


def test_the_page_copies_the_attention_title_and_icon_from_the_daemon() -> None:
    script = _script(_source())
    assert 'call("GET", "/view/attention")' in script
    assert "document.title = att.title" in script
    assert 'iconLink.setAttribute("href", att.icon)' in script
    body = script.split("async function refresh(force) {", 1)[1]
    # started before the throttle check, beside the tab's loader, and awaited before the next pass
    loop = body.split("} while (again);", 1)[0]
    assert loop.index("const attention = loadAttention();") < loop.index("throttled(name)")
    assert loop.index("await TABS[name]();") < loop.index("await attention;")
    assert "continue;" not in loop


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
    # the Live data tab keeps its own /now call, in the chosen ranker's order
    assert script.count('call("GET", "/now" + rankQuery())') == 1


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
    assert keys == {"overview", "recall", "live", "todos", "collectors", "flow", "explain"}


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


def test_live_data_fills_three_parts_from_one_now_fetch() -> None:
    script = _script(_source())
    body = re.search(r"async function loadLive\(\) \{(.*?)\n\}", script, re.DOTALL)
    assert body is not None
    assert re.findall(r'call\("GET", "([^"]*)"', body.group(1)) == ["/now"]
    for part in ("renderSessions(now)", "renderWindows(now)", "renderProjects(now)"):
        assert part in body.group(1), part
    assert "focusWindow(win.id)" in script  # a window row focuses its window
    # Windows and Projects are parts of Live data (Q1), not tabs of their own
    source = _source()
    for gone in ("loadWindows", "loadProjects", "loadSessions"):
        assert gone not in script, gone
    assert 'data-tab="windows"' not in source
    assert 'data-tab="projects"' not in source


def test_the_todos_tab_is_read_only_and_asks_the_daemon_for_each_group() -> None:
    script = _script(_source())
    body = re.search(r"async function loadTodos\(\) \{(.*?)\n\}", script, re.DOTALL)
    assert body is not None
    assert re.findall(r'call\("GET", "([^"]*)"', body.group(1)) == [
        "/todos?status=next",
        "/todos?status=open",
        "/todos?status=done&order=done_desc&limit=200",
    ]
    assert "Promise.all" in body.group(1)
    assert '"Done (newest 200)"' in body.group(1)
    # the Done button and its ids belong to Overview alone
    assert script.count('"todo-done-"') == 1
    assert "newest 200 shown" not in script


def test_the_collectors_tab_reads_the_inspect_route_and_the_throttle_spaces_reloads() -> None:
    script = _script(_source())
    body = re.search(r"async function loadCollectors\(\) \{(.*?)\n\}", script, re.DOTALL)
    assert body is not None
    assert re.findall(r'call\("GET", "([^"]*)"', body.group(1)) == ["/inspect/collectors"]
    assert "JSON.stringify(value)" in script  # a setting is shown as JSON text
    assert "doctor has not checked this switch" in script
    assert 'id="notify-status"' in _source()
    assert "insp.notify.last_failure_kind" in script
    assert "insp.notify.evaluation_failures" in script
    assert 'notifyStatus.textContent = "notify: ok · "' in script
    assert (
        "const MIN_GAP_MS = { recall: 1000, collectors: 2000, flow: 2000, explain: 2000 };"
        in script
    )
    assert "const wait = (MIN_GAP_MS[name] || 0) - (Date.now() - lastLoad);" in script
    assert "const skip = !bypass && throttled(name);" in script
    assert 'addEventListener("hashchange", () => { refresh(true); })' in script
    assert (
        'getElementById("collectors-refresh").addEventListener("click", () => { refresh(true); })'
        in script
    )
    # the one expression Date.now() appears in besides setting lastLoad
    assert script.count("Date.now()") == 2
    assert "setInterval" not in script
    assert script.count("setTimeout(connect, delay)") == 1


def test_the_ranker_dropdown_shows_only_on_the_tabs_it_orders() -> None:
    script = _script(_source())
    assert 'const RANKED_TABS = new Set(["overview", "recall", "live", "explain"]);' in script
    assert "rankerSelect.hidden = !rankersLoaded || !RANKED_TABS.has(name);" in script
    assert "rankerSelect.hidden = false;" not in script


def test_a_failed_write_is_reported_after_the_reload_that_follows_it() -> None:
    script = _script(_source())
    for failure in (
        'failure = "Not added: " + error.message;',
        'failure = "Not done: " + error.message;',
    ):
        assert failure in script, failure
    assert script.count("await refresh();\n  if (failure) { say(failure); }") == 2


def test_the_collector_health_state_is_a_coloured_badge() -> None:
    source = _source()
    assert 'el("span", "health " + hd.state, hd.state)' in _script(source)
    for state in ("ok", "starting", "degraded", "failed", "disabled"):
        assert f".health.{state}" in source, state


def test_score_parts_explain_themselves_on_hover() -> None:
    script = _script(_source())
    assert "explanation.title = partsLegend(it.parts);" in script
    assert "cell.title = partsLegend(it.parts);" in script
    legend = re.search(r"const PART_HELP = \{(.*?)\n\};", script, re.DOTALL)
    assert legend is not None
    keys = set(re.findall(r'^  "?([a-z ]+)"?:', legend.group(1), re.MULTILINE))
    built_in = {
        *(getattr(frecency, name) for name in FRECENCY_PARTS),
        recent.SINCE_FOCUS,
        recent.LATEST_FOCUS,
        recent.NEVER_FOCUSED,
        recent.PEEK,
        base._WORKSPACE_PART,
        base._NO_WORKSPACE_PART,
    }
    assert keys == built_in


def test_the_flow_tab_reads_the_pipeline_route_and_has_one_throttle() -> None:
    script = _script(_source())
    body = re.search(r"async function loadFlow\(\) \{(.*?)\n\}", script, re.DOTALL)
    assert body is not None
    assert re.findall(r'call\("GET", "([^"]*)"', body.group(1)) == ["/inspect/pipeline"]
    assert "flow: loadFlow" in script
    assert script.count("MIN_GAP_MS =") == 1
    assert "Set [summaries] enabled = true" in script
    assert "pipe.notify.last_failure_kind" in script
    assert "pipe.notify.evaluation_failures" in script


def test_the_explain_tab_reads_the_explain_route_and_the_top_twenty_items() -> None:
    script = _script(_source())
    body = re.search(r"async function loadExplain\(\) \{(.*?)\n\}", script, re.DOTALL)
    assert body is not None
    assert re.findall(r'call\("GET", "([^"]*)"', body.group(1)) == ["/inspect/explain", "/items"]
    assert "itemsQuery({ limit: 20 })" in body.group(1)
    assert "explain: loadExplain" in script
    assert "const LABEL_RULES = {" in script
    assert "The resolver has not run yet" in script
    assert "Ages are active working time." in script
    assert script.count("MIN_GAP_MS =") == 1


def test_the_page_stays_one_file_within_its_size_budget() -> None:
    assert len(_source().splitlines()) <= 1550
