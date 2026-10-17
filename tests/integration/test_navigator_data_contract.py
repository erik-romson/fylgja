"""The data the navigator page uses (WP7.1, A19; ``PLAN-final-plan.md`` §4.2).

Two checks. The page's JavaScript reads fields through fixed variable names, one table per
contract: ``/now`` fields (``PAGE_VARIABLES``, checked against ``docs/api/now.schema.json``) and
the Overview's view model (``VIEW_VARIABLES``, checked against the in-memory schema of
:class:`~fylgja.api.view_models.Overview`). The tests assert that every declared field exists in
its schema and that the script reads no other. Then the endpoints the page calls (``/now``,
``/view/overview``, ``/next``, ``/events`` and the focus routes) answer as the page expects when
called the way the page calls them: the page's own ``Origin`` and the meta secret as the bearer.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable, Mapping
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
from pydantic import BaseModel

from fylgja.api.app import create_app
from fylgja.api.inspect_models import InspectCollectors, InspectExplain, InspectPipeline
from fylgja.api.items_models import Items, Rankers
from fylgja.api.view_models import Attention, Overview
from fylgja.core.daemon import Daemon
from fylgja.testing.fakes import FakeProcessRunner

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"
ORIGIN = f"http://127.0.0.1:{PORT}"
AERO = "/opt/homebrew/bin/aerospace"
SCHEMA = Path(__file__).parents[2] / "docs" / "api" / "now.schema.json"

# The JS variable that holds each /now type, and the field paths the page reads on it. Keep this
# table in step with ``api/static/index.html``: the test fails when either side has a path the
# other lacks. A name stays in its table after the page stops using it, so no later table reuses it
# for another type.
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
    "wt": "Waiting",
    "f": "Focused",
    "pres": "Presence",
    "wf": "BuildWorkflow",
    "prov": "UsageProvider",
}
PAGE_FIELDS: dict[str, set[str]] = {
    ROOT: {
        "focused",
        "presence",
        "projects",
        "sessions",
        "usage",
        "usage.providers",
        "workspaces",
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
        "one_liner",
        "one_liner_from",
        "project_path",
        "state",
        "state_since",
        "story",
        "story.state",
        "tokens",
        "tokens.input",
        "tokens.output",
        "waiting_reason",
    },
    "Host": {"confidence", "kind", "unresolved", "via", "workspace"},
    "Workspace": {"label", "name", "windows"},
    "Window": {"app", "bundle_id", "context", "focused", "id", "title"},
    "ContextItem": {"confidence", "kind", "observed_at", "source", "title", "unread"},
    "Focused": {"app_name", "bundle_id", "since", "title", "workspace"},
    "Presence": {"in_call", "observed_at", "since", "source", "state"},
    "Project": {
        "ahead",
        "behind",
        "branch",
        "build",
        "build.error_kind",
        "build.freshness",
        "build.state",
        "build.workflows",
        "dirty",
        "dirty_count",
        "last_commit_at",
        "name",
        "path",
        "pr",
        "pr.checks",
        "pr.number",
        "pr.review_decision",
        "pr.state",
        "sessions",
        "workspace",
    },
    "BuildWorkflow": {"conclusion", "state", "workflow"},
    "UsageProvider": {
        "error_kind",
        "freshness",
        "observed_at",
        "provider",
        "resets_at",
        "source",
        "used_pct",
        "window",
    },
    "NextItem": {"kind", "rank", "reason", "score", "target_ref"},
    "Waiting": {"kind", "reason", "since", "target"},
    "Todo": {"created_at", "done_at", "scope_kind", "scope_ref", "source", "status", "text"},
}

# The Overview renders ``GET /view/overview`` (``fylgja.view.overview/1``) only. Its variables, the
# view-model type each holds and the fields the page reads on it.
VIEW_VARIABLES: dict[str, str] = {
    "ov": "Overview",
    "card": "Card",
    "cs": "CardSession",
    "cp": "CardProject",
    "cc": "CardContext",
    "ct": "CardTodo",
    "ne": "NextEntry",
    "tg": "TodoGroup",
    "tr": "TodoRow",
    "ft": "FocusTarget",
    "att": "Attention",
}
VIEW_FIELDS: dict[str, set[str]] = {
    "Attention": {"icon", "title"},
    "Overview": {"cards", "focused_workspace", "next", "todo_groups"},
    "Card": {
        "context",
        "focused",
        "go",
        "label",
        "name",
        "one_liner",
        "projects",
        "sessions",
        "todos",
        "windows",
    },
    "CardSession": {"child", "go", "key", "state", "text", "waiting"},
    "CardProject": {
        "branch",
        "build_failed",
        "build_state",
        "dirty",
        "name",
        "pr_checks",
        "pr_number",
    },
    "CardContext": {"text", "unread"},
    "CardTodo": {"next", "text"},
    "NextEntry": {"action", "folder", "go", "kind", "rank", "reason", "since", "title"},
    "TodoGroup": {"empty", "kind", "title", "todos"},
    "TodoRow": {"can_finish", "id", "next", "source", "text"},
    "FocusTarget": {"kind", "ref"},
}

# The Recall tab renders ``GET /items`` (``fylgja.items/1``) and ``GET /rankers``
# (``fylgja.rankers/1``) only. ``rk`` is a ``RankerChoice``, the page-ready ranker: nothing of
# ``RankerInfo`` (``loaded``, ``enabled``, ``error``) is declared, so a read of it fails.
ITEMS_VARIABLES: dict[str, str] = {
    "itms": "Items",
    "it": "NavItem",
    "pl": "ItemPlace",
    "gt": "GoTarget",
    "grp": "DayGroup",
    "chg": "AwayChange",
    "ls": "LastSession",
    "rks": "Rankers",
    "rk": "RankerChoice",
    "spt": "ScorePart",
}
ITEMS_FIELDS: dict[str, set[str]] = {
    "Items": {
        "activity.away_since",
        "activity.away_until",
        "days",
        "items",
        "last_session",
        "ranker",
        "ranker_fallback",
        "shown",
        "summaries_enabled",
        "total",
        "while_away",
    },
    "NavItem": {
        "explanation",
        "go",
        "go_agent",
        "go_ide",
        "go_terminal",
        "id",
        "name",
        "parts",
        "places",
        "score",
        "seconds_today",
        "state",
        "story",
        "story.text",
        "summary",
    },
    "ItemPlace": {"label", "last_focus_at", "open", "title", "workspace"},
    "DayGroup": {"day", "items"},
    "AwayChange": {"go", "kind", "name", "reason", "since"},
    "LastSession": {"active_seconds", "end", "start"},
    "Rankers": {"choices", "selected", "stale_choice"},
    "RankerChoice": {"default", "description", "name"},
    "ScorePart": {"name", "value"},
}

# The Collectors tab renders ``GET /inspect/collectors`` (``fylgja.inspect.collectors/1``) only.
INSPECT_VARIABLES: dict[str, str] = {
    "insp": "InspectCollectors",
    "col": "InspectCollector",
    "hd": "HealthDetail",
    "rc": "RecordCounts",
    "kc": "KindCount",
    "sw": "AppleScriptSwitch",
    "pipe": "InspectPipeline",
    "fc": "FlowCount",
    "ex": "InspectExplain",
    "lab": "LabelExplain",
    "pp": "ProjectPlacementInfo",
    "sp": "SessionPlacementInfo",
    "rr": "RankingRule",
}
INSPECT_FIELDS: dict[str, set[str]] = {
    "InspectCollectors": {
        "applescript",
        "collectors",
        "generated_at",
        "not_installed",
        "notify.enabled",
        "notify.evaluation_failures",
        "notify.failed",
        "notify.last_evaluation_failure_at",
        "notify.last_failure_at",
        "notify.last_failure_kind",
        "notify.sent",
        "started_at",
    },
    "InspectCollector": {
        "api_version",
        "capabilities",
        "distribution",
        "distribution_version",
        "enabled",
        "entity_kinds",
        "health",
        "loaded",
        "name",
        "origin",
        "records",
        "settings",
        "state",
    },
    "HealthDetail": {
        "detail",
        "last_error_kind",
        "last_ok_at",
        "restarts",
        "retry_in_seconds",
        "since",
        "state",
    },
    "RecordCounts": {"accepted", "dropped", "kinds", "last_at", "lost", "rejected"},
    "KindCount": {"accepted", "entity_kind", "last_at", "record_type"},
    "AppleScriptSwitch": {"configured", "doctor_at", "doctor_ok", "doctor_reason", "name"},
    "InspectPipeline": {
        "collectors_running",
        "events.clients",
        "events.max_clients",
        "flows",
        "gate.accepted",
        "gate.dropped",
        "gate.lost",
        "gate.rejected",
        "generated_at",
        "live_view.rows",
        "live_view.version",
        "messaging_enabled",
        "notify.enabled",
        "notify.evaluation_failures",
        "notify.failed",
        "notify.last_evaluation_failure_at",
        "notify.last_failure_at",
        "notify.last_failure_kind",
        "notify.open",
        "notify.sent",
        "privacy.ignore_bundle_ids",
        "privacy.ignore_title_patterns",
        "privacy.redact_patterns",
        "started_at",
        "summaries.cached",
        "summaries.calls",
        "summaries.enabled",
        "summaries.jobs",
        "summaries.max_concurrent",
        "summaries.running",
        "summaries.ttl_seconds",
        "writer.batch_max",
        "writer.batch_mean",
        "writer.duplicates",
        "writer.failed_items",
        "writer.failed_transactions",
        "writer.items",
        "writer.pending",
        "writer.running",
        "writer.transactions",
    },
    "FlowCount": {"accepted", "collector", "entity_kind", "last_at", "record_type"},
    "InspectExplain": {
        "generated_at",
        "idle_window_seconds",
        "labels",
        "max_items",
        "next",
        "projects",
        "ranking",
        "resolved",
        "sessions",
        "waiting",
    },
    "LabelExplain": {"inferred_label", "label", "label_source", "rule", "workspace"},
    "ProjectPlacementInfo": {
        "confidence",
        "name",
        "sessions",
        "unresolved",
        "via",
        "windows",
        "workspace",
    },
    "SessionPlacementInfo": {
        "confidence",
        "note",
        "project_confidence",
        "project_via",
        "session_key",
        "unresolved",
        "via",
        "window_id",
        "workspace",
    },
    "RankingRule": {"kind", "score"},
}

Resolver = Callable[[str, str], bool]
"""Whether a field path exists on a type: ``resolver(kind, path)``."""


def _source() -> str:
    return resources.files("fylgja").joinpath("api/static/index.html").read_text(encoding="utf-8")


def _script() -> str:
    match = re.search(r"<script>(.*)</script>", _source(), re.DOTALL)
    assert match is not None
    return match.group(1)


def _js_reads(variables: Mapping[str, str]) -> dict[str, set[str]]:
    """The field paths the script reads, per type, from ``variable.field.path`` expressions over
    the variable names of ``variables`` (a table of variable name to type)."""
    names = "|".join(sorted(variables, key=len, reverse=True))
    found: dict[str, set[str]] = {}
    for match in re.finditer(rf"(?<![\w.\"'])({names})((?:\.[a-z_]+)+)(?!\w*\()", _script()):
        path = match.group(2)[1:]
        found.setdefault(variables[match.group(1)], set()).add(path)
    return found


def _resolve(schema: dict[str, Any], kind: str, path: str, *, root: str = ROOT) -> bool:
    """Whether ``path`` exists on ``kind`` in the schema (following ``$ref`` and ``anyOf``);
    ``root`` names the schema's own type."""
    definitions = schema.get("$defs", {})
    if kind != root and kind not in definitions:
        return False
    node: dict[str, Any] = schema if kind == root else definitions[kind]
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


