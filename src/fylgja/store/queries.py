"""The explicit SQL of the store, and thin functions that run it. There is no ORM.

Write functions are called only by the writer task (:mod:`fylgja.core.writer`), inside its
transaction; read functions take any connection. Times are passed as stored text
(:func:`fylgja.store.db.to_db_time`).

**Change intervals (A20).** ``windows_snapshot``, ``session_snapshots`` and ``usage_snapshots``
hold one row per distinct state. :func:`put_window`, :func:`put_activity` and :func:`put_usage`
compare the observation with the entity's open interval: equal values change nothing, different
values close the open row at the observation time and open a new one, and an observation older
than the open row's ``valid_from`` is *stale* and ignored. ``close_missing_*`` closes the open
intervals of one collector whose entity a complete snapshot no longer lists.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from fylgja.store.rows import (
    ActivityRow,
    DaySessionRow,
    FocusRow,
    HealthRow,
    ImportedTodoRow,
    ProjectRow,
    SessionRow,
    SpanRow,
    StoreRow,
    SuggestionRow,
    SummaryRow,
    TodoRow,
    UsageRow,
    WindowRow,
    WorkedOnRow,
    WorkspaceRow,
)

__all__ = [
    "ACTIVITY_CARRIED",
    "ACTIVITY_OWNED",
    "Put",
    "PutStatus",
    "checkpoint_truncate",
    "close_open_span",
    "close_stale_spans",
    "close_imported_session_todos",
    "close_missing_activity",
    "close_missing_usage",
    "close_missing_windows",
    "close_open_activity",
    "close_workspaces_missing",
    "delete_expired",
    "end_missing_sessions",
    "ensure_session",
    "get_session",
    "get_todo",
    "get_workspace",
    "drop_open_span",
    "insert_event",
    "insert_open_span",
    "insert_suggestions",
    "insert_todo",
    "last_observed_at",
    "latest_focus",
    "list_import_todos",
    "list_todos",
    "load_active_todos",
    "load_day_sessions",
    "load_day_todos",
    "mark_todos_absent",
    "load_health",
    "load_live_sessions",
    "load_open_activity",
    "load_open_usage",
    "load_open_windows",
    "load_spans",
    "load_spans_before",
    "load_suggestions",
    "load_summaries",
    "load_worked_on",
    "load_open_workspaces",
    "load_projects",
    "optimize",
    "put_activity",
    "put_health",
    "put_summaries",
    "put_usage",
    "rate_suggestion",
    "put_window",
    "set_activity_readings",
    "set_manual_label",
    "soft_delete_todo",
    "unfocus_other_workspaces",
    "update_imported_todo",
    "update_session_from_activity",
    "update_todo",
    "upsert_build",
    "upsert_project",
    "upsert_session",
    "upsert_workspace",
]


class PutStatus(StrEnum):
    OPENED = "opened"
    """A new interval was opened (the first one, or the state changed)."""
    UNCHANGED = "unchanged"
    STALE = "stale"
    """The observation is older than the open interval and was ignored."""


@dataclass(frozen=True)
class Put[R]:
    status: PutStatus
    row: R | None
    """The open interval after the call; ``None`` when a stale observation found none open."""


# --------------------------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------------------------

INSERT_EVENT: Final = """
INSERT INTO events
    (dedup_key, source, record_type, entity_kind, entity_id, observed_at, received_at, payload)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (dedup_key) DO NOTHING
"""

LATEST_FOCUS: Final = """
SELECT source,
       json_extract(payload, '$.window_id') AS window_id,
       json_extract(payload, '$.workspace') AS workspace,
       json_extract(payload, '$.app_bundle_id') AS app_bundle_id,
       json_extract(payload, '$.title') AS title,
       observed_at
FROM events
WHERE entity_kind = 'focus' AND record_type = 'event'
ORDER BY observed_at DESC, id DESC
LIMIT 1
"""


def insert_event(
    connection: sqlite3.Connection,
    *,
    dedup_key: str,
    source: str,
    record_type: str,
    entity_kind: str,
    entity_id: str,
    observed_at: str,
    received_at: str,
    payload_json: str,
) -> bool:
    """Append one raw record. Returns ``False`` when the dedup key is already stored."""
    cursor = connection.execute(
        INSERT_EVENT,
        (
            dedup_key,
            source,
            record_type,
            entity_kind,
            entity_id,
            observed_at,
            received_at,
            payload_json,
        ),
    )
    return cursor.rowcount == 1


def latest_focus(connection: sqlite3.Connection) -> FocusRow | None:
    row = connection.execute(LATEST_FOCUS).fetchone()
    return None if row is None else FocusRow.from_db(row)


# --------------------------------------------------------------------------------------------
# Interval helper
# --------------------------------------------------------------------------------------------


def _put_interval(
    connection: sqlite3.Connection,
    *,
    select_open: str,
    select_last_end: str,
    key: Sequence[Any],
    close: str,
    insert: str,
    owned: Mapping[str, Any],
    params: Mapping[str, Any],
    at: str,
) -> tuple[PutStatus, sqlite3.Row | None]:
    current: sqlite3.Row | None = connection.execute(select_open, key).fetchone()
    if current is not None:
        if at < current["valid_from"]:
            return PutStatus.STALE, current
        if all(current[column] == value for column, value in owned.items()):
            return PutStatus.UNCHANGED, current
        connection.execute(close, (at, current["id"]))
    else:
        last_end = connection.execute(select_last_end, key).fetchone()[0]
        if last_end is not None and at < last_end:
            return PutStatus.STALE, None
    connection.execute(insert, {**params, **owned, "valid_from": at})
    opened: sqlite3.Row = connection.execute(select_open, key).fetchone()
    return PutStatus.OPENED, opened


def _row[R: StoreRow](kind: type[R], row: sqlite3.Row | None) -> R | None:
    return None if row is None else kind.from_db(row)


# --------------------------------------------------------------------------------------------
# windows_snapshot
# --------------------------------------------------------------------------------------------

_WINDOW_COLUMNS: Final = (
    "id, window_id, source, workspace, app_bundle_id, app_name, app_pid, title, valid_from"
)

SELECT_OPEN_WINDOW: Final = f"""
SELECT {_WINDOW_COLUMNS} FROM windows_snapshot
WHERE source = ? AND window_id = ? AND valid_to IS NULL
"""

SELECT_OPEN_WINDOWS: Final = f"""
SELECT {_WINDOW_COLUMNS} FROM windows_snapshot WHERE valid_to IS NULL ORDER BY window_id
"""

SELECT_OPEN_WINDOWS_OF_SOURCE: Final = """
SELECT id, window_id, valid_from FROM windows_snapshot WHERE source = ? AND valid_to IS NULL
"""

INSERT_WINDOW: Final = """
INSERT INTO windows_snapshot
    (source, window_id, workspace, app_bundle_id, app_name, app_pid, title, valid_from)
VALUES
    (:source, :window_id, :workspace, :app_bundle_id, :app_name, :app_pid, :title, :valid_from)
