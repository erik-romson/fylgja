"""Builders shared by the resolver, host-adapter and label tests (``tests/`` has no packages)."""

from __future__ import annotations

from datetime import UTC, datetime

from fylgja.core.resolver import SessionInput
from fylgja.models import ProcessRef
from fylgja.store.rows import SessionRow, WindowRow

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CMUX = "com.cmuxterm.app"
PYCHARM = "com.jetbrains.pycharm"
INTELLIJ = "com.jetbrains.intellij"
ITERM = "com.googlecode.iterm2"
SAFARI = "com.apple.Safari"


def window(
    window_id: str,
    workspace: str | None,
    bundle: str,
    app_pid: int | None,
    title: str = "",
    app_name: str = "App",
) -> WindowRow:
    return WindowRow(
        window_id=window_id,
        source="aerospace",
        workspace=workspace,
        app_bundle_id=bundle,
        app_name=app_name,
        app_pid=app_pid,
        title=title,
        valid_from=T0,
    )


def chain(*links: tuple[int, int, str]) -> tuple[ProcessRef, ...]:
    """``(pid, ppid, comm)`` links, nearest first."""
    return tuple(ProcessRef(pid=pid, ppid=ppid, comm=comm) for pid, ppid, comm in links)


def session_input(
    key: str,
    *,
    pid: int | None = 100,
    cwd: str | None = "/work/project",
    name: str | None = None,
    ancestry: tuple[ProcessRef, ...] | None = None,
    host_pid: int = 10,
) -> SessionInput:
    """A session whose chain is ``claude`` under a shell under ``host_pid`` unless given."""
    if ancestry is None and pid is not None:
        ancestry = chain((pid, 50, "claude"), (50, host_pid, "zsh"), (host_pid, 1, "host"))
    return SessionInput(
        session_key=key,
        native_id=key.split(":", 1)[-1],
        pid=pid,
        cwd=cwd,
        name=name,
        ancestry=ancestry or (),
    )


def session_row(
    key: str,
    *,
    pid: int | None = 100,
    cwd: str | None = "/work/project",
    title: str | None = None,
    name: str | None = None,
    state: str | None = "idle",
) -> SessionRow:
    kind, _, native_id = key.partition(":")
    return SessionRow(
        session_key=key,
        kind=kind,
        native_id=native_id,
        source=kind,
        pid=pid,
        process_key=None,
        cwd=cwd,
        project_path=None,
        host_kind=None,
        host_ref=None,
        window_id=None,
        workspace=None,
        name=name,
        title=title,
        model=None,
        branch=None,
        state=state,
        state_since=None,
        waiting_reason=None,
        parent_key=None,
        predecessor_key=None,
        remote_url=None,
        first_seen=T0,
        ended_at=None,
        last_seen=T0,
    )
