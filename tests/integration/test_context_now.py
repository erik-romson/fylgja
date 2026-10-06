"""Context items reach ``/now`` (WP5.3): ``focused.context[]``, ``workspaces[].windows[].context[]``
and the third source of the workspace one-liner.

A synthetic collector with the ``context`` capability emits the records the ``appcontext``
collector builds (:func:`fylgja.collectors.appcontext.parse.to_record`), so the core is shown to
serve any plugin the same way. The ``/now`` schema is unchanged: these are existing fields.
"""

from __future__ import annotations

from typing import Any

from api_support import (
    T0,
    Synthetic,
    Win,
    by_key,
    client,
    focus_event,
    make_daemon,
    window_snapshot,
)
from appcontext_support import PYCHARM, SLACK  # noqa: I001

from fylgja.collectors.appcontext import parse
from fylgja.collectors.appcontext.titles import parse_title
from fylgja.collectors.base import Capability, CollectorContext
from fylgja.core.daemon import Daemon
from fylgja.models import HealthState, Record
from fylgja.testing.fakes import FakeClock

IDE = Win("7", "3", "IDE", 70, "proj – main.py", bundle=PYCHARM)
CHAT = Win("8", "3", "Chat", 80, "general (Channel) - Team - 2 new items - Slack", bundle=SLACK)


def _from_ctx(record: Record) -> Record:
    """The record as the synthetic ``ctx`` plugin emits it (a record's source is its plugin)."""
    return record.model_copy(update={"source": "ctx"})  # type: ignore[no-any-return]


def _facts(win: Win) -> parse.WindowFacts:
    return parse.WindowFacts(win.window_id, win.bundle, win.title, win.workspace)


def _title_record(win: Win) -> Record:
    context = parse_title(win.bundle, win.title)
    assert context is not None
    return _from_ctx(parse.to_record(parse.title_item(context), _facts(win), T0))


def _second_tier(win: Win, **fields: Any) -> Record:
    item = parse.Item(
        key="jetbrains",
        kind="ide_project",
        tier=2,
        title="proj",
        fields=fields,
        confidence="high",
        link="obsidian://open?vault=x",
    )
    return _from_ctx(parse.to_record(item, _facts(win), T0))


def _windows(context: CollectorContext) -> None:
    context.sink.emit(window_snapshot("wm", [IDE, CHAT], T0))
    context.sink.emit(focus_event("wm", "7", "3", T0))


def _items(context: CollectorContext) -> None:
    for record in (_title_record(IDE), _title_record(CHAT), _second_tier(IDE, path="/work/proj")):
        context.sink.emit(record)


def _daemon() -> Daemon:
    wm = Synthetic("wm", frozenset({Capability.WINDOWS}), emit=_windows)
    ctx = Synthetic("ctx", frozenset({Capability.CONTEXT}), emit=_items)
    return make_daemon(wm, ctx)


async def _now(daemon: Daemon) -> dict[str, Any]:
    async with client(daemon) as http:
        response = await http.get("/now")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_context_fills_the_focused_window_and_its_workspace() -> None:
    daemon = _daemon()
    await daemon.start()
    try:
        await daemon.settle()
        now = await _now(daemon)
    finally:
        await daemon.stop()

    focused = now["focused"]
    assert [(c["kind"], c["source"]) for c in focused["context"]] == [
        ("ide", "ctx"),
        ("ide_project", "ctx"),
    ]
    title_item, project = focused["context"]
    assert title_item["title"] == "proj · main.py"
    assert title_item["data"] == {"project": "proj", "file": "main.py"}
    assert title_item["confidence"] == "high"
    assert project["data"] == {"path": "/work/proj"}
    assert project["link"] == "obsidian://open?vault=x"
    assert project["observed_at"]

    windows = by_key(by_key(now["workspaces"], "name")["3"]["windows"], "id")
    assert len(windows["7"]["context"]) == 2
    (chat,) = windows["8"]["context"]
    assert chat["unread"] == 2  # an explicit marker's count (A15)
    assert chat["title"] == "general"
    assert "context" not in {k for k, v in windows["8"].items() if v is None}