"""

CLOSE_WINDOW: Final = "UPDATE windows_snapshot SET valid_to = ? WHERE id = ?"

LAST_WINDOW_END: Final = """
SELECT max(valid_to) FROM windows_snapshot WHERE source = ? AND window_id = ?
"""


def put_window(
    connection: sqlite3.Connection,
    *,
    source: str,
    window_id: str,
    values: Mapping[str, Any],
    at: str,
) -> Put[WindowRow]:
    """Record the observed state of one window. ``values`` holds every value column."""
    owned = {
        column: values.get(column)
        for column in ("workspace", "app_bundle_id", "app_name", "app_pid", "title")
    }
    status, row = _put_interval(
        connection,
        select_open=SELECT_OPEN_WINDOW,
        select_last_end=LAST_WINDOW_END,
        key=(source, window_id),
        close=CLOSE_WINDOW,
        insert=INSERT_WINDOW,
        owned=owned,
        params={"source": source, "window_id": window_id},
        at=at,
    )
    return Put(status, _row(WindowRow, row))


def close_missing_windows(
    connection: sqlite3.Connection, *, source: str, present: Collection[str], at: str
) -> list[str]:
    """Close the open windows of ``source`` that are not in ``present``. Returns their ids."""
    closed: list[str] = []
    for row in connection.execute(SELECT_OPEN_WINDOWS_OF_SOURCE, (source,)).fetchall():
        if row["window_id"] in present or row["valid_from"] > at:
            continue
        connection.execute(CLOSE_WINDOW, (at, row["id"]))
        closed.append(row["window_id"])
    return closed


def load_open_windows(connection: sqlite3.Connection) -> list[WindowRow]:
    return [WindowRow.from_db(row) for row in connection.execute(SELECT_OPEN_WINDOWS)]


# --------------------------------------------------------------------------------------------
# workspaces
# --------------------------------------------------------------------------------------------

_WORKSPACE_COLUMNS: Final = (
    "name, source, monitor, focused, manual_label, manual_label_at, inferred_label, "
    "inferred_at, first_seen, closed_at, last_seen"
)

SELECT_WORKSPACE: Final = f"SELECT {_WORKSPACE_COLUMNS} FROM workspaces WHERE name = ?"

SELECT_OPEN_WORKSPACES: Final = f"""
SELECT {_WORKSPACE_COLUMNS} FROM workspaces
WHERE closed_at IS NULL AND last_seen IS NOT NULL
ORDER BY name
"""

UPSERT_WORKSPACE: Final = """
INSERT INTO workspaces (name, source, monitor, focused, first_seen, last_seen, closed_at)
VALUES (:name, :source, :monitor, :focused, :at, :at, NULL)
ON CONFLICT (name) DO UPDATE SET
    source = excluded.source,
    monitor = coalesce(excluded.monitor, workspaces.monitor),
    focused = excluded.focused,
    first_seen = coalesce(workspaces.first_seen, excluded.first_seen),
    last_seen = excluded.last_seen,
    closed_at = NULL
WHERE workspaces.last_seen IS NULL OR excluded.last_seen >= workspaces.last_seen
"""

SELECT_FOCUSED_WORKSPACES_OF_SOURCE: Final = """
SELECT name, last_seen FROM workspaces WHERE source = ? AND focused = 1 AND name <> ?
"""

UNFOCUS_WORKSPACE: Final = "UPDATE workspaces SET focused = 0 WHERE name = ?"

SELECT_OPEN_WORKSPACES_OF_SOURCE: Final = """
SELECT name, last_seen FROM workspaces WHERE source = ? AND closed_at IS NULL
"""

CLOSE_WORKSPACE: Final = "UPDATE workspaces SET closed_at = ?, focused = 0 WHERE name = ?"


def upsert_workspace(
    connection: sqlite3.Connection,
    *,
    name: str,
    source: str,
    monitor: str | None,
    focused: bool,
    at: str,
) -> WorkspaceRow:
    """Mark a workspace present. Labels are left alone; a stale observation changes nothing."""
    connection.execute(
        UPSERT_WORKSPACE,
        {"name": name, "source": source, "monitor": monitor, "focused": int(focused), "at": at},
    )
    row = get_workspace(connection, name)
    if row is None:  # pragma: no cover - the upsert above guarantees the row
        raise LookupError(name)
    return row


def get_workspace(connection: sqlite3.Connection, name: str) -> WorkspaceRow | None:
    row = connection.execute(SELECT_WORKSPACE, (name,)).fetchone()
    return None if row is None else WorkspaceRow.from_db(row)


UPDATE_INFERRED_LABEL: Final = """
UPDATE workspaces SET inferred_label = :label, inferred_at = :at
WHERE name = :name AND closed_at IS NULL AND inferred_label IS NOT :label
"""


def set_inferred_label(
    connection: sqlite3.Connection, *, name: str, label: str | None, at: str
) -> WorkspaceRow | None:
    """Store the inferred label of an open workspace. Returns the row when the label changed.

    ``inferred_at`` moves only with the label. The manual label is never touched (WP3.x).
    """
    cursor = connection.execute(UPDATE_INFERRED_LABEL, {"name": name, "label": label, "at": at})
    return get_workspace(connection, name) if cursor.rowcount else None


UPDATE_MANUAL_LABEL: Final = """
UPDATE workspaces SET manual_label = :label, manual_label_at = :at
WHERE name = :name AND closed_at IS NULL
"""


def set_manual_label(
    connection: sqlite3.Connection, *, name: str, label: str | None, at: str | None
) -> WorkspaceRow | None:
    """Set (or, with ``label=None``, clear) the manual label of an open workspace.

    Returns the updated row, or ``None`` when no open workspace has that name. Setting the same
    label again keeps the original ``manual_label_at``. The inferred label is never touched.
    """
    current = get_workspace(connection, name)
    if current is None or current.closed_at is not None:
        return None
    if label is not None and current.manual_label == label:
        return current
    connection.execute(
        UPDATE_MANUAL_LABEL, {"name": name, "label": label, "at": None if label is None else at}
    )
    return get_workspace(connection, name)


def unfocus_other_workspaces(
    connection: sqlite3.Connection, *, source: str, focused: str, at: str
) -> list[WorkspaceRow]:
    """Clear the focus flag of every other workspace of ``source`` not observed after ``at``."""
    changed: list[WorkspaceRow] = []
    rows = connection.execute(SELECT_FOCUSED_WORKSPACES_OF_SOURCE, (source, focused)).fetchall()
    for row in rows:
        if row["last_seen"] is not None and row["last_seen"] > at:
            continue
        connection.execute(UNFOCUS_WORKSPACE, (row["name"],))
        updated = get_workspace(connection, row["name"])
        if updated is not None:
            changed.append(updated)
    return changed


def close_workspaces_missing(
    connection: sqlite3.Connection, *, source: str, present: Collection[str], at: str
) -> list[str]:
    """Mark the workspaces of ``source`` that are not in ``present`` as gone."""
    closed: list[str] = []
    for row in connection.execute(SELECT_OPEN_WORKSPACES_OF_SOURCE, (source,)).fetchall():
        if row["name"] in present or (row["last_seen"] is not None and row["last_seen"] > at):
            continue
        connection.execute(CLOSE_WORKSPACE, (at, row["name"]))
        closed.append(row["name"])
    return closed


def load_open_workspaces(connection: sqlite3.Connection) -> list[WorkspaceRow]:
    return [WorkspaceRow.from_db(row) for row in connection.execute(SELECT_OPEN_WORKSPACES)]


# --------------------------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------------------------

_SESSION_COLUMNS: Final = (
    "session_key, kind, native_id, source, pid, process_key, cwd, project_path, host_kind, "
    "host_ref, window_id, workspace, name, title, model, branch, state, state_since, "
    "waiting_reason, parent_key, predecessor_key, remote_url, first_seen, ended_at, last_seen"
)

SELECT_SESSION: Final = f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE session_key = ?"

SELECT_LIVE_SESSIONS: Final = f"""
SELECT {_SESSION_COLUMNS} FROM sessions WHERE ended_at IS NULL ORDER BY session_key
"""

UPSERT_SESSION: Final = """
INSERT INTO sessions
    (session_key, kind, native_id, source, pid, process_key, cwd, name, model, branch, host_kind,
     host_ref, parent_key, predecessor_key, remote_url, first_seen, last_seen, ended_at)
