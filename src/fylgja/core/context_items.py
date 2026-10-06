"""The context items attached to windows (``PLAN-final-plan.md`` WP5.3).

A collector with the ``context`` capability emits ``context_item`` events whose ``data`` names a
window: ``window_id``, ``key`` and ``fields`` (and, when the app gives them, ``unread``,
``confidence`` and ``link``). The store appends every such event to ``events``; this class keeps
the **current** item of each ``(window, key)`` in memory, as
:class:`~fylgja.core.daemon.SessionMemory` does for the facts block, because the store has no table
for them. After a restart the items return with the collector's next emission.

It observes records after the ingest gate, so every string is already redacted, and a record whose
window or title the privacy filter ignores never arrives. A ``data.gone`` record withdraws an item,
and a closed window forgets all of its items. Records that do not name a window (a presence call, a
``worked_on`` marker) are not window context and are ignored here.

Nothing in this module knows a collector's name: any plugin may attach context to a window.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from pydantic import JsonValue

from fylgja.models import ContextItemEvent, ContextItemPayload, ContextItemSnapshot, Record

__all__ = ["MAX_ITEMS_PER_WINDOW", "ContextEntry", "ContextMemory"]

MAX_ITEMS_PER_WINDOW: Final = 12
MAX_WINDOWS: Final = 500


@dataclass(frozen=True)
class ContextEntry:
    """One current context item of a window."""

    window_id: str
    key: str
    source: str
    kind: str
    title: str | None
    data: Mapping[str, JsonValue]
    """The parsed fields (``data.fields`` of the record)."""
    unread: int | bool | None
    confidence: str | None
    link: str | None
    tier: int
    observed_at: datetime


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class ContextMemory:
    """Current context items by window, from the records themselves."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], ContextEntry] = {}

    def observe(self, record: Record) -> None:
        if isinstance(record, ContextItemEvent):
            self._item(record.source, record.observed_at, record.payload)
        elif isinstance(record, ContextItemSnapshot):
            for item in record.items:
                self._item(record.source, record.observed_at, item.payload)

    def _item(self, source: str, observed_at: datetime, payload: ContextItemPayload) -> None:
        window_id = _text(payload.data.get("window_id"))
        key = _text(payload.data.get("key"))
        if window_id is None or key is None:
            return
        slot = (window_id, key)
        if payload.data.get("gone") is True:
            self._items.pop(slot, None)
            return
        fields = payload.data.get("fields")
        unread = payload.data.get("unread")
        tier = payload.data.get("tier")
        if slot not in self._items and (
            sum(1 for w, _ in self._items if w == window_id) >= MAX_ITEMS_PER_WINDOW
            or len({w for w, _ in self._items}) >= MAX_WINDOWS
        ):
            return
        self._items[slot] = ContextEntry(
            window_id=window_id,
            key=key,
            source=source,
            kind=payload.context_kind,
            title=payload.title,
            data=fields if isinstance(fields, dict) else {},
            unread=unread if isinstance(unread, int) and not isinstance(unread, float) else None,
            confidence=_text(payload.data.get("confidence")),
            link=_text(payload.data.get("link")),
            tier=tier if isinstance(tier, int) and not isinstance(tier, bool) else 0,
            observed_at=observed_at,
        )

    def forget_window(self, window_id: str) -> None:
        for slot in [slot for slot in self._items if slot[0] == window_id]:
            del self._items[slot]

    def by_window(self, window_ids: Iterable[str]) -> dict[str, tuple[ContextEntry, ...]]:
        """The items of the listed windows, each window's items ordered by tier and key."""
        wanted = set(window_ids)
        found: dict[str, list[ContextEntry]] = {}
        for (window_id, _), entry in self._items.items():
            if window_id in wanted:
                found.setdefault(window_id, []).append(entry)
        return {
            window_id: tuple(sorted(entries, key=lambda e: (e.tier, e.key)))
            for window_id, entries in found.items()
        }

    def __len__(self) -> int:
        return len(self._items)
