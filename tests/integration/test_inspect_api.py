"""``GET /inspect/collectors`` over a daemon on a fake clock (``docs/api/inspect.md``,
``docs/decisions/0048``).

Two synthetic collectors (``wm`` with its own settings model, ``agents``) push records through
their ingest-gate sinks; the route reports each one's state, redacted settings and accepted
records per kind. The AppleScript switches come from doctor's state file under
``FYLGJA_STATE_DIR``: missing, written by ``switches.write_record``, not JSON, or not a file. The
route runs no process.
"""

from __future__ import annotations

from typing import Any

from api_support import (
    HOME,
    T0,
    Synthetic,
    Win,
    by_key,
    client,
    make_daemon,
    nulls,
    session,
    session_snapshot,
    sessions_collector,
    window_snapshot,
    workspace_snapshot,
)
from pydantic import BaseModel, ConfigDict

from fylgja.api.inspect_models import INSPECT_COLLECTORS_SCHEMA, INSPECT_PIPELINE_SCHEMA
from fylgja.collectors.appcontext import switches
from fylgja.collectors.base import Capability
from fylgja.core.daemon import Daemon
from fylgja.privacy import REDACTED
from fylgja.testing.fakes import FakeProcessRunner

CANARY = "sk-" + "Z9" * 10
"""Matches the default ``[privacy] redact_patterns`` entry ``sk-[A-Za-z0-9_-]{16,}``."""


class WmSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_seconds: int = 30
    api_token: str | None = None
    note: str = "plain"
    socket: str | None = None


USER_CONFIG: dict[str, Any] = {
    "collectors": {
        "wm": {
            "api_token": "t-value-2",
            "note": f"uses {CANARY}",
            "socket": f"{HOME}/.wm/socket",
        },
        "gone": {"enabled": True},
    },
    "actions": {"applescript": {"chrome": True, "iterm2": False}},
}


def _daemon(runner: FakeProcessRunner | None = None) -> Daemon:
    wm = Synthetic("wm", frozenset({Capability.WINDOWS}), config_model=WmSettings)
    return make_daemon(
        wm,
        sessions_collector(),
        user_config=USER_CONFIG,
        runner=FakeProcessRunner() if runner is None else runner,
    )


async def _inspect(daemon: Daemon) -> dict[str, Any]:
    async with client(daemon) as http:
        response = await http.get("/inspect/collectors")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert isinstance(body, dict)
    assert list(nulls(body)) == []
    return body


async def test_the_route_reports_states_settings_and_counts_per_kind() -> None:
    runner = FakeProcessRunner()
    daemon = _daemon(runner)
    await daemon.start()
    try:
        daemon.sink("wm").emit(workspace_snapshot("wm", ["1"], T0, focused="1"))
        daemon.sink("wm").emit(window_snapshot("wm", [Win("10", "1", "App", 100)], T0))
        daemon.sink("agents").emit(
            session_snapshot("agents", [session("k", "1", pid=200, cwd="/work/alpha/main")], T0)
        )
        await daemon.settle()
        body = await _inspect(daemon)
    finally:
        await daemon.stop()

    assert body["schema"] == INSPECT_COLLECTORS_SCHEMA
    assert "generated_at" in body
    assert "started_at" in body
    collectors = by_key(body["collectors"], "name")
    assert set(collectors) == {"wm", "agents"}

    wm = collectors["wm"]
    assert (wm["origin"], wm["state"], wm["enabled"], wm["loaded"]) == (
        "built-in",
        "enabled",
        True,
        True,
    )
    assert wm["capabilities"] == ["windows"]
    assert wm["entity_kinds"] == ["focus", "window", "workspace"]
    assert wm["health"]["state"] in {"starting", "ok"}
    assert wm["settings"] == {
        "interval_seconds": 30,
        "api_token": REDACTED,
        "note": f"uses {REDACTED}",
        "socket": "~/.wm/socket",
    }
    records = wm["records"]
    assert (records["accepted"], records["dropped"], records["lost"]) == (2, 0, 0)
    assert records["rejected"] == {"schema": 0, "source": 0, "entity_kind": 0, "timestamp": 0}
    assert [(k["entity_kind"], k["record_type"], k["accepted"]) for k in records["kinds"]] == [
        ("window", "snapshot", 1),
        ("workspace", "snapshot", 1),
    ]
    assert all("last_at" in k for k in records["kinds"])
    assert "last_at" in records

    agents = collectors["agents"]["records"]
    assert [(k["entity_kind"], k["record_type"], k["accepted"]) for k in agents["kinds"]] == [
        ("session", "snapshot", 1),
    ]
    assert collectors["agents"]["settings"] == {}

    assert "gone" in body["not_installed"]
    assert {"wm", "agents"}.isdisjoint(body["not_installed"])
    assert body["applescript"] == [
        {"name": switches.CHROME, "configured": True},
        {"name": switches.ITERM2, "configured": False},
    ]

    text = str(body)
    for secret in (CANARY, "t-value-2", HOME):
        assert secret not in text
    assert runner.calls == []  # no doctor, no osascript: the route reads memory


