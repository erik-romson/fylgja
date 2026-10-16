"""What ``/now`` shows, shared with the daemon's own consumers (``PLAN-final-plan.md`` §4.2).

``/now`` shows only the data of available collectors (enabled, ``starting`` or ``ok``): the live
sessions that have not ended, the windows and the project inputs those collectors report. The
notifier must alert on exactly the ``waiting[]`` list ``/now`` shows, and the daemon cannot import
the API, so the filter lives here and :func:`fylgja.api.now.build_now` uses it too.

Pure functions of a :class:`~fylgja.core.daemon.StateView`; nothing here does I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING

from fylgja.collectors.base import Capability
from fylgja.core.context_items import ContextEntry
from fylgja.core.waiting import WaitingEntry, waiting_on_me
from fylgja.store.rows import SessionRow

if TYPE_CHECKING:
    from fylgja.core.daemon import CollectorState, StateView

__all__ = [
    "available_names",
    "shown_capabilities",
    "shown_contexts",
    "shown_sessions",
    "shown_waiting",
]


def available_names(collectors: Iterable[CollectorState]) -> set[str]:
    """The names of the available collectors."""
    return {c.name for c in collectors if c.available}


def shown_capabilities(collectors: Iterable[CollectorState]) -> frozenset[Capability]:
    """The capabilities of the available collectors."""
    found: set[Capability] = set()
    for collector in collectors:
        if collector.available:
            found |= collector.capabilities
    return frozenset(found)


def shown_sessions(state: StateView) -> list[SessionRow]:
    """The live sessions of available collectors, ordered by key.

    Empty while no available collector has the ``SESSIONS`` capability.
    """
    if Capability.SESSIONS not in shown_capabilities(state.collectors):
        return []
    available = available_names(state.collectors)
    return sorted(
        (s for s in state.live.sessions.values() if s.source in available and s.ended_at is None),
        key=lambda s: s.session_key,
    )


def shown_contexts(
    contexts: Mapping[str, Sequence[ContextEntry]], available: set[str]
) -> dict[str, list[ContextEntry]]:
    """The context entries of available collectors, by window id."""
    shown = {
        window_id: [entry for entry in entries if entry.source in available]
        for window_id, entries in contexts.items()
    }
    return {window_id: entries for window_id, entries in shown.items() if entries}


def shown_waiting(state: StateView) -> list[WaitingEntry] | None:
    """The ``waiting[]`` list of ``/now``, most urgent first.

    ``None`` while no sessions, windows or project collector is available (``/now`` has no
    ``waiting`` then).
    """
    capabilities = shown_capabilities(state.collectors)
    show_windows = Capability.WINDOWS in capabilities
    show_sessions = Capability.SESSIONS in capabilities
    show_projects = show_sessions or Capability.PROJECT_STATE in capabilities
    if not (show_sessions or show_windows or show_projects):
        return None
    live = state.live
    return waiting_on_me(
        now=state.generated_at,
        sessions=shown_sessions(state),
        activity=live.activity,
        projects=live.projects.values() if show_projects else (),
        contexts=(
            shown_contexts(state.contexts, available_names(state.collectors))
            if show_windows
            else None
        ),
        touched=state.touched,
    )