def _file_resolver(path: Path) -> Resolver:
    """Resolves against a JSON Schema file whose root type is :data:`ROOT`."""
    schema = json.loads(path.read_text(encoding="utf-8"))
    return lambda kind, field: _resolve(schema, kind, field)


def _model_resolver(*models: type[BaseModel]) -> Resolver:
    """Resolves against the in-memory schemas of ``models``: a model's own name is the root of its
    schema, any other name is one of its ``$defs``."""
    schemas = {
        model.__name__: model.model_json_schema(by_alias=True, mode="serialization")
        for model in models
    }

    def resolve(kind: str, path: str) -> bool:
        return any(_resolve(schema, kind, path, root=name) for name, schema in schemas.items())

    return resolve


def _assert_declared_fields_exist(
    fields: Mapping[str, set[str]], resolve: Resolver, where: str
) -> None:
    for kind, paths in fields.items():
        for path in sorted(paths):
            assert resolve(kind, path), f"{kind}.{path} is not in {where}"


def _assert_reads_match(variables: Mapping[str, str], fields: Mapping[str, set[str]]) -> None:
    reads = _js_reads(variables)
    assert set(reads) <= set(fields)
    for kind, paths in reads.items():
        # a declared path also declares its parents: s.story.state reads s.story
        extra = {p for p in paths if p not in fields[kind]}
        assert not extra, f"the page reads undeclared {kind} fields: {sorted(extra)}"
    for kind, declared in fields.items():
        # fields the script touches only through a prefix are covered by their longer path
        used = reads.get(kind, set())
        unused = {
            d for d in declared if d not in used and not any(u.startswith(d + ".") for u in used)
        }
        assert not unused, f"declared but not read on {kind}: {sorted(unused)}"


