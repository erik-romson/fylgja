"""Typed rows read back from the store.

Each row class names its columns exactly; :meth:`StoreRow.from_db` converts stored times to aware
datetimes, 0/1 flags to ``bool`` and JSON text to Python values. Fields marked
``compare=False`` (``last_seen``, ``updated_at``) change on every observation; leaving them out
of equality means an observation that changes nothing else is not a change of the live view.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, ClassVar, Self

from fylgja.store.db import from_db_time

__all__ = [
    "ActivityRow",
    "FocusRow",
    "HealthRow",
    "ProjectRow",
    "SessionRow",
    "StoreRow",
    "TodoRow",
    "UsageRow",
    "WindowRow",
    "WorkspaceRow",
]


@dataclass(frozen=True)
class StoreRow:
    """Base of every row type. Column names equal field names."""

    _times: ClassVar[frozenset[str]] = frozenset()
    _flags: ClassVar[frozenset[str]] = frozenset()
    _json: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def from_db(cls, row: sqlite3.Row) -> Self:
        names = set(row.keys())
        values: dict[str, Any] = {}
        for item in fields(cls):
            if item.name not in names:
                raise KeyError(f"{cls.__name__}: column {item.name!r} missing from the query")
            value = row[item.name]
            if value is not None:
                if item.name in cls._times:
                    value = from_db_time(value)
                elif item.name in cls._flags:
                    value = bool(value)
                elif item.name in cls._json:
                    value = json.loads(value)
            values[item.name] = value
        return cls(**values)


@dataclass(frozen=True)
class WindowRow(StoreRow):
    """The open interval of one window (``windows_snapshot``)."""

    window_id: str
    source: str
    workspace: str | None
    app_bundle_id: str | None
    app_name: str | None
    app_pid: int | None
    title: str | None
    valid_from: datetime

    _times = frozenset({"valid_from"})


@dataclass(frozen=True)
class WorkspaceRow(StoreRow):
    name: str
    source: str | None
    monitor: str | None
    focused: bool
    manual_label: str | None
    manual_label_at: datetime | None
    inferred_label: str | None
    inferred_at: datetime | None
    first_seen: datetime | None
    closed_at: datetime | None
    last_seen: datetime | None = field(compare=False)

    _times = frozenset({"manual_label_at", "inferred_at", "first_seen", "closed_at", "last_seen"})
    _flags = frozenset({"focused"})


@dataclass(frozen=True)
class SessionRow(StoreRow):
    session_key: str
    kind: str
    native_id: str
    source: str
    pid: int | None
    process_key: str | None
    cwd: str | None
    project_path: str | None
    host_kind: str | None
    host_ref: str | None
    window_id: str | None
    workspace: str | None
    name: str | None
    title: str | None
    model: str | None
    branch: str | None
    state: str | None
    state_since: datetime | None
    waiting_reason: str | None
    parent_key: str | None
    predecessor_key: str | None
    remote_url: str | None
    first_seen: datetime
    ended_at: datetime | None
    last_seen: datetime = field(compare=False)

    _times = frozenset({"state_since", "first_seen", "ended_at", "last_seen"})


@dataclass(frozen=True)
class ActivityRow(StoreRow):
    """The open ``session_snapshots`` interval of one session."""

    session_key: str
    source: str
    valid_from: datetime
    state: str | None
    waiting_reason: str | None
    last_tool: str | None
    current_target: str | None
    open_task: str | None
    stage: dict[str, Any] | None
    last_prompt: str | None
    last_reply: str | None
    one_liner: str | None
    context_pct: float | None
    cost_usd: float | None
    tokens_input: int | None
    tokens_output: int | None
    tokens_cache_read: int | None
    tokens_cache_write: int | None

    _times = frozenset({"valid_from"})
    _json = frozenset({"stage"})


@dataclass(frozen=True)
class ProjectRow(StoreRow):
    path: str
    repo_root: str | None
    remote_url: str | None
    repo_name: str | None
    group_name: str | None
    branch: str | None
    dirty: bool | None
    ahead: int | None
    behind: int | None
    pr_number: int | None
    pr_state: str | None
    review_decision: str | None
    build_state: str | None
    build: dict[str, Any] | None
    build_observed_at: datetime | None
    build_error_kind: str | None
    last_commit_at: datetime | None
    updated_at: datetime = field(compare=False)

    _times = frozenset({"build_observed_at", "last_commit_at", "updated_at"})
    _flags = frozenset({"dirty"})
    _json = frozenset({"build"})


@dataclass(frozen=True)
class HealthRow(StoreRow):
    """One ``collector_health`` row."""

    name: str
    state: str
    since: datetime
    last_ok_at: datetime | None
    restarts: int
    last_error_kind: str | None

    _times = frozenset({"since", "last_ok_at"})


@dataclass(frozen=True)
class FocusRow(StoreRow):
    """The latest focus event."""

    source: str
    window_id: str | None
    workspace: str | None
    app_bundle_id: str | None
    title: str | None
    observed_at: datetime

    _times = frozenset({"observed_at"})


@dataclass(frozen=True)
class UsageRow(StoreRow):
    """The open ``usage_snapshots`` interval of one provider window."""

    collector: str
    provider: str
    window: str
    used_pct: float | None
    resets_at: datetime | None
    cost_usd: float | None
    cost_period: str | None
    source: str
    valid_from: datetime

    _times = frozenset({"resets_at", "valid_from"})

    @property
    def key(self) -> str:
        return f"{self.collector}:{self.provider}:{self.window}"


@dataclass(frozen=True)
class TodoRow(StoreRow):
    """One ``todos`` row that is not deleted."""

    id: str
    scope_kind: str | None
    scope_ref: str | None
    text: str
    status: str
    created_at: datetime
    done_at: datetime | None
    source: str
    source_ref: str | None
    link: str | None
    updated_at: datetime

    _times = frozenset({"created_at", "done_at", "updated_at"})
