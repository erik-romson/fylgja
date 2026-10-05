"""Pure parsers for AeroSpace output (``PLAN-final-plan.md`` WP1.6).

Everything here is a function over captured bytes or text: no I/O, no clock. The adapter feeds
lines of ``aerospace subscribe --all`` and the output of the snapshot commands; the collector turns
the results into records.

The parsers are tolerant. A torn or malformed line becomes a :class:`MalformedLine` the caller
counts, unknown event names become :class:`UnknownEvent`, and unknown fields are ignored. Window
titles are free text: they are carried through untouched and never logged here (the ingest gate
redacts them).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from fylgja.models import (
    FocusEvent,
    FocusPayload,
    SnapshotItem,
    WindowPayload,
    WindowSnapshot,
    WorkspacePayload,
    WorkspaceSnapshot,
)

__all__ = [
    "FOCUS_ENTITY_ID",
    "SNAPSHOT_SCOPE",
    "SOURCE",
    "WINDOW_FORMAT",
    "WORKSPACE_FORMAT",
    "AeroEvent",
    "BindingTriggered",
    "FocusChanged",
    "FocusedMonitorChanged",
    "FocusedWorkspaceChanged",
    "MalformedLine",
    "ModeChanged",
    "ParsedWindows",
    "ParsedWorkspaces",
    "SnapshotParseError",
    "UnknownEvent",
    "WindowDetected",
    "WindowInfo",
    "WorkspaceInfo",
    "focus_event",
    "parse_event_line",
    "parse_version",
    "parse_windows",
    "parse_workspaces",
    "triggers_snapshot",
    "window_snapshot",
    "workspace_snapshot",
]

SOURCE: Final = "aerospace"
SNAPSHOT_SCOPE: Final = "all"
FOCUS_ENTITY_ID: Final = "focus"

WINDOW_FORMAT: Final = (
    "%{window-id} %{workspace} %{monitor-id} %{app-bundle-id} "
    "%{app-name} %{app-pid} %{window-title}"
)
"""With ``--json`` the format may hold only variables and spaces; each variable becomes a key."""

WORKSPACE_FORMAT: Final = "%{workspace} %{workspace-is-focused} %{monitor-id}"

_VERSION: Final = re.compile(r"client version:\s*(\S+)")


# --------------------------------------------------------------------------------------------
# Field helpers
# --------------------------------------------------------------------------------------------


def _ident(value: Any) -> str | None:
    """An id as text: an integer or a non-empty string; anything else is ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value:
        return value
    return None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --------------------------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FocusChanged:
    """``focus-changed``: ``windowId`` and ``workspace``."""

    window_id: str | None
    workspace: str | None


@dataclass(frozen=True, slots=True)
class FocusedWorkspaceChanged:
    """``focused-workspace-changed``: ``workspace`` and ``prevWorkspace``."""

    workspace: str | None
    previous: str | None


@dataclass(frozen=True, slots=True)
class FocusedMonitorChanged:
    """``focused-monitor-changed``: ``monitorId`` and ``workspace``."""

    monitor_id: str | None
    workspace: str | None


@dataclass(frozen=True, slots=True)
class ModeChanged:
    """``mode-changed``: ``mode``."""

    mode: str | None


@dataclass(frozen=True, slots=True)
class WindowDetected:
    """``window-detected``. AeroSpace lists it as a subscribable event; its fields are tolerated."""

    window_id: str | None
    workspace: str | None


@dataclass(frozen=True, slots=True)
class BindingTriggered:
    """``binding-triggered``. Only the mode is kept: the binding itself is a keystroke."""

    mode: str | None


@dataclass(frozen=True, slots=True)
class UnknownEvent:
    """An event name this version does not know. Only the name is kept."""

    name: str


@dataclass(frozen=True, slots=True)
class MalformedLine:
    """A line that is not an event. ``reason`` is a short code the caller can count.

    ``torn`` is a line with no newline that is not valid JSON (the stream ended mid-line);
    ``invalid_json`` is a complete line that is not JSON; the rest are ``bad_utf8``,
    ``not_object`` and ``no_event``.
    """

    reason: str


AeroEvent = (
    FocusChanged
    | FocusedWorkspaceChanged
    | FocusedMonitorChanged
    | ModeChanged
    | WindowDetected
    | BindingTriggered
    | UnknownEvent
)


def parse_event_line(line: bytes | str) -> AeroEvent | MalformedLine | None:
    """Parse one line of ``aerospace subscribe``. A blank line gives ``None``."""
    if isinstance(line, bytes):
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            return MalformedLine("bad_utf8")
    else:
        text = line
    body = text.strip()
    if not body:
        return None
    try:
        data: Any = json.loads(body)
    except ValueError:
        return MalformedLine("invalid_json" if text.endswith("\n") else "torn")
    if not isinstance(data, dict):
        return MalformedLine("not_object")
    name = data.get("_event")
    if not isinstance(name, str) or not name:
        return MalformedLine("no_event")
    match name:
        case "focus-changed":
            return FocusChanged(_ident(data.get("windowId")), _ident(data.get("workspace")))
        case "focused-workspace-changed":
            return FocusedWorkspaceChanged(
                _ident(data.get("workspace")), _ident(data.get("prevWorkspace"))
            )
        case "focused-monitor-changed":
            return FocusedMonitorChanged(
                _ident(data.get("monitorId")), _ident(data.get("workspace"))
            )
        case "mode-changed":
            return ModeChanged(_text(data.get("mode")))
        case "window-detected":
            return WindowDetected(_ident(data.get("windowId")), _ident(data.get("workspace")))
        case "binding-triggered":
            return BindingTriggered(_text(data.get("mode")))
        case _:
            return UnknownEvent(name)