def test_every_field_the_page_reads_exists_in_the_now_schema() -> None:
    _assert_declared_fields_exist(PAGE_FIELDS, _file_resolver(SCHEMA), "now.schema.json")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["properties"]["schema"]["const"] == "fylgja.now/1"


def test_the_script_reads_only_the_declared_fields_and_all_of_them() -> None:
    _assert_reads_match(PAGE_VARIABLES, PAGE_FIELDS)


def test_every_field_the_overview_reads_exists_in_its_view_model() -> None:
    _assert_declared_fields_exist(VIEW_FIELDS, _model_resolver(Overview, Attention), "the views")
    schema = Overview.model_json_schema(by_alias=True, mode="serialization")
    assert schema["properties"]["schema"]["const"] == "fylgja.view.overview/1"
    attention = Attention.model_json_schema(by_alias=True, mode="serialization")
    assert attention["properties"]["schema"]["const"] == "fylgja.view.attention/1"


def test_the_overview_reads_only_the_declared_view_fields_and_all_of_them() -> None:
    _assert_reads_match(VIEW_VARIABLES, VIEW_FIELDS)


def test_the_overview_reads_nothing_from_now() -> None:
    script = _script()
    body = re.search(r"async function loadOverview\(\) \{(.*?)\n\}", script, re.DOTALL)
    assert body is not None
    assert re.findall(r'call\("GET", "([^"]*)"', body.group(1)) == ["/view/overview"]