VALUES
    (:session_key, :kind, :native_id, :source, :pid, :process_key, :cwd, :name, :model, :branch,
     :host_kind, :host_ref, :parent_key, :predecessor_key, :remote_url, :first_seen, :last_seen,
     :ended_at)
ON CONFLICT (session_key) DO UPDATE SET
    source = CASE WHEN :listed THEN excluded.source ELSE sessions.source END,
    pid = coalesce(excluded.pid, sessions.pid),
    process_key = coalesce(excluded.process_key, sessions.process_key),
    cwd = coalesce(excluded.cwd, sessions.cwd),
    name = coalesce(excluded.name, sessions.name),
    model = coalesce(excluded.model, sessions.model),
    branch = coalesce(excluded.branch, sessions.branch),
    host_kind = coalesce(excluded.host_kind, sessions.host_kind),
    host_ref = coalesce(excluded.host_ref, sessions.host_ref),
    parent_key = coalesce(excluded.parent_key, sessions.parent_key),
    predecessor_key = coalesce(excluded.predecessor_key, sessions.predecessor_key),
    remote_url = coalesce(excluded.remote_url, sessions.remote_url),
    first_seen = min(sessions.first_seen, excluded.first_seen),
    last_seen = excluded.last_seen,
    ended_at = CASE
        WHEN :listed AND (sessions.ended_at IS NULL OR excluded.last_seen >= sessions.ended_at)
            THEN excluded.ended_at
        ELSE coalesce(excluded.ended_at, sessions.ended_at)
    END
WHERE excluded.last_seen >= sessions.last_seen
"""

ENSURE_SESSION: Final = """
INSERT INTO sessions (session_key, kind, native_id, source, first_seen, last_seen)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (session_key) DO NOTHING
"""

UPDATE_SESSION_FROM_ACTIVITY: Final = """
UPDATE sessions SET
    state = coalesce(:state, state),
    state_since = CASE WHEN :state IS NOT NULL AND state IS NOT :state THEN :at
                       ELSE state_since END,
    waiting_reason = :waiting_reason,
    title = coalesce(:title, title),
    branch = coalesce(:branch, branch),
    last_seen = max(last_seen, :at)
WHERE session_key = :session_key
"""

SELECT_LIVE_SESSIONS_OF_SOURCE: Final = """
SELECT session_key, last_seen FROM sessions WHERE source = ? AND ended_at IS NULL
"""

END_SESSION: Final = "UPDATE sessions SET ended_at = ? WHERE session_key = ?"


def upsert_session(
    connection: sqlite3.Connection,
    *,
    session_key: str,
    kind: str,
    native_id: str,
    source: str,
    values: Mapping[str, Any],
    first_seen: str,
    at: str,
    listed: bool,
) -> SessionRow:
    """Merge an observation into a ``sessions`` row; ``None`` values keep what is stored.

    ``listed`` means a snapshot lists the session: it is alive unless ``ended_at`` says
    otherwise, so a previously ended session is reopened.
    """
    params: dict[str, Any] = {
        column: values.get(column)
        for column in (
            "pid",
            "process_key",
            "cwd",
            "name",
            "model",
            "branch",
            "host_kind",
            "host_ref",
            "parent_key",
            "predecessor_key",
            "remote_url",
            "ended_at",
        )
    }
    params.update(
        session_key=session_key,
        kind=kind,
        native_id=native_id,
        source=source,
        first_seen=first_seen,
        last_seen=at,
        listed=int(listed),
    )
    connection.execute(UPSERT_SESSION, params)
    row = get_session(connection, session_key)
    if row is None:  # pragma: no cover - the upsert above guarantees the row
        raise LookupError(session_key)
    return row


def ensure_session(
    connection: sqlite3.Connection,
    *,
    session_key: str,
    kind: str,
    native_id: str,
    source: str,
    at: str,
) -> None:
    """Create a minimal ``sessions`` row unless one exists."""
    connection.execute(ENSURE_SESSION, (session_key, kind, native_id, source, at, at))


def update_session_from_activity(
    connection: sqlite3.Connection,
    *,
    session_key: str,
    state: str | None,
    waiting_reason: str | None,
    title: str | None,
    branch: str | None,
    at: str,
) -> None:
    """Carry an activity's state into ``sessions``; ``state_since`` moves only on a change."""
    connection.execute(
        UPDATE_SESSION_FROM_ACTIVITY,
        {
            "session_key": session_key,
            "state": state,
            "waiting_reason": waiting_reason,
            "title": title,
            "branch": branch,
            "at": at,
        },
    )


UPDATE_SESSION_PLACEMENT: Final = """
UPDATE sessions SET
    project_path = :project_path,
    host_kind = :host_kind,
    host_ref = :host_ref,
    window_id = :window_id,
    workspace = :workspace
WHERE session_key = :session_key AND ended_at IS NULL
  AND (project_path IS NOT :project_path OR host_kind IS NOT :host_kind
       OR host_ref IS NOT :host_ref OR window_id IS NOT :window_id
       OR workspace IS NOT :workspace)
"""


def set_session_placement(
    connection: sqlite3.Connection,
    *,
    session_key: str,
    project_path: str | None,
    host_kind: str | None,
    host_ref: str | None,
    window_id: str | None,
    workspace: str | None,
) -> SessionRow | None:
    """Write the resolver's join of a live session (WP1.8).

    The resolver owns these five columns, so a ``None`` clears a join that no longer holds.
    Returns the changed row, or ``None`` when the session is gone or nothing changed.
    """
    cursor = connection.execute(
        UPDATE_SESSION_PLACEMENT,
        {
            "session_key": session_key,
            "project_path": project_path,
            "host_kind": host_kind,
            "host_ref": host_ref,
            "window_id": window_id,
            "workspace": workspace,
        },
    )
    return get_session(connection, session_key) if cursor.rowcount else None


def get_session(connection: sqlite3.Connection, session_key: str) -> SessionRow | None:
    row = connection.execute(SELECT_SESSION, (session_key,)).fetchone()
    return None if row is None else SessionRow.from_db(row)


def end_missing_sessions(
    connection: sqlite3.Connection, *, source: str, present: Collection[str], at: str
) -> list[str]:
    """End the live sessions of ``source`` that are not in ``present``. Returns their keys."""
    ended: list[str] = []
    for row in connection.execute(SELECT_LIVE_SESSIONS_OF_SOURCE, (source,)).fetchall():
        if row["session_key"] in present or row["last_seen"] > at:
            continue
        connection.execute(END_SESSION, (at, row["session_key"]))
        ended.append(row["session_key"])
    return ended


def load_live_sessions(connection: sqlite3.Connection) -> list[SessionRow]:
    return [SessionRow.from_db(row) for row in connection.execute(SELECT_LIVE_SESSIONS)]


# --------------------------------------------------------------------------------------------
# session_snapshots
# --------------------------------------------------------------------------------------------

ACTIVITY_OWNED: Final = (
    "state",
    "waiting_reason",
    "last_tool",
    "current_target",
    "open_task",
    "stage",
    "last_prompt",
    "last_reply",
    "one_liner",
)
"""Columns a ``SessionActivity`` record sets; each record replaces all of them.

``one_liner`` is derived by the writer from the record (§4.6); it is owned so that a changed title
alone opens a new interval."""