async def test_the_focused_windows_title_context_is_the_workspace_one_liner_fallback() -> None:
    daemon = _daemon()
    await daemon.start()
    try:
        await daemon.settle()
        now = await _now(daemon)
    finally:
        await daemon.stop()
    workspace = by_key(now["workspaces"], "name")["3"]
    assert workspace["sessions"] == []
    assert workspace["one_liner"] == "Chat Channel — proj · main.py"  # label, then the context


async def test_a_degraded_collector_s_context_is_absent_and_now_is_200() -> None:
    clock = FakeClock(T0)
    ctx = Synthetic("ctx", frozenset({Capability.CONTEXT}), emit=_items)
    wm = Synthetic("wm", frozenset({Capability.WINDOWS}), emit=_windows)
    daemon = make_daemon(wm, ctx, clock=clock)
    await daemon.start()
    try:
        await daemon.settle()
        assert (await _now(daemon))["focused"]["context"]
        ctx.doctor_state = HealthState.DEGRADED
        await clock.advance(1.0)  # the supervisor's first doctor probe
        await daemon.settle()
        now = await _now(daemon)
    finally:
        await daemon.stop()
    assert now["degraded"] == ["ctx"]
    assert "context" not in now["focused"]
    windows = by_key(by_key(now["workspaces"], "name")["3"]["windows"], "id")
    assert all("context" not in window for window in windows.values())
    assert by_key(now["workspaces"], "name")["3"]["one_liner"] == "Chat Channel"


async def test_a_gone_record_withdraws_an_item_and_a_closed_window_forgets_its_items() -> None:
    daemon = _daemon()
    await daemon.start()
    try:
        await daemon.settle()
        sink = daemon.sink("ctx")
        sink.emit(_from_ctx(parse.gone_record("7", "jetbrains", PYCHARM, T0)))
        await daemon.settle()
        now = await _now(daemon)
        assert [c["kind"] for c in now["focused"]["context"]] == ["ide"]

        daemon.sink("wm").emit(window_snapshot("wm", [CHAT], T0))
        await daemon.settle()
        assert daemon.contexts.by_window(["7"]) == {}
    finally:
        await daemon.stop()


async def test_titles_and_data_are_redacted_before_they_are_kept() -> None:
    secret = "sk-" + "a" * 24
    win = Win("9", "3", "IDE", 90, f"{secret} – main.py", bundle=PYCHARM)

    def emit(context: CollectorContext) -> None:
        context.sink.emit(window_snapshot("wm", [win], T0))
        context.sink.emit(focus_event("wm", "9", "3", T0))
        item = parse.Item(
            key="title", kind="ide", tier=1, title=f"{secret} · main.py", fields={"project": secret}
        )
        context.sink.emit(_from_ctx(parse.to_record(item, _facts(win), T0)))

    daemon = make_daemon(
        Synthetic("wm", frozenset({Capability.WINDOWS}), emit=emit),
        Synthetic("ctx", frozenset({Capability.CONTEXT})),
    )
    await daemon.start()
    try:
        await daemon.settle()
        now = await _now(daemon)
    finally:
        await daemon.stop()
    assert secret not in str(now)


async def test_an_ignored_application_never_has_context() -> None:
    win = Win("9", "3", "Vault", 90, "secret vault", bundle="com.1password.1password")

    def emit(context: CollectorContext) -> None:
        context.sink.emit(window_snapshot("wm", [win], T0))
        item = parse.Item(key="title", kind="ide", tier=1, title="hidden")
        context.sink.emit(_from_ctx(parse.to_record(item, _facts(win), T0)))

    daemon = make_daemon(
        Synthetic("wm", frozenset({Capability.WINDOWS}), emit=emit),
        Synthetic("ctx", frozenset({Capability.CONTEXT})),
    )
    await daemon.start()
    try:
        await daemon.settle()
        assert len(daemon.contexts) == 0
    finally:
        await daemon.stop()
