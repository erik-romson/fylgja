"""``POST /focus/window/{id}`` and ``POST /focus/session/{key}`` through ASGI (WP3.5)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from api_support import (
    BASE_URL,
    HOME,
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
from fylgja.core.focus import CMUX_SCRIPT, ITERM2_SCRIPT, OSASCRIPT
from fylgja.core.hosts import CmuxData, CmuxRecord, CmuxStore, HostData, JetBrainsProject
from fylgja.models import HookEvent, HookEventPayload
from fylgja.testing.fakes import FakeClock, FakeFS, FakeProcessRunner

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"
AERO = "/opt/homebrew/bin/aerospace"
SURFACE = "7A1B2C3D-0000-4000-8000-00000000AAAA"
ITERM_UUID = "ABCDEF01-2345-4678-89AB-CDEF01234567"
AUTH = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}


async def _hosts() -> HostData:
    store = CmuxStore((CmuxRecord("s-cmux", "cmux-ws-1", SURFACE, pid=501),))
    return HostData(
        home=HOME,
        cmux=CmuxData(store=store),
        jetbrains={"intellijidea": (JetBrainsProject(path="/work/b"),)},
    )


def _runner() -> FakeProcessRunner:
    runner = FakeProcessRunner()
    runner.script(AERO)
    runner.script(OSASCRIPT, stdout="ok")
    return runner


@pytest.fixture
def runner() -> FakeProcessRunner:
    return _runner()


@pytest.fixture
async def daemon(runner: FakeProcessRunner) -> AsyncIterator[Daemon]:
    fs = FakeFS()
    instance = make_daemon(
        windows_collector(),
        sessions_collector(),
        fs=fs,
        host_loader=_hosts,
        runner=runner,
        user_config={"actions": {"applescript": {"iterm2": True}}},
    )
    await instance.start()
    wm, agents = instance.sink("wm"), instance.sink("agents")
    wm.emit(workspace_snapshot("wm", ["1", "2", "3"], T0, focused="2"))
    wm.emit(
        window_snapshot(
            "wm",
            [
                Win("100", "2", "cmux", 500, "x", "com.cmuxterm.app"),
                Win("102", "1", "IDEA", 600, "b – main.py", "com.jetbrains.intellij"),
                Win("103", "3", "iTerm2", 700, "x", "com.googlecode.iterm2"),
            ],
            T0,
        )
    )
    agents.emit(
        session_snapshot(
            "agents",
            [
                session("claude", "s-cmux", pid=501, cwd="/work/a", host_pid=500),
                session("claude", "s-jb", pid=602, cwd="/work/b", host_pid=600),
                session("claude", "s-iterm", pid=701, cwd="/work/c", host_pid=700),
                session("codex", "t-none", cwd="/work/d"),
            ],
            T0,
        )
    )
    agents.emit(
        HookEvent(
            source="agents",
            entity_id="claude:s-iterm",
            observed_at=T0 + timedelta(seconds=1),
            payload=HookEventPayload(
                session_id="s-iterm",
                event="UserPromptSubmit",
                pid=701,
                ids={"ITERM_SESSION_ID": f"w0t0p0:{ITERM_UUID}"},
            ),
        )
    )
    await instance.settle()
    try:
        yield instance
    finally:
        await instance.stop()


def _http(daemon: Daemon) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(daemon, secret=SECRET)), base_url=BASE_URL
    )


async def _post(daemon: Daemon, path: str) -> httpx.Response:
    async with _http(daemon) as http:
        return await http.post(path, headers=AUTH, content=b"{}")


async def test_focus_window(daemon: Daemon, runner: FakeProcessRunner) -> None:
    response = await _post(daemon, "/focus/window/100")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"focused": "window", "via": "aerospace"}
    assert runner.calls == [(AERO, "focus", "--window-id", "100")]


async def test_unknown_window_is_404(daemon: Daemon, runner: FakeProcessRunner) -> None:
    for window in ("999", "abc", "100;ls"):
        assert (await _post(daemon, f"/focus/window/{window}")).status_code == 404
    assert runner.calls == []


async def test_focus_cmux_session_to_tab_level(daemon: Daemon, runner: FakeProcessRunner) -> None:
    response = await _post(daemon, "/focus/session/claude:s-cmux")
    assert response.status_code == 200
    assert response.json() == {"focused": "tab", "via": "cmux-applescript"}
    assert runner.calls == [
        (AERO, "workspace", "2"),
        (AERO, "focus", "--window-id", "100"),
        (OSASCRIPT, "-e", CMUX_SCRIPT, SURFACE),
    ]


async def test_cmux_failure_gives_window_level(daemon: Daemon, runner: FakeProcessRunner) -> None:
    runner.script(
        OSASCRIPT, timeout=True
    )  # shorter scripts do not shadow: same prefix, newest wins
    failing = FakeProcessRunner()
    failing.script(AERO)
    failing.script(OSASCRIPT, timeout=True)
    daemon.runner = failing
    response = await _post(daemon, "/focus/session/claude:s-cmux")
    assert response.status_code == 200
    assert response.json() == {"focused": "window", "via": "aerospace (cmux: timeout)"}


async def test_focus_jetbrains_session_is_window_level(
    daemon: Daemon, runner: FakeProcessRunner
) -> None:
    response = await _post(daemon, "/focus/session/claude:s-jb")
    assert response.status_code == 200
    assert response.json()["focused"] == "window"
    assert runner.calls == [(AERO, "workspace", "1"), (AERO, "focus", "--window-id", "102")]


async def test_focus_iterm2_session_uses_the_hook_session_id(
    daemon: Daemon, runner: FakeProcessRunner
) -> None:
    response = await _post(daemon, "/focus/session/claude:s-iterm")
    assert response.json() == {"focused": "tab", "via": "iterm2-applescript"}
    assert runner.calls[-1] == (OSASCRIPT, "-e", ITERM2_SCRIPT, ITERM_UUID)


async def test_unknown_session_is_404_and_a_windowless_one_is_409(
    daemon: Daemon, runner: FakeProcessRunner
) -> None:
    assert (await _post(daemon, "/focus/session/claude:nope")).status_code == 404
    windowless = await _post(daemon, "/focus/session/codex:t-none")
    assert windowless.status_code == 409
    assert set(windowless.json()) == {"detail"}
    assert runner.calls == []


async def test_aerospace_failure_is_502(daemon: Daemon) -> None:
    broken = FakeProcessRunner()
    broken.script(AERO, returncode=1)
    daemon.runner = broken
    window = await _post(daemon, "/focus/window/100")
    assert window.status_code == 502
    assert "aerospace focus" in window.json()["detail"]
    assert (await _post(daemon, "/focus/session/claude:s-cmux")).status_code == 502


async def test_focus_needs_the_bearer(daemon: Daemon, runner: FakeProcessRunner) -> None:
    async with _http(daemon) as http:
        response = await http.post(
            "/focus/window/100", headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 401
    assert runner.calls == []


async def test_focus_routes_are_post_only(daemon: Daemon) -> None:
    async with _http(daemon) as http:
        assert (await http.get("/focus/window/100")).status_code == 405


def _unused(_: Any, __: FakeClock) -> None:  # pragma: no cover
    raise AssertionError