ACTIVITY_CARRIED: Final = (
    "context_pct",
    "cost_usd",
    "tokens_input",
    "tokens_output",
    "tokens_cache_read",
    "tokens_cache_write",
)
"""Columns other parts of the core fill; a new interval carries them over from the last one."""

_ACTIVITY_COLUMNS: Final = (
    "id, session_key, source, valid_from, state, waiting_reason, last_tool, current_target, "
    "open_task, stage, last_prompt, last_reply, one_liner, context_pct, cost_usd, tokens_input, "
    "tokens_output, tokens_cache_read, tokens_cache_write"
)

SELECT_OPEN_ACTIVITY: Final = f"""
SELECT {_ACTIVITY_COLUMNS} FROM session_snapshots WHERE session_key = ? AND valid_to IS NULL
"""

SELECT_LIVE_ACTIVITY: Final = f"""
SELECT {_ACTIVITY_COLUMNS} FROM session_snapshots
WHERE valid_to IS NULL
  AND session_key IN (SELECT session_key FROM sessions WHERE ended_at IS NULL)
ORDER BY session_key
"""

SELECT_OPEN_ACTIVITY_OF_SOURCE: Final = """
SELECT id, session_key, valid_from FROM session_snapshots WHERE source = ? AND valid_to IS NULL
"""

INSERT_ACTIVITY: Final = """
INSERT INTO session_snapshots
    (session_key, source, valid_from, state, waiting_reason, last_tool, current_target,
     open_task, stage, last_prompt, last_reply, one_liner, context_pct, cost_usd, tokens_input,
     tokens_output, tokens_cache_read, tokens_cache_write)
VALUES
    (:session_key, :source, :valid_from, :state, :waiting_reason, :last_tool, :current_target,
     :open_task, :stage, :last_prompt, :last_reply, :one_liner, :context_pct, :cost_usd,
     :tokens_input, :tokens_output, :tokens_cache_read, :tokens_cache_write)
"""

CLOSE_ACTIVITY: Final = "UPDATE session_snapshots SET valid_to = ? WHERE id = ?"

LAST_ACTIVITY_END: Final = "SELECT max(valid_to) FROM session_snapshots WHERE session_key = ?"


def put_activity(
    connection: sqlite3.Connection,
    *,
    session_key: str,
    source: str,
    values: Mapping[str, Any],
    at: str,
) -> Put[ActivityRow]:
    """Record a session's activity. ``values`` holds the :data:`ACTIVITY_OWNED` columns."""
    owned = {column: values.get(column) for column in ACTIVITY_OWNED}
    current = connection.execute(SELECT_OPEN_ACTIVITY, (session_key,)).fetchone()
    carried = {column: None if current is None else current[column] for column in ACTIVITY_CARRIED}
    status, row = _put_interval(
        connection,
        select_open=SELECT_OPEN_ACTIVITY,
        select_last_end=LAST_ACTIVITY_END,
        key=(session_key,),
        close=CLOSE_ACTIVITY,
        insert=INSERT_ACTIVITY,
        owned=owned,
        params={"session_key": session_key, "source": source, **carried},
        at=at,
    )
    return Put(status, _row(ActivityRow, row))


UPDATE_ACTIVITY_READINGS: Final = """
UPDATE session_snapshots SET
    context_pct = coalesce(:context_pct, context_pct),
    cost_usd = coalesce(:cost_usd, cost_usd),
    tokens_input = coalesce(:tokens_input, tokens_input),
    tokens_output = coalesce(:tokens_output, tokens_output),
    tokens_cache_read = coalesce(:tokens_cache_read, tokens_cache_read),
    tokens_cache_write = coalesce(:tokens_cache_write, tokens_cache_write)
WHERE session_key = :session_key AND valid_to IS NULL
  AND ((:context_pct IS NOT NULL AND context_pct IS NOT :context_pct)
       OR (:cost_usd IS NOT NULL AND cost_usd IS NOT :cost_usd)
       OR (:tokens_input IS NOT NULL AND tokens_input IS NOT :tokens_input)
       OR (:tokens_output IS NOT NULL AND tokens_output IS NOT :tokens_output)
       OR (:tokens_cache_read IS NOT NULL AND tokens_cache_read IS NOT :tokens_cache_read)
       OR (:tokens_cache_write IS NOT NULL AND tokens_cache_write IS NOT :tokens_cache_write))
"""


def set_activity_readings(
    connection: sqlite3.Connection,
    *,
    session_key: str,
    values: Mapping[str, Any],
) -> ActivityRow | None:
    """Update the :data:`ACTIVITY_CARRIED` readings of the open interval **in place** (§4.7).

    Context percentage, cost and token sums change with every message; they are not state, so
    they never open an interval (A20) but are carried from one interval to the next by
    :func:`put_activity`. ``values`` holds any of the carried columns; a missing or ``None`` one
    keeps the stored value. Returns the changed row, or ``None`` when nothing changed.
    """
    params = {column: values.get(column) for column in ACTIVITY_CARRIED}
    cursor = connection.execute(UPDATE_ACTIVITY_READINGS, {"session_key": session_key, **params})
    if not cursor.rowcount:
        return None
    return _row(ActivityRow, connection.execute(SELECT_OPEN_ACTIVITY, (session_key,)).fetchone())


def close_open_activity(connection: sqlite3.Connection, *, session_key: str, at: str) -> bool:
    """Close the open activity interval of one session (it ended)."""
    current = connection.execute(SELECT_OPEN_ACTIVITY, (session_key,)).fetchone()
    if current is None:
        return False
    connection.execute(CLOSE_ACTIVITY, (max(at, current["valid_from"]), current["id"]))
    return True


def close_missing_activity(
    connection: sqlite3.Connection, *, source: str, present: Collection[str], at: str
) -> list[str]:
    closed: list[str] = []
    for row in connection.execute(SELECT_OPEN_ACTIVITY_OF_SOURCE, (source,)).fetchall():
        if row["session_key"] in present or row["valid_from"] > at:
            continue
        connection.execute(CLOSE_ACTIVITY, (at, row["id"]))
        closed.append(row["session_key"])
    return closed


def load_open_activity(connection: sqlite3.Connection) -> list[ActivityRow]:
    """The open activity intervals of live sessions."""
    return [ActivityRow.from_db(row) for row in connection.execute(SELECT_LIVE_ACTIVITY)]


# --------------------------------------------------------------------------------------------
# projects
# --------------------------------------------------------------------------------------------

_PROJECT_COLUMNS: Final = (
    "path, repo_root, remote_url, repo_name, group_name, branch, dirty, ahead, behind, "
    "pr_number, pr_state, review_decision, build_state, build, build_observed_at, "
    "build_error_kind, last_commit_at, updated_at"
)

SELECT_PROJECT: Final = f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE path = ?"

SELECT_PROJECTS: Final = f"SELECT {_PROJECT_COLUMNS} FROM projects ORDER BY path"

