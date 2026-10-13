"""The data the navigator page uses (WP7.1, A19; ``PLAN-final-plan.md`` §4.2).

Two checks. The page's JavaScript reads ``/now`` fields through a few fixed variable names; the
declared table below lists them, and the test asserts that every one exists in
``docs/api/now.schema.json`` and that the script reads no other. Then the endpoints the page calls
(``/now``, ``/next``, ``/events`` and the focus routes) answer as the page expects when called the
way the page calls them: the page's own ``Origin`` and the meta secret as the bearer.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import pytest
from api_support import (
    BASE_URL,
    PORT,
    T0,
    Win,
    make_daemon,
    session,
    session_snapshot,
    sessions_collector,
    window_snapshot,
    windows_collector,
    workspace_snapshot,
)

from fylgja.api.app import create_app
from fylgja.core.daemon import Daemon
from fylgja.testing.fakes import FakeProcessRunner

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"
ORIGIN = f"http://127.0.0.1:{PORT}"
AERO = "/opt/homebrew/bin/aerospace"
SCHEMA = Path(__file__).parents[2] / "docs" / "api" / "now.schema.json"

# The JS variable that holds each /now type, and the field paths the page reads on it. Keep this
# table in step with ``api/static/index.html``: the test fails when either side has a path the
# other lacks.
ROOT = "<now>"
PAGE_VARIABLES: dict[str, str] = {
    "now": ROOT,
    "current": ROOT,
    "w": "Workspace",
    "target": "Workspace",
    "s": "Session",
    "h": "Host",
    "a": "Session",
    "b": "Session",
    "p": "Project",
    "win": "Window",
    "c": "ContextItem",
    "todo": "Todo",
    "t": "Todo",
    "n": "NextItem",
}
PAGE_FIELDS: dict[str, set[str]] = {
    ROOT: {
        "focused",
        "focused.workspace",
        "next",
        "projects",
        "sessions",
        "workspaces",
    },
    "Workspace": {
        "focused",
        "inferred_label",
        "label",
        "name",
        "one_liner",
        "projects",
        "sessions",
        "todos",
        "windows",
    },
    "Session": {
        "children",
        "context_pct",
        "current_target",
        "current_tool",
        "host",
        "key",
        "kind",
        "last_seen",
        "name",
        "one_liner",
        "one_liner_from",
        "project_path",
        "state",
        "state_since",
        "story",
        "story.state",
        "title",
        "tokens",
        "tokens.input",
        "tokens.output",
        "waiting_reason",
    },
    "Host": {"confidence", "kind", "unresolved", "via", "workspace"},
    "Project": {
        "branch",
        "build",
        "build.state",
        "dirty",
        "name",
        "path",
        "pr",
        "pr.checks",
        "pr.number",
        "workspace",
    },
    "Window": {"context", "focused", "id"},
    "ContextItem": {"kind", "title", "unread"},
    "Todo": {
        "id",
        "scope_kind",
        "scope_ref",
        "source",
        "status",
        "text",
    },
    "NextItem": {"kind", "rank", "reason", "target_ref"},
}


def _source() -> str:
    return resources.files("fylgja").joinpath("api/static/index.html").read_text(encoding="utf-8")


def _script() -> str:
    match = re.search(r"<script>(.*)</script>", _source(), re.DOTALL)
    assert match is not None
    return match.group(1)


def _js_reads() -> dict[str, set[str]]:
    """The field paths the script reads, per type, from ``variable.field.path`` expressions."""
    names = "|".join(sorted(PAGE_VARIABLES, key=len, reverse=True))
    found: dict[str, set[str]] = {}
    for match in re.finditer(rf"(?<![\w.\"'])({names})((?:\.[a-z_]+)+)(?!\w*\()", _script()):
        path = match.group(2)[1:]
        found.setdefault(PAGE_VARIABLES[match.group(1)], set()).add(path)
    return found


def _resolve(schema: dict[str, Any], kind: str, path: str) -> bool:
    """Whether ``path`` exists on ``kind`` in the schema (following ``$ref`` and ``anyOf``)."""
    node: dict[str, Any] = schema if kind == ROOT else schema["$defs"][kind]
    for part in path.split("."):
        node = _deref(schema, node)
        properties = node.get("properties", {})
        if part not in properties:
            return False
        node = properties[part]
    return True


def _deref(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    for _ in range(5):
        if "$ref" in node:
            node = schema["$defs"][node["$ref"].rsplit("/", 1)[1]]
        elif "anyOf" in node:
            options = [o for o in node["anyOf"] if o.get("type") != "null"]
            node = options[0] if options else {}
        else:
            break
    return node


def test_every_field_the_page_reads_exists_in_the_now_schema() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    for kind, paths in PAGE_FIELDS.items():
        for path in sorted(paths):
            assert _resolve(schema, kind, path), f"{kind}.{path} is not in now.schema.json"
    assert schema["properties"]["schema"]["const"] == "fylgja.now/1"


def test_the_script_reads_only_the_declared_fields_and_all_of_them() -> None:
    reads = _js_reads()
    assert set(reads) <= set(PAGE_FIELDS)
    for kind, paths in reads.items():
        # a declared path also declares its parents: now.focused.workspace reads now.focused
        extra = {p for p in paths if p not in PAGE_FIELDS[kind]}
        assert not extra, f"the page reads undeclared {kind} fields: {sorted(extra)}"
    for kind, declared in PAGE_FIELDS.items():
        # fields the script touches only through a prefix are covered by their longer path
        used = reads.get(kind, set())
        unused = {
            d for d in declared if d not in used and not any(u.startswith(d + ".") for u in used)
        }
        assert not unused, f"declared but not read on {kind}: {sorted(unused)}"


@pytest.fixture
def runner() -> FakeProcessRunner:
    runner = FakeProcessRunner()
    runner.script(AERO)
    return runner


@pytest.fixture
async def daemon(runner: FakeProcessRunner) -> AsyncIterator[Daemon]:
    instance = make_daemon(windows_collector(), sessions_collector(), runner=runner)
    await instance.start()
    instance.sink("wm").emit(workspace_snapshot("wm", ["1", "2"], T0, focused="2"))
    instance.sink("wm").emit(
        window_snapshot(
            "wm",
            [Win("100", "2", "Term", 500, "a title"), Win("101", "1", "Editor", 600, "other")],
            T0,
        )
    )
    instance.sink("agents").emit(
        session_snapshot(
            "agents", [session("claude", "s-1", pid=501, cwd="/work/a", host_pid=500)], T0
        )
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


def _page_headers(secret: str) -> dict[str, str]:
    """What the page's ``call()`` sends on a write."""
    return {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "Origin": ORIGIN,
    }