async def test_before_start_there_is_no_start_time_and_nothing_counted() -> None:
    body = await _inspect(_daemon())
    assert "started_at" not in body
    for collector in body["collectors"]:
        assert collector["records"]["accepted"] == 0
        assert collector["records"]["kinds"] == []
        assert "last_at" not in collector["records"]


async def test_doctor_s_recorded_switch_check_is_shown() -> None:
    switches.write_record(switches.CHROME, ok=False, at=T0, reason="timeout")
    body = await _inspect(_daemon())
    chrome, iterm2 = body["applescript"]
    assert chrome == {
        "name": switches.CHROME,
        "configured": True,
        "doctor_ok": False,
        "doctor_at": "2026-10-05T12:00:00Z",
        "doctor_reason": "timeout",
    }
    assert iterm2 == {"name": switches.ITERM2, "configured": False}


async def test_an_unreadable_switch_file_shows_no_doctor_check() -> None:
    target = switches.state_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not json", encoding="utf-8")
    body = await _inspect(_daemon())
    assert [set(item) for item in body["applescript"]] == [{"name", "configured"}] * 2

    target.unlink()
    target.mkdir()  # a directory where the file should be: the read fails
    body = await _inspect(_daemon())
    assert [set(item) for item in body["applescript"]] == [{"name", "configured"}] * 2


async def test_a_switch_file_is_read_up_to_its_size_limit() -> None:
    switches.write_record(switches.CHROME, ok=True, at=T0)
    target = switches.state_file()
    text = target.read_text(encoding="utf-8")
    target.write_text(text + " " * (switches.MAX_STATE_BYTES * 2), encoding="utf-8")
    body = await _inspect(_daemon())
    assert body["applescript"][0]["doctor_ok"] is True

    target.write_text(" " * switches.MAX_STATE_BYTES + text, encoding="utf-8")
    body = await _inspect(_daemon())
    assert "doctor_ok" not in body["applescript"][0]  # the record lies past the limit


async def test_the_pipeline_route_reports_counters_and_no_privacy_values() -> None:
    daemon = _daemon()
    await daemon.start()
    try:
        daemon.sink("wm").emit(workspace_snapshot("wm", ["1"], T0, focused="1"))
        daemon.sink("wm").emit(window_snapshot("wm", [Win("10", "1", "App", 100)], T0))
        daemon.sink("agents").emit(
            session_snapshot("agents", [session("k", "1", pid=200, cwd="/work/alpha/main")], T0)
        )
        await daemon.settle()
        async with client(daemon) as http:
            response = await http.get("/inspect/pipeline")
    finally:
        await daemon.stop()

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert list(nulls(body)) == []
    assert body["schema"] == INSPECT_PIPELINE_SCHEMA
    assert body["gate"]["accepted"] >= 3
    assert body["writer"]["transactions"] >= 1
    assert body["live_view"]["rows"]["windows"] == 1
    assert body["summaries"]["enabled"] is False
    assert body["messaging_enabled"] is False
    assert "start:writer" in body["lifecycle"]
    assert body["collectors_running"] == 2
    assert body["events"]["max_clients"] == 16
    assert body["events"]["clients"] == 0
    assert set(body["privacy"]) == {"redact_patterns", "ignore_bundle_ids", "ignore_title_patterns"}
    assert all(isinstance(value, int) for value in body["privacy"].values())
    assert "sk-" not in str(body["privacy"])