UPSERT_PROJECT: Final = """
INSERT INTO projects
    (path, repo_root, remote_url, repo_name, group_name, branch, dirty, ahead, behind, pr_number,
     pr_state, review_decision, last_commit_at, updated_at)
VALUES
    (:path, :repo_root, :remote_url, :repo_name, :group_name, :branch, :dirty, :ahead, :behind,
     :pr_number, :pr_state, :review_decision, :last_commit_at, :updated_at)
ON CONFLICT (path) DO UPDATE SET
    repo_root = coalesce(excluded.repo_root, projects.repo_root),
    remote_url = coalesce(excluded.remote_url, projects.remote_url),
    repo_name = coalesce(excluded.repo_name, projects.repo_name),
    group_name = coalesce(excluded.group_name, projects.group_name),
    -- A git observation always says whether the tree is dirty, so one that carries `dirty` is
    -- complete: a missing branch (detached HEAD), upstream (no ahead/behind) or commit time
    -- (no commit yet) replaces what was stored. A PR observation likewise carries `pr_number`
    -- (0: the branch has no PR). Any other observation leaves these columns alone.
    branch = CASE WHEN excluded.dirty IS NOT NULL THEN excluded.branch
                  ELSE coalesce(excluded.branch, projects.branch) END,
    dirty = coalesce(excluded.dirty, projects.dirty),
    ahead = CASE WHEN excluded.dirty IS NOT NULL THEN excluded.ahead
                 ELSE coalesce(excluded.ahead, projects.ahead) END,
    behind = CASE WHEN excluded.dirty IS NOT NULL THEN excluded.behind
                  ELSE coalesce(excluded.behind, projects.behind) END,
    pr_number = coalesce(excluded.pr_number, projects.pr_number),
    pr_state = CASE WHEN excluded.pr_number IS NOT NULL THEN excluded.pr_state
                    ELSE coalesce(excluded.pr_state, projects.pr_state) END,
    review_decision = CASE WHEN excluded.pr_number IS NOT NULL THEN excluded.review_decision
                           ELSE coalesce(excluded.review_decision, projects.review_decision) END,
    last_commit_at = CASE WHEN excluded.dirty IS NOT NULL THEN excluded.last_commit_at
                          ELSE coalesce(excluded.last_commit_at, projects.last_commit_at) END,
    updated_at = excluded.updated_at
WHERE excluded.updated_at >= projects.updated_at
"""

UPSERT_BUILD: Final = """
INSERT INTO projects (path, build_state, build, build_observed_at, build_error_kind, updated_at)
VALUES (:path, :build_state, :build, :at, :build_error_kind, :at)
ON CONFLICT (path) DO UPDATE SET
    build_state = excluded.build_state,
    build = excluded.build,
    build_observed_at = excluded.build_observed_at,
    build_error_kind = excluded.build_error_kind
WHERE projects.build_observed_at IS NULL
   OR excluded.build_observed_at >= projects.build_observed_at
"""


def get_project(connection: sqlite3.Connection, path: str) -> ProjectRow | None:
    row = connection.execute(SELECT_PROJECT, (path,)).fetchone()
    return None if row is None else ProjectRow.from_db(row)


def _get_project(connection: sqlite3.Connection, path: str) -> ProjectRow:
    row = get_project(connection, path)
    if row is None:  # pragma: no cover - callers upsert first
        raise LookupError(path)
    return row


def upsert_project(
    connection: sqlite3.Connection, *, path: str, values: Mapping[str, Any], at: str
) -> ProjectRow:
    """Merge an observation into a ``projects`` row; ``None`` values keep what is stored."""
    params: dict[str, Any] = {
        column: values.get(column)
        for column in (
            "repo_root",
            "remote_url",
            "repo_name",
            "group_name",
            "branch",
            "ahead",
            "behind",
            "pr_number",
            "pr_state",
            "review_decision",
            "last_commit_at",
        )
    }
    dirty = values.get("dirty")
    params.update(path=path, dirty=None if dirty is None else int(dirty), updated_at=at)
    connection.execute(UPSERT_PROJECT, params)
    return _get_project(connection, path)


def upsert_build(
    connection: sqlite3.Connection,
    *,
    path: str,
    build_state: str,
    build_json: str,
    error_kind: str | None,
    at: str,
) -> ProjectRow:
    """Store the latest build fold of a project (§4.7)."""
    connection.execute(
        UPSERT_BUILD,
        {
            "path": path,
            "build_state": build_state,
            "build": build_json,
            "build_error_kind": error_kind,
            "at": at,
        },
    )
    return _get_project(connection, path)


def load_projects(connection: sqlite3.Connection) -> list[ProjectRow]:
    return [ProjectRow.from_db(row) for row in connection.execute(SELECT_PROJECTS)]


# --------------------------------------------------------------------------------------------
# usage_snapshots
# --------------------------------------------------------------------------------------------

_USAGE_COLUMNS: Final = (
    "id, collector, provider, window, used_pct, resets_at, cost_usd, cost_period, source, "
    "valid_from"
)

SELECT_OPEN_USAGE: Final = f"""
SELECT {_USAGE_COLUMNS} FROM usage_snapshots
WHERE collector = ? AND provider = ? AND window = ? AND valid_to IS NULL
"""

SELECT_ALL_OPEN_USAGE: Final = f"""
SELECT {_USAGE_COLUMNS} FROM usage_snapshots WHERE valid_to IS NULL
ORDER BY collector, provider, window
"""

SELECT_OPEN_USAGE_OF_COLLECTOR: Final = """
SELECT id, provider, window, valid_from FROM usage_snapshots
WHERE collector = ? AND valid_to IS NULL
"""

INSERT_USAGE: Final = """
INSERT INTO usage_snapshots
    (collector, provider, window, used_pct, resets_at, cost_usd, cost_period, source, valid_from)
VALUES
    (:collector, :provider, :window, :used_pct, :resets_at, :cost_usd, :cost_period, :source,
     :valid_from)
"""

CLOSE_USAGE: Final = "UPDATE usage_snapshots SET valid_to = ? WHERE id = ?"

LAST_USAGE_END: Final = """
SELECT max(valid_to) FROM usage_snapshots WHERE collector = ? AND provider = ? AND window = ?
"""


def put_usage(
    connection: sqlite3.Connection,
    *,
    collector: str,
    provider: str,
    window: str,
    values: Mapping[str, Any],
    at: str,
) -> Put[UsageRow]:
    owned = {
        column: values.get(column)
        for column in ("used_pct", "resets_at", "cost_usd", "cost_period", "source")
    }
    status, row = _put_interval(
        connection,
        select_open=SELECT_OPEN_USAGE,
        select_last_end=LAST_USAGE_END,
        key=(collector, provider, window),
        close=CLOSE_USAGE,
        insert=INSERT_USAGE,
        owned=owned,
        params={"collector": collector, "provider": provider, "window": window},
        at=at,
    )
    return Put(status, _row(UsageRow, row))


def close_missing_usage(
    connection: sqlite3.Connection,
    *,
    collector: str,
    present: Collection[tuple[str, str]],
    at: str,
    keep_providers: Collection[str] = (),
) -> list[str]:
    """Close the open usage rows of ``collector`` whose ``(provider, window)`` is not present.

    The rows of ``keep_providers`` stay open: the attempt for those providers failed, so what was
    last read is still the best answer (``/now`` marks it ``unavailable``).
    """
    closed: list[str] = []
    for row in connection.execute(SELECT_OPEN_USAGE_OF_COLLECTOR, (collector,)).fetchall():
        if (
            (row["provider"], row["window"]) in present
            or row["provider"] in keep_providers
            or row["valid_from"] > at
        ):
            continue
        connection.execute(CLOSE_USAGE, (at, row["id"]))
        closed.append(f"{collector}:{row['provider']}:{row['window']}")
    return closed


def load_open_usage(connection: sqlite3.Connection) -> list[UsageRow]:
    return [UsageRow.from_db(row) for row in connection.execute(SELECT_ALL_OPEN_USAGE)]


# --------------------------------------------------------------------------------------------
# collector_health
# --------------------------------------------------------------------------------------------