async def test_now_has_what_the_page_reads(http: httpx.AsyncClient) -> None:
    now = (await http.get("/now")).json()
    assert now["schema"] == "fylgja.now/1"
    workspaces = {w["name"]: w for w in now["workspaces"]}
    assert set(workspaces) == {"1", "2"}
    for workspace in workspaces.values():
        for field in ("name", "focused", "windows", "sessions", "projects"):
            assert field in workspace, field
        for window in workspace["windows"]:
            assert {"id", "focused"} <= set(window)
    assert [w["name"] for w in workspaces.values() if w["focused"]] == ["2"]
    sessions = {s["key"]: s for s in now["sessions"]}
    assert "claude:s-1" in sessions
    assert "claude:s-1" in workspaces["2"]["sessions"]
    assert {"key", "kind"} <= set(sessions["claude:s-1"])


async def test_next_answers_as_the_page_expects(http: httpx.AsyncClient) -> None:
    response = await http.get("/next", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["items"], list)
    assert body["list_id"].startswith("nl-")
    for item in body["items"]:
        assert {"rank", "kind", "reason", "target_ref"} <= set(item)
    now = (await http.get("/now")).json()
    assert isinstance(now["next"], list)


async def test_the_focus_calls_the_page_makes_work(
    http: httpx.AsyncClient, runner: FakeProcessRunner
) -> None:
    secret = re.search(r'name="fylgja-secret" content="([^"]*)"', (await http.get("/")).text)
    assert secret is not None
    headers = _page_headers(secret.group(1))
    window = await http.post("/focus/window/100", headers=headers, content=json.dumps({}))
    assert window.status_code == 200, window.text
    assert (AERO, "focus", "--window-id", "100") in runner.calls
    # the session key has a colon: the page sends it through encodeURIComponent
    key = "claude%3As-1"
    focused = await http.post(f"/focus/session/{key}", headers=headers, content=json.dumps({}))
    assert focused.status_code == 200, focused.text
    assert (await http.post("/focus/window/999", headers=headers, content="{}")).status_code == 404
    assert (
        await http.post("/focus/session/nope", headers=headers, content="{}")
    ).status_code == 404


async def test_the_focus_calls_need_the_bearer_and_the_page_origin(http: httpx.AsyncClient) -> None:
    good = _page_headers(SECRET)
    bare = {k: v for k, v in good.items() if k != "Authorization"}
    assert (await http.post("/focus/window/100", headers=bare, content="{}")).status_code == 401
    foreign = {**good, "Origin": "https://evil.example"}
    assert (await http.post("/focus/window/100", headers=foreign, content="{}")).status_code == 403


async def test_events_answer_the_page_with_now_changed(daemon: Daemon) -> None:
    app = create_app(daemon, secret=SECRET)
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    chunks: asyncio.Queue[bytes] = asyncio.Queue()
    started: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

    async def receive() -> dict[str, Any]:
        return await incoming.get()

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            started.set_result(message)
        elif message["type"] == "http.response.body" and message.get("body"):
            chunks.put_nowait(message["body"])

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/events",
        "raw_path": b"/events",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", f"127.0.0.1:{PORT}".encode()),
            (b"origin", ORIGIN.encode()),
            (b"accept", b"text/event-stream"),
        ],
        "server": ("127.0.0.1", PORT),
        "client": ("127.0.0.1", 50000),
    }
    incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    task = asyncio.create_task(app(scope, receive, send))
    try:
        start = await asyncio.wait_for(started, 5)
        headers = {k.decode(): v.decode() for k, v in start["headers"]}
        assert start["status"] == 200
        assert headers["content-type"].startswith("text/event-stream")
        daemon.sink("wm").emit(window_snapshot("wm", [Win("100", "2", "Term", 500, "changed")], T0))
        await daemon.settle()
        seen = ""
        while "event: now.changed" not in seen:
            seen += (await asyncio.wait_for(chunks.get(), 2)).decode()
        assert "changed" not in seen.replace("now.changed", "")  # no title in the frame
    finally:
        incoming.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(task, 5)
