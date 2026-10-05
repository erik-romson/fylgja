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
    FocusRow,
    HealthRow,
    ProjectRow,
    SessionRow,
    StoreRow,
    UsageRow,
    WindowRow,
    WorkspaceRow,
)

__all__ = [
    "ACTIVITY_CARRIED",
    "ACTIVITY_OWNED",
    "Put",
    "PutStatus",
    "checkpoint_truncate",
    "close_missing_activity",
    "close_missing_usage",
    "close_missing_windows",
    "close_open_activity",
    "close_workspaces_missing",
    "delete_expired",
    "end_missing_sessions",
    "ensure_session",
    "get_session",
    "get_workspace",
    "insert_event",
    "latest_focus",
    "load_health",
    "load_live_sessions",
    "load_open_activity",
    "load_open_usage",
    "load_open_windows",
    "load_open_workspaces",
    "load_projects",
    "optimize",
    "put_activity",
    "put_health",
    "put_usage",
    "put_window",
    "unfocus_other_workspaces",
    "update_session_from_activity",
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
    source = excluded.source,
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
    (path, repo_root, remote_url, repo_name, branch, dirty, ahead, behind, pr_number, pr_state,
     review_decision, last_commit_at, updated_at)
VALUES
    (:path, :repo_root, :remote_url, :repo_name, :branch, :dirty, :ahead, :behind, :pr_number,
     :pr_state, :review_decision, :last_commit_at, :updated_at)
ON CONFLICT (path) DO UPDATE SET
    repo_root = coalesce(excluded.repo_root, projects.repo_root),
    remote_url = coalesce(excluded.remote_url, projects.remote_url),
    repo_name = coalesce(excluded.repo_name, projects.repo_name),
    branch = coalesce(excluded.branch, projects.branch),
    dirty = coalesce(excluded.dirty, projects.dirty),
    ahead = coalesce(excluded.ahead, projects.ahead),
    behind = coalesce(excluded.behind, projects.behind),
    pr_number = coalesce(excluded.pr_number, projects.pr_number),
    pr_state = coalesce(excluded.pr_state, projects.pr_state),
    review_decision = coalesce(excluded.review_decision, projects.review_decision),
    last_commit_at = coalesce(excluded.last_commit_at, projects.last_commit_at),
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


def _get_project(connection: sqlite3.Connection, path: str) -> ProjectRow:
    row = connection.execute(SELECT_PROJECT, (path,)).fetchone()
    if row is None:  # pragma: no cover - callers upsert first
        raise LookupError(path)
    return ProjectRow.from_db(row)


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
) -> list[str]:
    """Close the open usage rows of ``collector`` whose ``(provider, window)`` is not present."""
    closed: list[str] = []
    for row in connection.execute(SELECT_OPEN_USAGE_OF_COLLECTOR, (collector,)).fetchall():
        if (row["provider"], row["window"]) in present or row["valid_from"] > at:
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