def triggers_snapshot(event: AeroEvent) -> bool:
    """Whether the event may have changed the window or workspace lists."""
    return isinstance(
        event, FocusChanged | FocusedWorkspaceChanged | FocusedMonitorChanged | WindowDetected
    )


# --------------------------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------------------------


class SnapshotParseError(ValueError):
    """The output of a list command is not the JSON array it should be."""


@dataclass(frozen=True, slots=True)
class WindowInfo:
    window_id: str
    workspace: str | None
    app_bundle_id: str | None
    app_name: str | None
    app_pid: int | None
    title: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceInfo:
    name: str
    focused: bool
    monitor: str | None


@dataclass(frozen=True, slots=True)
class ParsedWindows:
    windows: tuple[WindowInfo, ...]
    skipped: int
    """Entries dropped: not an object, no usable window id, or a repeated window id."""


@dataclass(frozen=True, slots=True)
class ParsedWorkspaces:
    workspaces: tuple[WorkspaceInfo, ...]
    skipped: int


def _json_array(data: bytes | str, what: str) -> list[Any]:
    try:
        value: Any = json.loads(data)
    except ValueError as exc:
        raise SnapshotParseError(f"{what}: output is not JSON") from exc
    if not isinstance(value, list):
        raise SnapshotParseError(f"{what}: output is not a JSON array")
    return value


def _window_sort_key(window: WindowInfo) -> tuple[int, str]:
    return (len(window.window_id), window.window_id)


def parse_windows(data: bytes | str) -> ParsedWindows:
    """Parse ``aerospace list-windows --json --format <WINDOW_FORMAT>``.

    The result is sorted by window id so that the same state gives the same snapshot. An empty
    array is a valid answer (no windows).
    """
    windows: dict[str, WindowInfo] = {}
    skipped = 0
    for entry in _json_array(data, "list-windows"):
        window_id = _ident(entry.get("window-id")) if isinstance(entry, dict) else None
        if window_id is None or window_id in windows:
            skipped += 1
            continue
        windows[window_id] = WindowInfo(
            window_id=window_id,
            workspace=_ident(entry.get("workspace")),
            app_bundle_id=_text(entry.get("app-bundle-id")),
            app_name=_text(entry.get("app-name")),
            app_pid=_integer(entry.get("app-pid")),
            title=_text(entry.get("window-title")),
        )
    return ParsedWindows(tuple(sorted(windows.values(), key=_window_sort_key)), skipped)


def parse_workspaces(data: bytes | str) -> ParsedWorkspaces:
    """Parse ``aerospace list-workspaces --all --json --format <WORKSPACE_FORMAT>``.

    ``--all`` includes empty and persistent workspaces.
    """
    workspaces: dict[str, WorkspaceInfo] = {}
    skipped = 0
    for entry in _json_array(data, "list-workspaces"):
        name = _ident(entry.get("workspace")) if isinstance(entry, dict) else None
        if name is None or name in workspaces:
            skipped += 1
            continue
        workspaces[name] = WorkspaceInfo(
            name=name,
            focused=entry.get("workspace-is-focused") is True,
            monitor=_ident(entry.get("monitor-id")),
        )
    return ParsedWorkspaces(tuple(workspaces.values()), skipped)


def parse_version(text: bytes | str) -> str | None:
    """The client version from ``aerospace --version`` (for example ``0.21.3-Beta``)."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    match = _VERSION.search(text)
    return None if match is None else match.group(1)


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------


def window_snapshot(windows: Iterable[WindowInfo], observed_at: datetime) -> WindowSnapshot:
    """A **complete** snapshot: a window missing from it is closed."""
    return WindowSnapshot(
        source=SOURCE,
        entity_id=SNAPSHOT_SCOPE,
        observed_at=observed_at,
        complete=True,
        items=tuple(
            SnapshotItem(
                entity_id=w.window_id,
                payload=WindowPayload(
                    workspace=w.workspace,
                    app_bundle_id=w.app_bundle_id,
                    app_name=w.app_name,
                    app_pid=w.app_pid,
                    title=w.title,
                ),
            )
            for w in windows
        ),
    )


def workspace_snapshot(
    workspaces: Iterable[WorkspaceInfo], observed_at: datetime
) -> WorkspaceSnapshot:
    """A **complete** snapshot of every workspace, empty and persistent ones included."""
    return WorkspaceSnapshot(
        source=SOURCE,
        entity_id=SNAPSHOT_SCOPE,
        observed_at=observed_at,
        complete=True,
        items=tuple(
            SnapshotItem(
                entity_id=w.name,
                payload=WorkspacePayload(name=w.name, focused=w.focused, monitor=w.monitor),
            )
            for w in workspaces
        ),
    )


def focus_event(
    observed_at: datetime,
    *,
    window_id: str | None,
    workspace: str | None,
    app_bundle_id: str | None = None,
    title: str | None = None,
) -> FocusEvent:
    return FocusEvent(
        source=SOURCE,
        entity_id=FOCUS_ENTITY_ID,
        observed_at=observed_at,
        payload=FocusPayload(
            window_id=window_id, workspace=workspace, app_bundle_id=app_bundle_id, title=title
        ),
    )