UPSERT_HEALTH: Final = """
INSERT INTO collector_health (name, state, since, last_ok_at, restarts, last_error_kind)
VALUES (:name, :state, :since, :last_ok_at, :restarts, :last_error_kind)
ON CONFLICT (name) DO UPDATE SET
    state = excluded.state,
    since = excluded.since,
    last_ok_at = excluded.last_ok_at,
    restarts = excluded.restarts,
    last_error_kind = excluded.last_error_kind
"""

SELECT_HEALTH: Final = """
SELECT name, state, since, last_ok_at, restarts, last_error_kind FROM collector_health
ORDER BY name
"""


def put_health(
    connection: sqlite3.Connection,
    *,
    name: str,
    state: str,
    since: str,
    last_ok_at: str | None,
    restarts: int,
    last_error_kind: str | None,
) -> None:
    connection.execute(
        UPSERT_HEALTH,
        {
            "name": name,
            "state": state,
            "since": since,
            "last_ok_at": last_ok_at,
            "restarts": restarts,
            "last_error_kind": last_error_kind,
        },
    )


def load_health(connection: sqlite3.Connection) -> list[HealthRow]:
    return [HealthRow.from_db(row) for row in connection.execute(SELECT_HEALTH)]


# --------------------------------------------------------------------------------------------
# todos
# --------------------------------------------------------------------------------------------

_TODO_COLUMNS: Final = (
    "id, scope_kind, scope_ref, text, status, created_at, done_at, source, source_ref, link, "
    "updated_at"
)

INSERT_TODO: Final = """
INSERT INTO todos (id, scope_kind, scope_ref, text, status, created_at, done_at, source,
                   source_ref, link, updated_at)
VALUES (:id, :scope_kind, :scope_ref, :text, :status, :created_at, :done_at, :source,
        :source_ref, :link, :updated_at)
"""

SELECT_TODO: Final = f"SELECT {_TODO_COLUMNS} FROM todos WHERE id = ? AND deleted_at IS NULL"

UPDATE_TODO: Final = """
UPDATE todos SET text = :text, status = :status, done_at = :done_at, updated_at = :updated_at
WHERE id = :id AND deleted_at IS NULL
"""

SOFT_DELETE_TODO: Final = """
UPDATE todos SET deleted_at = :at, deleted_reason = 'user', updated_at = :at
WHERE id = :id AND deleted_at IS NULL
"""

SELECT_ACTIVE_TODOS: Final = (
    f"SELECT {_TODO_COLUMNS} FROM todos WHERE deleted_at IS NULL AND status IN ('open', 'next') "
    "ORDER BY created_at, rowid"
)


def insert_todo(
    connection: sqlite3.Connection,
    *,
    todo_id: str,
    scope_kind: str | None,
    scope_ref: str | None,
    text: str,
    status: str,
    created_at: str,
    done_at: str | None,
    source: str,
    source_ref: str | None,
    link: str | None,
    updated_at: str | None = None,
) -> TodoRow:
    """Insert a todo; ``updated_at`` defaults to ``created_at``."""
    connection.execute(
        INSERT_TODO,
        {
            "id": todo_id,
            "scope_kind": scope_kind,
            "scope_ref": scope_ref,
            "text": text,
            "status": status,
            "created_at": created_at,
            "done_at": done_at,
            "source": source,
            "source_ref": source_ref,
            "link": link,
            "updated_at": created_at if updated_at is None else updated_at,
        },
    )
    row = get_todo(connection, todo_id)
    if row is None:  # pragma: no cover - the insert above just wrote it
        raise RuntimeError("todo vanished after insert")
    return row


def get_todo(connection: sqlite3.Connection, todo_id: str) -> TodoRow | None:
    """The todo, or ``None`` when it does not exist or was deleted."""
    found = connection.execute(SELECT_TODO, (todo_id,)).fetchone()
    return None if found is None else TodoRow.from_db(found)


def update_todo(
    connection: sqlite3.Connection,
    *,
    todo_id: str,
    text: str,
    status: str,
    done_at: str | None,
    at: str,
) -> TodoRow | None:
    connection.execute(
        UPDATE_TODO,
        {"id": todo_id, "text": text, "status": status, "done_at": done_at, "updated_at": at},
    )
    return get_todo(connection, todo_id)


def soft_delete_todo(connection: sqlite3.Connection, *, todo_id: str, at: str) -> bool:
    """Mark the todo deleted by the user; ``False`` when there is nothing to delete."""
    return connection.execute(SOFT_DELETE_TODO, {"id": todo_id, "at": at}).rowcount > 0


def load_active_todos(connection: sqlite3.Connection) -> list[TodoRow]:
    """The open and next todos that are not deleted."""
    return [TodoRow.from_db(row) for row in connection.execute(SELECT_ACTIVE_TODOS)]


def list_todos(
    connection: sqlite3.Connection,
    *,
    scope_kind: str | None = None,
    scope_ref: str | None = None,
    statuses: Collection[str] = (),
    order: str = "created",
    limit: int | None = None,
) -> list[TodoRow]:
    """Todos that are not deleted, optionally of one scope and some statuses.

    ``order`` is ``created`` (oldest first, the default) or ``done_desc`` (latest finished first);
    ``limit`` caps the answer (default: no cap).
    """
    clauses = ["deleted_at IS NULL"]
    params: list[str] = []
    if scope_kind is not None:
        clauses.append("scope_kind = ?")
        params.append(scope_kind)
    if scope_ref is not None:
        clauses.append("scope_ref = ?")
        params.append(scope_ref)
    if statuses:
        ordered = sorted(statuses)
        clauses.append(f"status IN ({', '.join('?' for _ in ordered)})")
        params.extend(ordered)
    where = " AND ".join(clauses)
    ordering = "done_at DESC, rowid DESC" if order == "done_desc" else "created_at, rowid"
    sql = f"SELECT {_TODO_COLUMNS} FROM todos WHERE {where} ORDER BY {ordering}"
    bound: list[str | int] = [*params]
    if limit is not None:
        sql += " LIMIT ?"
        bound.append(limit)
    return [TodoRow.from_db(row) for row in connection.execute(sql, bound)]


# Imported lists (WP4.3). The rows of one list share a source and a ``source_ref`` prefix
# (``<list ref>#``); the reconciler (fylgja.core.todo_import) checks the exact list itself.

SELECT_IMPORT_TODOS: Final = (
    f"SELECT {_TODO_COLUMNS}, deleted_at, deleted_reason FROM todos "
    "WHERE source = :source AND substr(source_ref, 1, length(:prefix)) = :prefix ORDER BY rowid"
)

UPDATE_IMPORTED_TODO: Final = """
UPDATE todos SET scope_kind = :scope_kind, scope_ref = :scope_ref, text = :text, status = :status,
    link = :link, done_at = :done_at, updated_at = :updated_at,
    deleted_at = NULL, deleted_reason = NULL
WHERE id = :id
"""

MARK_TODO_ABSENT: Final = """
UPDATE todos SET deleted_at = :at, deleted_reason = 'source_absent', updated_at = :at
WHERE id = :id AND deleted_at IS NULL
"""

SELECT_IMPORTED_SESSION_TODOS: Final = """
SELECT id FROM todos
WHERE scope_kind = 'session' AND scope_ref = :session_key AND source <> :native_source
  AND deleted_at IS NULL AND updated_at <= :at
ORDER BY rowid
"""


def list_import_todos(
    connection: sqlite3.Connection, *, source: str, prefix: str
) -> list[ImportedTodoRow]:
    """Every row of ``source`` whose ``source_ref`` starts with ``prefix``, deleted ones too."""
    found = connection.execute(SELECT_IMPORT_TODOS, {"source": source, "prefix": prefix})
    return [ImportedTodoRow.from_db(row) for row in found]