def test_every_field_recall_reads_exists_in_the_items_and_rankers_models() -> None:
    _assert_declared_fields_exist(ITEMS_FIELDS, _model_resolver(Items, Rankers), "Items, Rankers")
    resolve = _model_resolver(Items, Rankers)
    assert not resolve("RankerChoice", "loaded")  # a diagnostic field is not page-ready
    assert not resolve("RankerChoice", "error")


def test_recall_reads_only_the_declared_items_fields_and_all_of_them() -> None:
    _assert_reads_match(ITEMS_VARIABLES, ITEMS_FIELDS)


def test_the_recall_tab_reads_nothing_from_now() -> None:
    script = _script()
    body = re.search(r"async function loadRecall\(\) \{(.*?)\n\}", script, re.DOTALL)
    assert body is not None
    assert re.findall(r'call\("GET", "([^"]*)"', body.group(1)) == ["/items"]
    rankers = re.search(r"async function loadRankers\(\) \{(.*?)\n\}", script, re.DOTALL)
    assert rankers is not None
    assert re.findall(r'call\("GET", "([^"]*)"', rankers.group(1)) == ["/rankers"]


def test_every_field_the_collectors_tab_reads_exists_in_the_inspect_model() -> None:
    _assert_declared_fields_exist(
        INSPECT_FIELDS,
        _model_resolver(InspectCollectors, InspectPipeline, InspectExplain),
        "the inspect models",
    )
    schema = InspectCollectors.model_json_schema(by_alias=True, mode="serialization")
    assert schema["properties"]["schema"]["const"] == "fylgja.inspect.collectors/1"


def test_the_collectors_tab_reads_only_the_declared_inspect_fields_and_all_of_them() -> None:
    _assert_reads_match(INSPECT_VARIABLES, INSPECT_FIELDS)


def test_no_variable_name_is_in_two_tables() -> None:
    tables = (PAGE_VARIABLES, VIEW_VARIABLES, ITEMS_VARIABLES, INSPECT_VARIABLES)
    names = [name for table in tables for name in table]
    assert len(names) == len(set(names))


