"""Terminal tables for the read commands.

Every function takes the decoded JSON of the API (dicts and lists, as ``fylgja.now/1`` writes it:
absent fields are missing keys) and returns text without colour, so renderings are stable and can
be compared to golden files. ``width`` is the terminal width; the last column of a table gives way
first, and a cell that does not fit ends in ``…``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = [
    "DEFAULT_WIDTH",
    "render_collectors",
    "render_now",
    "render_sessions",
    "render_workspace",
    "render_workspaces",
]

DEFAULT_WIDTH: Final = 100
_GAP: Final = "  "
_ELLIPSIS: Final = "…"
_MIN_LAST: Final = 12
_DASH: Final = "—"

Row = Mapping[str, Any]


def _clip(text: str, room: int) -> str:
    text = " ".join(text.split())  # one line: a title or one-liner may carry newlines or tabs
    if room <= 0:
        return ""
    if len(text) <= room:
        return text
    return text[: room - 1] + _ELLIPSIS if room > 1 else _ELLIPSIS


def table(headers: Sequence[str], rows: Sequence[Sequence[str]], width: int) -> str:
    """Aligned columns under a header; the last column takes the remaining width."""
    cells = [[" ".join(str(c).split()) for c in row] for row in rows]
    sizes = [len(h) for h in headers]
    for row in cells:
        for index, cell in enumerate(row):
            sizes[index] = max(sizes[index], len(cell))
    fixed = sum(sizes[:-1]) + len(_GAP) * (len(sizes) - 1)
    sizes[-1] = max(min(sizes[-1], width - fixed), _MIN_LAST if sizes[-1] > _MIN_LAST else 1)
    lines = [_GAP.join(h.ljust(sizes[i]) for i, h in enumerate(headers))]
    for row in cells:
        padded = [_clip(c, sizes[i]).ljust(sizes[i]) for i, c in enumerate(row)]
        lines.append(_GAP.join(padded))
    return "\n".join(line.rstrip() for line in lines)


def _name(path: str | None) -> str:
    if not path:
        return _DASH
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _count(value: Any) -> str:
    return str(len(value)) if isinstance(value, list) else "0"


def render_workspaces(workspaces: Sequence[Row], width: int = DEFAULT_WIDTH) -> str:
    """One row per AeroSpace workspace: ``*`` marks the focused one."""
    if not workspaces:
        return "no workspaces"
    rows = [
        [
            f"{w.get('name', '?')}{'*' if w.get('focused') else ''}",
            _count(w.get("windows")),
            _count(w.get("sessions")),
            str(w.get("one_liner") or w.get("label") or _DASH),
        ]
        for w in workspaces
    ]
    return table(["WS", "WIN", "SESS", "SUMMARY"], rows, width)


def _state(session: Row) -> str:
    state = str(session.get("state") or "unknown")
    reason = session.get("waiting_reason")
    return f"{state} ({reason})" if reason and state.startswith("waiting") else state


def _where(session: Row) -> str:
    host = session.get("host")
    if isinstance(host, Mapping):
        workspace = host.get("workspace")
        if workspace:
            return str(workspace)
    return _DASH


def render_sessions(sessions: Sequence[Row], width: int = DEFAULT_WIDTH) -> str:
    """One row per session: kind, state, AeroSpace workspace, project and the one-liner."""
    if not sessions:
        return "no sessions"
    rows = [
        [
            str(s.get("kind", "?")),
            _state(s),
            _where(s),
            _name(s.get("project_path") or s.get("cwd")),
            str(s.get("one_liner") or s.get("title") or s.get("name") or _DASH),
        ]
        for s in sessions
    ]
    return table(["KIND", "STATE", "WS", "PROJECT", "ONE-LINER"], rows, width)


def _health(value: Any) -> str:
    if isinstance(value, Mapping):
        state = str(value.get("state", "?"))
        detail = value.get("detail")
        return f"{state}: {detail}" if detail else state
    return str(value)


def _origin(row: Row) -> str:
    distribution = row.get("distribution")
    if not distribution:
        return str(row.get("origin", "?"))
    version = row.get("distribution_version")
    return f"{row.get('origin', '?')} ({distribution}{f' {version}' if version else ''})"


def render_collectors(rows: Sequence[Row], width: int = DEFAULT_WIDTH) -> str:
    """The ``collectors list`` rows. Fields of a plugin that was not loaded read ``not_loaded``."""
    if not rows:
        return "no collectors"
    body = []
    for row in rows:
        capabilities = row.get("capabilities")
        shown = ",".join(capabilities) if isinstance(capabilities, list) else str(capabilities)
        body.append(
            [
                str(row.get("name", "?")),
                _origin(row),
                str(row.get("state", "?")),
                str(row.get("api_version", "?")),
                shown or _DASH,
                _health(row.get("health")),
            ]
        )
    return table(["NAME", "ORIGIN", "STATE", "API", "CAPABILITIES", "HEALTH"], body, width)


def render_workspace(
    workspace: Row, sessions: Sequence[Row] = (), width: int = DEFAULT_WIDTH
) -> str:
    """One workspace: its summary line, its windows and the sessions placed there."""
    name = f"{workspace.get('name', '?')}{'*' if workspace.get('focused') else ''}"
    head = f"workspace {name}: {workspace.get('one_liner') or workspace.get('label') or _DASH}"
    parts = [_clip(head, width)]
    windows = [w for w in workspace.get("windows") or [] if isinstance(w, Mapping)]
    if windows:
        rows = [
            [
                f"{w.get('id', '?')}{'*' if w.get('focused') else ''}",
                str(w.get("app") or _DASH),
                str(w.get("title") or _DASH),
            ]
            for w in windows
        ]
        parts += ["", table(["WINDOW", "APP", "TITLE"], rows, width)]
    else:
        parts += ["", "no windows"]
    keys = set(workspace.get("sessions") or [])
    placed = [s for s in sessions if s.get("key") in keys]
    if placed:
        parts += ["", render_sessions(placed, width)]
    elif keys:
        parts += ["", "sessions: " + ", ".join(sorted(str(k) for k in keys))]
    return "\n".join(parts)


def render_now(now: Row, width: int = DEFAULT_WIDTH) -> str:
    """The workspace and session table of ``GET /now``, then what is degraded."""
    parts: list[str] = []
    focused = now.get("focused")
    if isinstance(focused, Mapping):
        where = focused.get("workspace")
        app = focused.get("app_name")
        title = focused.get("title")
        text = f"focused: {'workspace ' + str(where) if where else _DASH} · {app or _DASH}"
        parts.append(_clip(f"{text} · {title}" if title else text, width))
    workspaces = now.get("workspaces")
    parts += ["", "WORKSPACES"]
    parts.append(
        render_workspaces(workspaces, width)
        if isinstance(workspaces, list)
        else "unavailable: no collector reports windows"
    )
    sessions = now.get("sessions")
    parts += ["", "SESSIONS"]
    parts.append(
        render_sessions(sessions, width)
        if isinstance(sessions, list)
        else "unavailable: no collector reports sessions"
    )
    degraded = now.get("degraded")
    if isinstance(degraded, list) and degraded:
        parts += ["", "degraded: " + ", ".join(str(d) for d in degraded)]
    return "\n".join(parts).lstrip("\n")