def update_imported_todo(
    connection: sqlite3.Connection,
    *,
    todo_id: str,
    scope_kind: str,
    scope_ref: str,
    text: str,
    status: str,
    link: str | None,
    done_at: str | None,
    at: str,
) -> TodoRow | None:
    """Set an imported row to what its source says; a deleted row is restored."""
    connection.execute(
        UPDATE_IMPORTED_TODO,
        {
            "id": todo_id,
            "scope_kind": scope_kind,
            "scope_ref": scope_ref,
            "text": text,
            "status": status,
            "link": link,
            "done_at": done_at,
            "updated_at": at,
        },
    )
    return get_todo(connection, todo_id)


def mark_todos_absent(connection: sqlite3.Connection, *, ids: Sequence[str], at: str) -> list[str]:
    """Soft-delete the rows a complete read no longer lists (``source_absent``).

    Returns the ids that were live until now.
    """
    return [
        todo_id
        for todo_id in ids
        if connection.execute(MARK_TODO_ABSENT, {"id": todo_id, "at": at}).rowcount > 0
    ]


def close_imported_session_todos(
    connection: sqlite3.Connection, *, session_key: str, native_source: str, at: str
) -> list[str]:
    """Mark the imported todos of a session that ended at ``at`` ``source_absent``.

    A row a read restored after ``at`` stays. Returns the ids that were closed.
    """
    ids = [
        str(row[0])
        for row in connection.execute(
            SELECT_IMPORTED_SESSION_TODOS,
            {"session_key": session_key, "native_source": native_source, "at": at},
        )
    ]
    return mark_todos_absent(connection, ids=ids, at=at)


# --------------------------------------------------------------------------------------------
# Focus spans (WP5.2)
# --------------------------------------------------------------------------------------------

INSERT_OPEN_SPAN: Final = """
INSERT INTO focus_spans
    (window_id, workspace, app_bundle_id, title, project_path, session_key, started_at)
VALUES (:window_id, :workspace, :app_bundle_id, :title, :project_path, :session_key, :started_at)
"""

CLOSE_OPEN_SPAN: Final = """
UPDATE focus_spans
SET ended_at = :ended_at,
    end_reason = :end_reason,
    project_path = COALESCE(:project_path, project_path),
    session_key = COALESCE(:session_key, session_key)
WHERE ended_at IS NULL AND window_id IS :window_id AND started_at = :started_at
"""

DROP_OPEN_SPAN: Final = """
DELETE FROM focus_spans
WHERE ended_at IS NULL AND window_id IS :window_id AND started_at = :started_at
"""

SELECT_OPEN_SPANS: Final = "SELECT id, started_at FROM focus_spans WHERE ended_at IS NULL"

CLOSE_SPAN_BY_ID: Final = "UPDATE focus_spans SET ended_at = ?, end_reason = ? WHERE id = ?"

DELETE_SPAN_BY_ID: Final = "DELETE FROM focus_spans WHERE id = ?"

LAST_OBSERVED_AT: Final = "SELECT MAX(observed_at) FROM events"

LOAD_SPANS: Final = """
SELECT window_id, workspace, app_bundle_id, title, project_path, session_key,
       started_at, ended_at, end_reason
FROM focus_spans
WHERE started_at < :until AND (ended_at IS NULL OR ended_at > :since)
ORDER BY started_at, id
"""

LOAD_SPANS_BEFORE: Final = """
SELECT window_id, workspace, app_bundle_id, title, project_path, session_key,
       started_at, ended_at, end_reason
FROM focus_spans
WHERE ended_at IS NOT NULL AND ended_at <= :before
ORDER BY ended_at DESC, started_at DESC, id DESC
LIMIT :limit
"""

LOAD_WORKED_ON: Final = """
SELECT json_extract(payload, '$.data.project') AS project_path,
       COUNT(*) AS markers,
       MAX(observed_at) AS last_at
FROM events
WHERE entity_kind = 'context_item' AND record_type = 'event'
  AND json_extract(payload, '$.context_kind') = 'worked_on'
  AND json_extract(payload, '$.data.project') IS NOT NULL
  AND observed_at >= :since AND observed_at < :until
GROUP BY project_path
ORDER BY last_at DESC, project_path
"""


def insert_open_span(
    connection: sqlite3.Connection,
    *,
    window_id: str | None,
    workspace: str | None,
    app_bundle_id: str | None,
    title: str | None,
    project_path: str | None,
    session_key: str | None,
    started_at: str,
) -> None:
    """Open a span (``ended_at`` stays ``NULL`` until it is closed)."""
    connection.execute(
        INSERT_OPEN_SPAN,
        {
            "window_id": window_id,
            "workspace": workspace,
            "app_bundle_id": app_bundle_id,
            "title": title,
            "project_path": project_path,
            "session_key": session_key,
            "started_at": started_at,
        },
    )


def close_open_span(
    connection: sqlite3.Connection,
    *,
    window_id: str | None,
    started_at: str,
    ended_at: str,
    end_reason: str,
    project_path: str | None,
    session_key: str | None,
) -> bool:
    """Close the open span of ``window_id`` that started at ``started_at``; a ``None``
    attribution keeps what the span has. Returns whether a row was closed."""
    cursor = connection.execute(
        CLOSE_OPEN_SPAN,
        {
            "window_id": window_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "end_reason": end_reason,
            "project_path": project_path,
            "session_key": session_key,
        },
    )
    return cursor.rowcount == 1


def drop_open_span(
    connection: sqlite3.Connection, *, window_id: str | None, started_at: str
) -> bool:
    """Delete an open span that turned out to have no length."""
    cursor = connection.execute(DROP_OPEN_SPAN, {"window_id": window_id, "started_at": started_at})
    return cursor.rowcount == 1


def last_observed_at(connection: sqlite3.Connection) -> str | None:
    """The newest ``observed_at`` of any stored record, or ``None`` for an empty store."""
    value = connection.execute(LAST_OBSERVED_AT).fetchone()[0]
    return None if value is None else str(value)


def close_stale_spans(
    connection: sqlite3.Connection, *, last_observed: str | None, now: str
) -> int:
    """Close every span a stopped daemon left open (it crashed or was killed).

    Each is closed at the last time the store observed anything, never earlier than the span's own
    start and never later than ``now``, with ``end_reason = 'shutdown'``; a span that then has no
    length is deleted. Returns how many spans were closed.
    """
    stale = [(int(row[0]), str(row[1])) for row in connection.execute(SELECT_OPEN_SPANS)]
    for span_id, started in stale:
        end = min(last_observed or started, now)
        end = max(end, started)
        if end <= started:
            connection.execute(DELETE_SPAN_BY_ID, (span_id,))
        else:
            connection.execute(CLOSE_SPAN_BY_ID, (end, "shutdown", span_id))
    return len(stale)


def load_spans(connection: sqlite3.Connection, *, since: str, until: str) -> list[SpanRow]:
    """The spans that overlap ``[since, until)``, oldest first, unclipped. An open span has
    ``ended_at = None``."""
    return [
        SpanRow.from_db(row)
        for row in connection.execute(LOAD_SPANS, {"since": since, "until": until})
    ]


def load_spans_before(connection: sqlite3.Connection, *, before: str, limit: int) -> list[SpanRow]:
    """At most ``limit`` closed spans that ended at or before ``before``, newest first: what the
    span window reads when its 14 days hold no complete work session (docs/decisions/0050).
    Together with :func:`load_spans` from ``since = before`` it covers every span once."""
    return [
        SpanRow.from_db(row)
        for row in connection.execute(LOAD_SPANS_BEFORE, {"before": before, "limit": limit})
    ]


