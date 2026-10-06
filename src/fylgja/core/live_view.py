"""The live view: the daemon's in-memory current model (``PLAN-final-plan.md`` §3.1).

It holds the open windows, the present workspaces, the live sessions and their current activity,
the projects, provider usage, the open and next todos, the latest focus and collector health.
``/now`` is built from it, and SSE publishes its change notifications.

* It changes only through :meth:`LiveView.apply`, which the writer calls with the changes of each
  committed transaction (so it never shows anything that is not in SQLite), and through
  :meth:`LiveView.rebuild`, which reloads it from the store when the daemon starts.
* Each call that changes something increments :attr:`LiveView.version` by one and notifies every
  subscriber with a :class:`LiveUpdate` that lists the effective changes. A change that leaves a
  row equal to the stored one (``last_seen``-style fields excluded) is not a change.
* All of this runs on the event loop thread; subscribers must be quick and must not block.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, cast

from fylgja.store import queries
from fylgja.store.rows import (
    ActivityRow,
    FocusRow,
    HealthRow,
    ProjectRow,
    SessionRow,
    StoreRow,
    TodoRow,
    UsageRow,
    WindowRow,
    WorkspaceRow,
)

__all__ = [
    "FOCUS_KEY",
    "ROW_TYPES",
    "Change",
    "Collection",
    "LiveSnapshot",
    "LiveUpdate",
    "LiveView",
    "Subscriber",
    "load_changes",
]

logger = logging.getLogger("fylgja.live_view")


class Collection(StrEnum):
    WINDOWS = "windows"
    WORKSPACES = "workspaces"
    SESSIONS = "sessions"
    ACTIVITY = "activity"
    PROJECTS = "projects"
    USAGE = "usage"
    FOCUS = "focus"
    COLLECTORS = "collectors"
    TODOS = "todos"


ROW_TYPES: Final[Mapping[Collection, type[StoreRow]]] = MappingProxyType(
    {
        Collection.WINDOWS: WindowRow,
        Collection.WORKSPACES: WorkspaceRow,
        Collection.SESSIONS: SessionRow,
        Collection.ACTIVITY: ActivityRow,
        Collection.PROJECTS: ProjectRow,
        Collection.USAGE: UsageRow,
        Collection.FOCUS: FocusRow,
        Collection.COLLECTORS: HealthRow,
        Collection.TODOS: TodoRow,
    }
)
"""The row type each collection holds."""

FOCUS_KEY: Final = "current"
"""The only key of :attr:`Collection.FOCUS`."""


@dataclass(frozen=True)
class Change:
    """The new current row of one entity, or ``None`` when it is gone."""

    collection: Collection
    key: str
    row: StoreRow | None

    def __post_init__(self) -> None:
        expected = ROW_TYPES[self.collection]
        if self.row is not None and not isinstance(self.row, expected):
            raise TypeError(
                f"{self.collection} holds {expected.__name__}, not {type(self.row).__name__}"
            )


@dataclass(frozen=True)
class LiveUpdate:
    """What one change of the live view did."""

    version: int
    changes: tuple[Change, ...]

    @property
    def collections(self) -> frozenset[Collection]:
        return frozenset(change.collection for change in self.changes)


@dataclass(frozen=True)
class LiveSnapshot:
    """An immutable copy of the live view at one version."""

    version: int
    windows: Mapping[str, WindowRow]
    workspaces: Mapping[str, WorkspaceRow]
    sessions: Mapping[str, SessionRow]
    activity: Mapping[str, ActivityRow]
    projects: Mapping[str, ProjectRow]
    usage: Mapping[str, UsageRow]
    collectors: Mapping[str, HealthRow]
    todos: Mapping[str, TodoRow]
    focused: FocusRow | None


Subscriber = Callable[[LiveUpdate], None]


class LiveView:
    """The current model. See the module docstring for the rules."""

    def __init__(self) -> None:
        self._version = 0
        self._tables: dict[Collection, dict[str, StoreRow]] = {c: {} for c in Collection}
        self._subscribers: list[Subscriber] = []

    # -- reading ------------------------------------------------------------------------------

    @property
    def version(self) -> int:
        return self._version

    def table(self, collection: Collection) -> Mapping[str, StoreRow]:
        return MappingProxyType(self._tables[collection])

    @property
    def windows(self) -> Mapping[str, WindowRow]:
        return cast(Mapping[str, WindowRow], self.table(Collection.WINDOWS))

    @property
    def workspaces(self) -> Mapping[str, WorkspaceRow]:
        return cast(Mapping[str, WorkspaceRow], self.table(Collection.WORKSPACES))

    @property
    def sessions(self) -> Mapping[str, SessionRow]:
        return cast(Mapping[str, SessionRow], self.table(Collection.SESSIONS))

    @property
    def activity(self) -> Mapping[str, ActivityRow]:
        return cast(Mapping[str, ActivityRow], self.table(Collection.ACTIVITY))

    @property
    def projects(self) -> Mapping[str, ProjectRow]:
        return cast(Mapping[str, ProjectRow], self.table(Collection.PROJECTS))

    @property
    def usage(self) -> Mapping[str, UsageRow]:
        return cast(Mapping[str, UsageRow], self.table(Collection.USAGE))

    @property
    def collectors(self) -> Mapping[str, HealthRow]:
        return cast(Mapping[str, HealthRow], self.table(Collection.COLLECTORS))

    @property
    def todos(self) -> Mapping[str, TodoRow]:
        """The open and next todos that are not deleted, by id."""
        return cast(Mapping[str, TodoRow], self.table(Collection.TODOS))

    @property
    def focused(self) -> FocusRow | None:
        row = self._tables[Collection.FOCUS].get(FOCUS_KEY)
        return cast(FocusRow | None, row)

    def snapshot(self) -> LiveSnapshot:
        def copy[R: StoreRow](collection: Collection, _: type[R]) -> Mapping[str, R]:
            return MappingProxyType(cast(dict[str, R], dict(self._tables[collection])))

        return LiveSnapshot(
            version=self._version,
            windows=copy(Collection.WINDOWS, WindowRow),
            workspaces=copy(Collection.WORKSPACES, WorkspaceRow),
            sessions=copy(Collection.SESSIONS, SessionRow),
            activity=copy(Collection.ACTIVITY, ActivityRow),
            projects=copy(Collection.PROJECTS, ProjectRow),
            usage=copy(Collection.USAGE, UsageRow),
            collectors=copy(Collection.COLLECTORS, HealthRow),
            todos=copy(Collection.TODOS, TodoRow),
            focused=self.focused,
        )

    # -- changing -----------------------------------------------------------------------------

    def apply(self, changes: Iterable[Change]) -> LiveUpdate | None:
        """Apply committed changes. Returns the update, or ``None`` when nothing changed."""
        effective: list[Change] = []
        for change in changes:
            table = self._tables[change.collection]
            if change.row is None:
                if table.pop(change.key, None) is not None:
                    effective.append(change)
                continue
            current = table.get(change.key)
            table[change.key] = change.row  # keeps compare=False fields such as last_seen fresh
            if current is None or current != change.row:
                effective.append(change)
        if not effective:
            return None
        self._version += 1
        update = LiveUpdate(self._version, tuple(effective))
        self._notify(update)
        return update

    def rebuild(self, connection: sqlite3.Connection) -> LiveUpdate | None:
        """Replace the whole model with what the store holds (run at start)."""
        loaded = load_changes(connection)
        present = {(change.collection, change.key) for change in loaded}
        removals = [
            Change(collection, key, None)
            for collection, table in self._tables.items()
            for key in table
            if (collection, key) not in present
        ]
        return self.apply([*removals, *loaded])

    # -- notification -------------------------------------------------------------------------

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        """Call ``subscriber`` after every effective change. Returns the unsubscribe function."""
        self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

        return unsubscribe

    def _notify(self, update: LiveUpdate) -> None:
        for subscriber in tuple(self._subscribers):
            try:
                subscriber(update)
            except Exception:
                logger.exception("live view subscriber failed")


def load_changes(connection: sqlite3.Connection) -> list[Change]:
    """The current model as stored, expressed as changes from an empty view."""
    changes: list[Change] = []
    changes += [
        Change(Collection.WINDOWS, r.window_id, r) for r in queries.load_open_windows(connection)
    ]
    changes += [
        Change(Collection.WORKSPACES, r.name, r) for r in queries.load_open_workspaces(connection)
    ]
    changes += [
        Change(Collection.SESSIONS, r.session_key, r)
        for r in queries.load_live_sessions(connection)
    ]
    changes += [
        Change(Collection.ACTIVITY, r.session_key, r)
        for r in queries.load_open_activity(connection)
    ]
    changes += [Change(Collection.PROJECTS, r.path, r) for r in queries.load_projects(connection)]
    changes += [Change(Collection.USAGE, r.key, r) for r in queries.load_open_usage(connection)]
    changes += [Change(Collection.COLLECTORS, r.name, r) for r in queries.load_health(connection)]
    changes += [Change(Collection.TODOS, r.id, r) for r in queries.load_active_todos(connection)]
    focus = queries.latest_focus(connection)
    if focus is not None:
        changes.append(Change(Collection.FOCUS, FOCUS_KEY, focus))
    return changes