def test_the_resolvers_reject_what_the_schemas_lack() -> None:
    view = _model_resolver(Overview, Attention)
    assert view("Attention", "title")
    assert view("Card", "sessions")
    assert view("Overview", "todo_groups")
    assert not view("Card", "windows.id")
    assert not view("Card", "nope")
    assert not view("Workspace", "name")  # a /now type is not in the view model
    now = _file_resolver(SCHEMA)
    assert now("Workspace", "name")
    assert not now("Card", "name")


def _root_fields(fields: set[str]) -> set[str]:
    return {path.split(".")[0] for path in fields}


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


async def test_items_and_rankers_have_what_the_recall_tab_reads(http: httpx.AsyncClient) -> None:
    items = (await http.get("/items")).json()
    assert items["schema"] == "fylgja.items/1"
    schema = Items.model_json_schema(by_alias=True, mode="serialization")
    # a field without a value is absent, so only the always-present ones must be there
    for name in _root_fields(ITEMS_FIELDS["Items"]) & set(schema["required"]):
        assert name in items, name
    assert set(items) <= set(schema["properties"])
    assert items["shown"] == len(items["items"])
    assert items["total"] >= items["shown"]
    for item in items["items"]:
        for name in ("id", "name", "explanation", "seconds_today", "state", "places"):
            assert name in item, name
    rankers = (await http.get("/rankers")).json()
    assert rankers["schema"] == "fylgja.rankers/1"
    for name in ITEMS_FIELDS["Rankers"]:
        assert name in rankers, name
    for choice in rankers["choices"]:
        assert {"name", "default"} <= set(choice)
    assert rankers["selected"] in {choice["name"] for choice in rankers["choices"]} | {"natural"}
    stale = (await http.get("/rankers", params={"rank": "no-such-ranker"})).json()
    assert stale["stale_choice"] is True
    assert stale["selected"] == stale["default"]


async def test_inspect_collectors_has_every_root_field_the_page_reads(
    http: httpx.AsyncClient,
) -> None:
    answer = await http.get("/inspect/collectors")
    assert answer.status_code == 200
    body = answer.json()
    assert body["schema"] == "fylgja.inspect.collectors/1"
    for name in _root_fields(INSPECT_FIELDS["InspectCollectors"]) - {"started_at"}:
        assert name in body, name
    for collector in body["collectors"]:
        assert {"name", "origin", "state", "enabled", "loaded", "health"} <= set(collector)
        assert "state" in collector["health"]


async def test_inspect_pipeline_has_every_root_field_the_page_reads(
    http: httpx.AsyncClient,
) -> None:
    answer = await http.get("/inspect/pipeline")
    assert answer.status_code == 200
    body = answer.json()
    assert body["schema"] == "fylgja.inspect.pipeline/1"
    for name in _root_fields(INSPECT_FIELDS["InspectPipeline"]) - {"started_at"}:
        assert name in body, name
    for flow in body["flows"]:
        assert {"collector", "entity_kind", "record_type", "accepted"} <= set(flow)


async def test_inspect_explain_has_every_root_field_the_page_reads(
    http: httpx.AsyncClient,
) -> None:
    answer = await http.get("/inspect/explain")
    assert answer.status_code == 200
    body = answer.json()
    assert body["schema"] == "fylgja.inspect.explain/1"
    for name in _root_fields(INSPECT_FIELDS["InspectExplain"]):
        assert name in body, name
    for label in body["labels"]:
        assert {"workspace", "label", "label_source"} <= set(label)
    for rule in body["ranking"]:
        assert {"kind", "score"} <= set(rule)


async def test_todos_answers_the_raw_tab(http: httpx.AsyncClient) -> None:
    response = await http.get("/todos?status=open,next,done")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
    for query in (
        "/todos?status=next",
        "/todos?status=open",
        "/todos?status=done&order=done_desc&limit=200",
    ):
        answer = await http.get(query)
        assert answer.status_code == 200, query
        assert isinstance(answer.json(), list), query


async def test_attention_answers_as_the_page_expects(http: httpx.AsyncClient) -> None:
    response = await http.get("/view/attention", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "fylgja.view.attention/1"
    for name in sorted(VIEW_FIELDS["Attention"]):
        assert isinstance(body[name], str), name


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