def load_worked_on(connection: sqlite3.Connection, *, since: str, until: str) -> list[WorkedOnRow]:
    """The projects Claude's prompt history marked as worked on in ``[since, until)``, most
    recent first."""
    return [
        WorkedOnRow.from_db(row)
        for row in connection.execute(LOAD_WORKED_ON, {"since": since, "until": until})
    ]


LOAD_DAY_SESSIONS: Final = """
SELECT s.session_key, s.kind, s.project_path, s.workspace, s.title, s.first_seen,
       (SELECT n.one_liner FROM session_snapshots n
         WHERE n.session_key = s.session_key AND n.valid_from < :until AND n.one_liner IS NOT NULL
         ORDER BY n.valid_from DESC LIMIT 1) AS one_liner
FROM sessions s
WHERE (s.first_seen >= :since AND s.first_seen < :until)
   OR EXISTS (SELECT 1 FROM session_snapshots a
               WHERE a.session_key = s.session_key
                 AND a.valid_from >= :since AND a.valid_from < :until)
ORDER BY s.first_seen, s.session_key
"""

LOAD_DAY_TODOS: Final = (
    f"SELECT {_TODO_COLUMNS} FROM todos WHERE deleted_at IS NULL "
    "AND ((created_at >= :since AND created_at < :until) "
    "OR (done_at IS NOT NULL AND done_at >= :since AND done_at < :until)) "
    "ORDER BY created_at, rowid"
)


def load_day_sessions(
    connection: sqlite3.Connection, *, since: str, until: str
) -> list[DaySessionRow]:
    """The sessions first seen in ``[since, until)`` or with a change of activity in it, oldest
    first, each with its latest one-liner before ``until``."""
    found = connection.execute(LOAD_DAY_SESSIONS, {"since": since, "until": until})
    return [DaySessionRow.from_db(row) for row in found]


def load_day_todos(connection: sqlite3.Connection, *, since: str, until: str) -> list[TodoRow]:
    """The todos (not deleted) created or finished in ``[since, until)``, oldest first."""
    return [
        TodoRow.from_db(row)
        for row in connection.execute(LOAD_DAY_TODOS, {"since": since, "until": until})
    ]


# --------------------------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------------------------

DELETE_EVENTS_BEFORE: Final = "DELETE FROM events WHERE observed_at < ?"

DELETE_SPANS_BEFORE: Final = "DELETE FROM focus_spans WHERE ended_at IS NOT NULL AND ended_at < ?"

DELETE_WINDOW_INTERVALS_BEFORE: Final = (
    "DELETE FROM windows_snapshot WHERE valid_to IS NOT NULL AND valid_to < ?"
)

DELETE_SESSION_INTERVALS_BEFORE: Final = (
    "DELETE FROM session_snapshots WHERE valid_to IS NOT NULL AND valid_to < ?"
)

DELETE_USAGE_INTERVALS_BEFORE: Final = (
    "DELETE FROM usage_snapshots WHERE valid_to IS NOT NULL AND valid_to < ?"
)


def delete_expired(
    connection: sqlite3.Connection, *, events_before: str, archives_before: str
) -> dict[str, int]:
    """Delete raw events observed before ``events_before``, and spans and closed intervals that
    ended before ``archives_before``. Open spans and intervals are never deleted. Returns the
    deleted row count per table."""
    plan = (
        ("events", DELETE_EVENTS_BEFORE, events_before),
        ("focus_spans", DELETE_SPANS_BEFORE, archives_before),
        ("windows_snapshot", DELETE_WINDOW_INTERVALS_BEFORE, archives_before),
        ("session_snapshots", DELETE_SESSION_INTERVALS_BEFORE, archives_before),
        ("usage_snapshots", DELETE_USAGE_INTERVALS_BEFORE, archives_before),
    )
    return {table: connection.execute(sql, (cutoff,)).rowcount for table, sql, cutoff in plan}


OPTIMIZE: Final = "PRAGMA optimize"

CHECKPOINT_TRUNCATE: Final = "PRAGMA wal_checkpoint(TRUNCATE)"


def optimize(connection: sqlite3.Connection) -> None:
    connection.execute(OPTIMIZE)


def checkpoint_truncate(connection: sqlite3.Connection) -> bool:
    """Checkpoint the WAL and truncate it. ``False`` when a reader kept it from completing.

    Must run outside a transaction.
    """
    busy = connection.execute(CHECKPOINT_TRUNCATE).fetchone()[0]
    return int(busy) == 0


# --------------------------------------------------------------------------------------------
# suggestions_log
# --------------------------------------------------------------------------------------------

_SUGGESTION_COLUMNS: Final = (
    "id, list_id, generated_at, rank, kind, target_ref, score, reason, feedback, feedback_at, "
    "acted_at"
)

INSERT_SUGGESTION: Final = """
INSERT OR IGNORE INTO suggestions_log (id, list_id, generated_at, rank, kind, target_ref, score,
                                       reason)
VALUES (:id, :list_id, :generated_at, :rank, :kind, :target_ref, :score, :reason)
"""

SELECT_SUGGESTIONS: Final = (
    f"SELECT {_SUGGESTION_COLUMNS} FROM suggestions_log WHERE list_id = ? ORDER BY rank, id"
)

RATE_SUGGESTION: Final = """
UPDATE suggestions_log SET feedback = :rating, feedback_at = :at WHERE id = :id
"""


def insert_suggestions(connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> None:
    """Log the items of one generated list. A suggestion id already logged is left as it is."""
    connection.executemany(INSERT_SUGGESTION, rows)


def load_suggestions(connection: sqlite3.Connection, list_id: str) -> list[SuggestionRow]:
    """The logged items of ``list_id``, top item first (empty when the list is unknown)."""
    found = connection.execute(SELECT_SUGGESTIONS, (list_id,)).fetchall()
    return [SuggestionRow.from_db(row) for row in found]


def rate_suggestion(
    connection: sqlite3.Connection, *, suggestion_id: str, rating: str, at: str
) -> None:
    """Record ``rating`` (``good`` or ``bad``) on one logged item; a second rating replaces it."""
    connection.execute(RATE_SUGGESTION, {"id": suggestion_id, "rating": rating, "at": at})


# --------------------------------------------------------------------------------------------
# summaries
# --------------------------------------------------------------------------------------------

_SUMMARY_COLUMNS: Final = (
    "scope_kind, scope_ref, kind, text, input_digest, model, generated_at, error_kind"
)

PUT_SUMMARY: Final = """
INSERT INTO summaries (scope_kind, scope_ref, kind, text, input_digest, model, generated_at,
                       error_kind)
VALUES (:scope_kind, :scope_ref, :kind, :text, :input_digest, :model, :generated_at, :error_kind)
ON CONFLICT (scope_kind, scope_ref, kind) DO UPDATE SET
    text = excluded.text,
    input_digest = excluded.input_digest,
    model = excluded.model,
    generated_at = excluded.generated_at,
    error_kind = excluded.error_kind
"""

SELECT_SUMMARIES: Final = (
    f"SELECT {_SUMMARY_COLUMNS} FROM summaries ORDER BY scope_kind, scope_ref, kind"
)


def put_summaries(connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> None:
    """Store the rows of one summary (one per ``kind``), replacing what that scope held."""
    connection.executemany(PUT_SUMMARY, rows)


def load_summaries(connection: sqlite3.Connection) -> list[SummaryRow]:
    """Every stored summary row, grouped by scope."""
    return [SummaryRow.from_db(row) for row in connection.execute(SELECT_SUMMARIES)]
