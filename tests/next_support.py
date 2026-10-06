"""Shared set-up of the "what next" tests (WP6.2): a daemon whose picture has something to rank.

Workspace ``3`` is focused. One Claude session sits in it and waits for input, so rule 1 has an
item; a todo marked ``next`` in workspace ``3`` gives rule 3 one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from api_support import (
    T0,
    Win,
    activity_snapshot,
    make_daemon,
    session,
    session_snapshot,
    sessions_collector,
    window_snapshot,
    windows_collector,
    workspace_snapshot,
)

from fylgja.core.daemon import Daemon
from fylgja.models import SessionActivity
from fylgja.store import db

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"
FOCUSED = "3"
SESSION_KEY = "claude:abc-123"
WAIT_REASON = "agent question"
WAITING_REASON = f"waiting for input: {WAIT_REASON}"
TODO_TEXT = "write the release notes"
TODO_REASON = f"todo marked next in workspace {FOCUSED}: {TODO_TEXT}"


@asynccontextmanager
async def world(
    *, waiting: bool = True, todo: bool = True, user_config: dict[str, Any] | None = None
) -> AsyncIterator[Daemon]:
    """A started daemon with the picture described in the module docstring."""
    daemon = make_daemon(windows_collector(), sessions_collector(), user_config=user_config)
    await daemon.start()
    try:
        wm, agents = daemon.sink("wm"), daemon.sink("agents")
        wm.emit(workspace_snapshot("wm", ["1", "2", FOCUSED], T0, focused=FOCUSED))
        wm.emit(window_snapshot("wm", [Win("100", FOCUSED, "Term", 500)], T0))
        agents.emit(
            session_snapshot(
                "agents",
                [session("claude", "abc-123", pid=501, cwd="/work/alpha", host_pid=500)],
                T0,
            )
        )
        state = (
            SessionActivity(state="waiting_input", waiting_reason=WAIT_REASON)
            if waiting
            else SessionActivity(state="idle")
        )
        agents.emit(activity_snapshot("agents", {SESSION_KEY: state}, T0))
        await daemon.settle()
        if todo:
            await daemon.todos.add(
                TODO_TEXT, scope_kind="workspace", scope_ref=FOCUSED, status="next"
            )
        await daemon.settle()
        yield daemon
    finally:
        await daemon.stop()


def log_rows(daemon: Daemon, list_id: str | None = None) -> list[sqlite3.Row]:
    """``suggestions_log`` as stored (read-only connection), oldest list first, top item first."""
    connection = db.connect_reader(daemon.writer.database)
    try:
        where, args = ("WHERE list_id = ?", (list_id,)) if list_id else ("", ())
        return connection.execute(
            f"SELECT * FROM suggestions_log {where} ORDER BY generated_at, list_id, rank", args
        ).fetchall()
    finally:
        connection.close()
