"""Terminal tables for the read commands.

Every function takes the decoded JSON of the API (dicts and lists, as ``fylgja.now/1`` writes it:
absent fields are missing keys) and returns text without colour, so renderings are stable and can
be compared to golden files. ``width`` is the terminal width; the last column of a table gives way
first, and a cell that does not fit ends in ``…``.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

__all__ = [
    "DEFAULT_WIDTH",
    "render_collectors",
    "render_history",
    "render_items",
    "render_next",
    "render_now",
    "render_rankers",
    "render_sessions",
    "render_story",
    "render_todos",
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


def _scope(todo: Row) -> str:
    kind, ref = todo.get("scope_kind"), todo.get("scope_ref")
    if not kind or not ref:
        return _DASH
    if kind == "workspace":
        return f"ws {ref}"
    if kind == "project":
        return f"project {_name(str(ref))}"
    return f"session {str(ref).partition(':')[2] or ref}"[:32]


def render_todos(todos: Sequence[Row], width: int = DEFAULT_WIDTH) -> str:
    """One row per todo: the first 8 characters of the id (enough for ``todo done``), the status,
    the scope and the text."""
    if not todos:
        return "no todos"
    rows = [
        [
            str(t.get("id", "?"))[:8],
            str(t.get("status", "?")),
            _scope(t),
            str(t.get("text", "")),
        ]
        for t in todos
    ]
    return table(["ID", "STATUS", "SCOPE", "TODO"], rows, width)


def _target(item: Row) -> str:
    """What an item is about: a todo by the start of its id (as ``todo done`` takes it), a path by
    its last segment, anything else (a session key, a workspace, a window) as it is."""
    ref = str(item.get("target_ref") or "")
    if not ref:
        return _DASH
    if item.get("kind") == "todo":
        return ref[:8]
    return _name(ref) if ref.startswith("/") else ref


def render_next(payload: Row, width: int = DEFAULT_WIDTH) -> str:
    """The ranked "what next" list: rank, what it is about, and why, in words."""
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return "nothing is waiting for you"
    rows = [
        [
            str(item.get("rank", "?")),
            str(item.get("kind", "?")),
            _target(item),
            str(item.get("reason", "")),
        ]
        for item in items
        if isinstance(item, dict)
    ]
    return table(["#", "WHAT", "TARGET", "WHY"], rows, width)


def _duration(seconds: object) -> str:
    """``1h05m``, ``12m`` or ``40s`` (a minute is rounded to the nearest)."""
    total = float(seconds) if isinstance(seconds, int | float) else 0.0
    if total < 60:
        return f"{round(total)}s"
    minutes = round(total / 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _local(value: object, form: str) -> str:
    try:
        return datetime.fromisoformat(str(value)).astimezone().strftime(form)
    except ValueError:
        return _DASH


def _totals(title: str, rows: Sequence[Row], key: str, total: float, width: int) -> str:
    table_rows = [
        [
            str(row.get(key) or _DASH),
            _duration(row.get("seconds")),
            f"{100 * float(row.get('seconds') or 0) / total:.0f}%" if total else _DASH,
        ]
        for row in rows
    ]
    return table([title, "TIME", "SHARE"], table_rows, width)


def render_history(history: Row, width: int = DEFAULT_WIDTH, *, spans: bool = True) -> str:
    """The ``/history`` answer: the window, the totals per workspace, project and app, what
    Claude's prompt history marked as worked on, and (with ``spans``) the ordered spans.

    Times are the machine's local time.
    """
    total = float(history.get("total_seconds") or 0)
    head = (
        f"{_local(history.get('since'), '%Y-%m-%d %H:%M')} to "
        f"{_local(history.get('until'), '%Y-%m-%d %H:%M')}: {_duration(total)} of focus"
    )
    if not history.get("spans") and total == 0:
        return head + "\nno focus spans in this window"
    parts = [head]
    workspaces = history.get("workspaces") or []
    projects = [
        {**p, "label": p.get("name") or p.get("project_path")}
        for p in history.get("projects") or []
    ]
    apps = history.get("apps") or []
    parts += ["", _totals("WORKSPACE", workspaces, "workspace", total, width)]
    parts += ["", _totals("PROJECT", projects, "label", total, width)]
    parts += ["", _totals("APP", apps, "app_bundle_id", total, width)]
    worked = history.get("worked_on") or []
    if worked:
        rows = [
            [
                str(w.get("name") or _DASH),
                str(w.get("markers", 0)),
                _local(w.get("last_at"), "%H:%M"),
            ]
            for w in worked
        ]
        parts += [
            "",
            "worked on (Claude prompts):",
            table(["PROJECT", "PROMPTS", "LAST"], rows, width),
        ]
    if spans and history.get("spans"):
        rows = [
            [
                f"{_local(s.get('start'), '%H:%M:%S')}-{_local(s.get('end'), '%H:%M:%S')}"
                + ("+" if s.get("open") else ""),
                _duration(s.get("seconds")),
                str(s.get("workspace") or _DASH),
                _name(s.get("project_path")),
                str(s.get("end_reason") or ("now" if s.get("open") else _DASH)),
                str(s.get("title") or s.get("app_bundle_id") or _DASH),
            ]
            for s in history["spans"]
        ]
        parts += ["", table(["WHEN", "TIME", "WS", "PROJECT", "END", "WINDOW"], rows, width)]
    if history.get("spans_truncated"):
        parts += ["", "the span list is cut short; the totals cover every span"]
    return "\n".join(parts)


_STORY_NOTES: Final = {
    "stale": "stale: the inputs changed since; it is rewritten at most once per TTL",
    "failed": "the latest attempt failed",
    "pending": "being written",
}


def render_story(summary: Row, width: int = DEFAULT_WIDTH) -> str:
    """A summary (``/summaries/{scope}/{ref}``): ARC and NOW, or the paragraph, wrapped to
    ``width``, then its state and the time it describes."""
    room = max(width, 20)
    lines: list[str] = []
    for label, key in (("ARC", "arc"), ("NOW", "now")):
        if summary.get(key):
            lines.append(textwrap.fill(f"{label}: {summary[key]}", room, subsequent_indent="     "))
    if summary.get("text"):
        lines.append(textwrap.fill(str(summary["text"]), room))
    state = str(summary.get("state", _DASH))
    note = _STORY_NOTES.get(state)
    if state == "failed" and summary.get("error_kind"):
        note = f"{note} ({summary['error_kind']})"
    stamp = summary.get("generated_at")
    when = f", as of {_local(stamp, '%Y-%m-%d %H:%M')}" if stamp else ""
    lines.append(f"[{state}{when}]" + (f" {note}" if note else ""))
    return "\n".join(lines)


def _day_label(day: object) -> str:
    return str(day) if day else _DASH


def render_items(payload: Row, width: int = DEFAULT_WIDTH) -> str:
    """The ``items`` answer: day groups in server order, an item as a line and its notes.

    Nothing is sorted, filtered or counted here; ``total`` and ``shown`` are the daemon's.
    """
    by_id = {
        str(item.get("id")): item
        for item in payload.get("items") or []
        if isinstance(item, Mapping)
    }
    ranker = str(payload.get("ranker", "?"))
    fallback = payload.get("ranker_fallback")
    head = f"ranker {ranker}" + (f" (failed; {fallback} answered)" if fallback else "")
    head += f" · {payload.get('shown', 0)} of {payload.get('total', 0)} items"
    if payload.get("query"):
        head += f" · search {payload['query']!r}"
    parts = [_clip(head, width)]
    for group in payload.get("days") or []:
        if not isinstance(group, Mapping):
            continue
        parts += ["", _day_label(group.get("day"))]
        for item_id in group.get("items") or []:
            item = by_id.get(str(item_id))
            if item is None:
                continue
            parts += _item_lines(item, width)
    if not by_id:
        parts += ["", "no items"]
    return "\n".join(parts)


def _item_lines(item: Row, width: int) -> list[str]:
    tickets = item.get("tickets")
    places = item.get("places")
    apps = list(
        dict.fromkeys(  # first-seen order, each label once
            str(place.get("label"))
            for place in places or []
            if isinstance(place, Mapping) and place.get("label")
        )
    )
    state = item.get("state")
    columns = [
        str(item.get("name", "?")),
        ",".join(tickets) if isinstance(tickets, list) and tickets else _DASH,
        _duration(item.get("seconds_today")),
        ",".join(apps) or _DASH,
        ",".join(state) if isinstance(state, list) and state else _DASH,
    ]
    lines = ["  " + _clip(" · ".join(columns), width - 2)]
    if item.get("summary"):
        lines.append("    " + _clip(str(item["summary"]), width - 4))
    if item.get("explanation"):
        lines.append("    · " + _clip(str(item["explanation"]), width - 6))
    return lines


def _ranker_origin(row: Row) -> str:
    distribution = row.get("distribution")
    if not distribution:
        return str(row.get("origin", "?"))
    version = row.get("version")
    return f"{row.get('origin', '?')} ({distribution}{f'@{version}' if version else ''})"


def render_rankers(payload: Row, width: int = DEFAULT_WIDTH) -> str:
    """The ``rankers`` answer: every discovered ranker and which one is the default in effect."""
    rows = [row for row in payload.get("rankers") or [] if isinstance(row, Mapping)]
    default = str(payload.get("default", "?"))
    configured = str(payload.get("configured_default", default))
    note = f"default in effect: {default}"
    if configured != default:
        note += f" (configured: {configured}, not loaded)"
    if not rows:
        return "no rankers\n" + note
    body = []
    for row in rows:
        name = str(row.get("name", "?"))
        if row.get("error"):
            loaded = f"error: {row['error']}"
        else:
            loaded = "loaded" if row.get("loaded") else "not loaded"
        body.append(
            [
                name + ("*" if name == default else ""),
                _ranker_origin(row),
                "enabled" if row.get("enabled") else "disabled",
                loaded,
                str(row.get("description") or _DASH),
            ]
        )
    return table(["NAME", "ORIGIN", "ENABLED", "LOADED", "DESCRIPTION"], body, width) + "\n" + note
