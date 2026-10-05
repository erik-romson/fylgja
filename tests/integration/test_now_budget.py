"""The ``/now`` performance budget (``PLAN-final-plan.md`` §4.2, WP1.9's done condition).

With 15 live sessions (each with a full facts block, an activity and a placement) and 60 windows
over 12 workspaces of fixture data, ``GET /now`` through the ASGI app (Host guard, routing, the
build and the serialization) has a p95 of at most 50 ms over 250 requests, after a warm-up.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import pytest
from api_support import (
    T0,
    Win,
    activity_snapshot,
    client,
    focus_event,
    make_daemon,
    session,
    session_snapshot,
    sessions_collector,
    window_snapshot,
    windows_collector,
    workspace_snapshot,
)

from fylgja.models import SessionActivity
from fylgja.testing.fakes import FakeFS

pytestmark = pytest.mark.slow

SESSIONS = 15
WINDOWS = 60
WORKSPACES = 12
PROJECTS = 8
WARMUP = 20
REQUESTS = 250
BUDGET_MS = 50.0


def _facts(index: int) -> dict[str, Any]:
    return {
        "title": f"Session {index} working on the exporter and its tests",
        "prompt": "p" * 300,
        "said": "s" * 800,
        "mode": "default",
        "branch": f"feature-{index}",
        "version": "2.1.289",
        "model": "claude-opus-5",
        "turns": 40 + index,
        "started": 1790000000,
        "last_at": 1790003600,
        "prompts": [f"prompt {n} " + "x" * 150 for n in range(30)],
        "tools": {f"Tool{n}": n for n in range(12)},
        "files": [f"/work/project-{index % PROJECTS}/src/file_{n}.py" for n in range(20)],
        "tasks": [{"status": "pending", "content": f"task {n}"} for n in range(20)],
    }


def _windows() -> list[Win]:
    windows: list[Win] = []
    for index in range(WINDOWS):
        workspace = str(index % WORKSPACES + 1)
        if index < SESSIONS:  # one terminal window per session
            windows.append(
                Win(str(1000 + index), workspace, "Terminal", 5000 + index, f"Shell {index}")
            )
        else:
            title = f"Document {index} — a page title of ordinary length for a browser tab"
            windows.append(Win(str(1000 + index), workspace, "Browser", 9000, title))
    return windows


async def test_now_p95_is_within_budget_with_15_sessions_and_60_windows() -> None:
    fs = FakeFS()
    for project in range(PROJECTS):
        fs.add_dir(f"/work/project-{project}/.git")
    daemon = make_daemon(windows_collector(), sessions_collector(), fs=fs)
    await daemon.start()
    try:
        wm, agents = daemon.sink("wm"), daemon.sink("agents")
        names = [str(n) for n in range(1, WORKSPACES + 1)]
        wm.emit(workspace_snapshot("wm", names, T0, focused="1"))
        wm.emit(window_snapshot("wm", _windows(), T0))
        wm.emit(focus_event("wm", "1000", "1", T0))
        agents.emit(
            session_snapshot(
                "agents",
                [
                    session(
                        "claude",
                        f"s-{index}",
                        pid=6000 + index,
                        cwd=f"/work/project-{index % PROJECTS}/src",
                        host_pid=5000 + index,
                        process_key=f"{6000 + index}-1790000000",
                        entrypoint="cli",
                        version="2.1.289",
                    )
                    for index in range(SESSIONS)
                ],
                T0,
            )
        )
        agents.emit(
            activity_snapshot(
                "agents",
                {
                    f"claude:s-{index}": SessionActivity(
                        state=("working", "idle", "waiting")[index % 3],
                        current_tool="Bash" if index % 3 != 1 else None,
                        current_target="pytest",
                        title=f"Session {index}",
                        last_prompt="run the tests",
                        facts=_facts(index),
                    )
                    for index in range(SESSIONS)
                },
                T0,
            )
        )
        await daemon.settle()

        async with client(daemon) as http:
            first = (await http.get("/now")).json()
            assert len(first["sessions"]) == SESSIONS
            assert sum(len(w["windows"]) for w in first["workspaces"]) == WINDOWS
            assert all("host" in s and "facts" in s for s in first["sessions"])
            for _ in range(WARMUP):
                await http.get("/now")
            timings: list[float] = []
            for _ in range(REQUESTS):
                started = time.perf_counter()
                response = await http.get("/now")
                timings.append((time.perf_counter() - started) * 1000)
                assert response.status_code == 200
    finally:
        await daemon.stop()

    p95 = statistics.quantiles(timings, n=100)[94]
    assert len(timings) >= 200
    assert p95 <= BUDGET_MS, f"/now p95 {p95:.1f} ms over {len(timings)} requests"
