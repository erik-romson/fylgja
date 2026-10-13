"""The ``/items`` performance budget (``docs/api/items.md``).

The store holds 20 000 closed focus spans over the last 14 days (ten working hours a day, over the
60 open windows and 15 windows closed since), and the live picture is that of
``test_now_budget.py``: 15 sessions with full facts blocks and 60 windows over 12 workspaces. The
daemon loads the spans into its span window at start. Through the ASGI app (Host guard, routing,
ranking and serialization), ``GET /items?rank=frecency`` and ``GET /now`` each have a p95 of at most
50 ms over 250 requests, after a warm-up. The ranking is memoized per 5 s step, so a burst of
requests ranks once.
"""

from __future__ import annotations

import statistics
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from api_support import (
    T0,
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
from test_now_budget import (
    PROJECTS,
    REQUESTS,
    SESSIONS,
    WARMUP,
    WINDOWS,
    WORKSPACES,
    Win,
    _facts,
)

from fylgja.models import SessionActivity
from fylgja.store import db
from fylgja.testing.fakes import FakeFS

pytestmark = pytest.mark.slow

SPANS = 20_000
DAYS = 14
CLOSED_WINDOWS = 15
WORK_HOURS = 10
BUDGET_MS = 50.0

INSERT = """
INSERT INTO focus_spans
    (window_id, workspace, app_bundle_id, title, project_path, session_key,
     started_at, ended_at, end_reason)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _windows() -> list[Win]:
    """The 60 windows of ``test_now_budget.py``, each browser window with its own app."""
    windows: list[Win] = []
    for index in range(WINDOWS):
        workspace = str(index % WORKSPACES + 1)
        if index < SESSIONS:  # one terminal window per session
            windows.append(
                Win(
                    str(1000 + index),
                    workspace,
                    "Terminal",
                    5000 + index,
                    f"Shell {index}",
                    bundle="com.example.terminal",
                )
            )
        else:
            title = f"Document {index} — a page title of ordinary length for a browser tab"
            bundle = f"com.example.app{index % 20}"
            windows.append(Win(str(1000 + index), workspace, "Browser", 9000, title, bundle))
    return windows


def _span_rows() -> list[tuple[Any, ...]]:
    """20 000 closed spans: ten working hours a day for 14 days, ending before T0."""
    per_day = -(-SPANS // DAYS)
    slot = WORK_HOURS * 3600 / per_day
    first = T0 - timedelta(days=DAYS) + timedelta(hours=1)
    rows: list[tuple[Any, ...]] = []
    for index in range(SPANS):
        day, number = divmod(index, per_day)
        start = first + timedelta(days=day, seconds=number * slot)
        end = start + timedelta(seconds=slot * 0.8)
        window = index % (WINDOWS + CLOSED_WINDOWS)
        if window < SESSIONS:
            place = (
                f"/work/project-{window % PROJECTS}",
                f"claude:s-{window}",
                "com.example.terminal",
            )
        elif window < WINDOWS:
            place = (None, None, f"com.example.app{window % 20}")
        else:  # a window closed since: its folder becomes a closed place
            place = (f"/work/old-{window}", None, "com.example.ide")
        project_path, session_key, bundle = place
        rows.append(
            (
                str(1000 + window),
                str(window % WORKSPACES + 1),
                bundle,
                f"Window {window}",
                project_path,
                session_key,
                db.to_db_time(start),
                db.to_db_time(end),
                "focus",
            )
        )
    return rows


def _write_spans(database: Path) -> None:
    connection = db.open_store(database)  # autocommit: one explicit transaction
    try:
        connection.execute("BEGIN")
        connection.executemany(INSERT, _span_rows())
        connection.execute("COMMIT")
    finally:
        connection.close()


async def _timings(http: httpx.AsyncClient, path: str) -> list[float]:
    for _ in range(WARMUP):
        await http.get(path)
    timings: list[float] = []
    for _ in range(REQUESTS):
        started = time.perf_counter()
        response = await http.get(path)
        timings.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200
    return timings


def _p95(timings: list[float]) -> float:
    return statistics.quantiles(timings, n=100)[94]


async def test_items_and_now_p95_are_within_budget_with_20000_spans(tmp_path: Path) -> None:
    database = tmp_path / "fylgja.db"
    _write_spans(database)
    fs = FakeFS()
    for project in range(PROJECTS):
        fs.add_dir(f"/work/project-{project}/.git")
    daemon = make_daemon(windows_collector(), sessions_collector(), fs=fs, database=database)
    await daemon.start()
    try:
        assert len(daemon.span_window) >= SPANS - 200  # the oldest morning may fall outside
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
            started = time.perf_counter()
            first = (await http.get("/items?rank=frecency")).json()
            cold = (time.perf_counter() - started) * 1000
            assert first["ranker"] == "frecency"
            assert "ranker_fallback" not in first
            assert first["total"] >= 40
            assert first["shown"] == first["total"]
            assert sum(len(group["items"]) for group in first["days"]) == first["total"]
            items = await _timings(http, "/items?rank=frecency")
            now = await _timings(http, "/now")
    finally:
        await daemon.stop()

    assert len(items) == len(now) == REQUESTS
    items_p95, now_p95 = _p95(items), _p95(now)
    print(f"/items cold {cold:.1f} ms, p95 {items_p95:.1f} ms; /now p95 {now_p95:.1f} ms")
    assert items_p95 <= BUDGET_MS, f"/items p95 {items_p95:.1f} ms over {len(items)} requests"
    assert now_p95 <= BUDGET_MS, f"/now p95 {now_p95:.1f} ms over {len(now)} requests"
