"""The built-in ``recent`` ranker: the most recently focused first, in active working time.

Ages are :meth:`Activity.active_since` of the item's last focus, so time away from the keyboard
does not age anything. The order is:

1. focused items, by active minutes since their last focus, ascending (the item in focus now, or
   the last one before an absence, is at 0);
2. items never focused, open ones first;
3. ties by key.

Parts: ``active min since focus <m>``, ``latest focus`` at 0 and ``never focused``. The score is
minus the active minutes since the last focus; an item never focused scores minus every active
minute the window holds, minus 1. The workspace order follows the items: a workspace comes where
its first open item is ranked, and workspaces with none follow in natural order. No settings.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel

from fylgja.rankers.base import Item, Part, Ranked, RankerPlugin, RankInput, natural_key

__all__ = ["order_workspaces", "plugin", "rank"]

SINCE_FOCUS: Final = "active min since focus"
LATEST_FOCUS: Final = "latest focus"
NEVER_FOCUSED: Final = "never focused"


def _open(item: Item) -> bool:
    return any(place.open for place in item.places)


def rank(data: RankInput, settings: BaseModel) -> list[Ranked]:
    """The items most recently focused first (see the module docstring)."""
    activity = data.activity
    never_score = -(activity.active_seconds / 60 + 1)
    rows: list[tuple[tuple[int, int, float, str], Ranked]] = []
    for item in data.items:
        if item.last_focus_at is None:
            ranked = Ranked(item.key, never_score, (Part(NEVER_FOCUSED, 0.0),))
            rows.append(((1, 0 if _open(item) else 1, 0.0, item.key), ranked))
            continue
        minutes = activity.active_since(item.last_focus_at) / 60
        part = Part(LATEST_FOCUS, 0.0) if minutes == 0 else Part(SINCE_FOCUS, minutes)
        rows.append(((0, 0, minutes, item.key), Ranked(item.key, 0.0 - minutes, (part,))))
    rows.sort(key=lambda row: row[0])
    return [ranked for _, ranked in rows]


def order_workspaces(data: RankInput, ranked: Sequence[Ranked]) -> list[str]:
    """Workspaces where their first open item is ranked; the rest in natural order."""
    position = {r.key: index for index, r in enumerate(ranked)}
    first: dict[str, int] = {}
    for item in data.items:
        index = position.get(item.key)
        if index is None:
            continue
        for place in item.places:
            if place.open and place.workspace is not None:
                first[place.workspace] = min(first.get(place.workspace, index), index)
    last = len(position)
    return sorted(data.workspaces, key=lambda name: (first.get(name, last), natural_key(name)))


plugin = RankerPlugin(
    name="recent",
    description="Most recently focused first, in active working time",
    rank=rank,
    order_workspaces=order_workspaces,
)
