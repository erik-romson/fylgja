"""The built-in ``recent`` ranker: the most recently focused first, in active working time.

Ages are :meth:`Activity.active_since` of the item's last focus, so time away from the keyboard
does not age anything. The order is:

1. focused items, by active minutes since their last focus, ascending (the item in focus now, or
   the last one before an absence, is at 0);
2. items never focused, open ones first;
3. ties by key.

A **peek** is not a focus (``docs/decisions/0054``): a span shorter than ``min_peek_seconds`` that
began on a workspace switch and ended on the next focus change. Switching to a workspace focuses
the window last focused there, so a quick look at a workspace focuses a window nobody chose. When
an item's latest span is a peek, its last focus is the end of its latest span that is not one, and
an item with nothing but peeks counts as never focused.

Parts: ``active min since focus <m>``, ``latest focus`` at 0 and ``never focused``, followed by the
label ``peek`` when a peek was left out. The score is minus the active minutes since the last
focus; an item never focused scores minus every active minute the window holds, minus 1. The
workspace order follows the items: a workspace comes where its first open item is ranked, and
workspaces with none follow in natural order. The one setting is ``min_peek_seconds``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from fylgja.rankers.base import (
    Item,
    Part,
    Ranked,
    RankerPlugin,
    RankInput,
    natural_key,
    spans_by_item,
)
from fylgja.store.rows import SpanRow

__all__ = ["RecentSettings", "order_workspaces", "plugin", "rank"]

SINCE_FOCUS: Final = "active min since focus"
LATEST_FOCUS: Final = "latest focus"
NEVER_FOCUSED: Final = "never focused"
PEEK: Final = "peek"


class RecentSettings(BaseModel):
    """``[rankers.recent]``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_peek_seconds: float = Field(20, ge=0, le=600)
    """A stay this short after a workspace switch is a peek, not a focus; 0 counts every stay."""


def _peeks(spans: Sequence[SpanRow], min_seconds: float) -> set[int]:
    """The indexes of the spans that are peeks: shorter than ``min_seconds``, begun where the span
    before ended on another workspace, and ended where the next span begins."""
    peeks: set[int] = set()
    for index in range(1, len(spans) - 1):
        before, span, after = spans[index - 1], spans[index], spans[index + 1]
        if span.ended_at is None or span.workspace is None or before.workspace is None:
            continue
        if (span.ended_at - span.started_at).total_seconds() >= min_seconds:
            continue
        if before.workspace == span.workspace or before.ended_at != span.started_at:
            continue
        if after.started_at == span.ended_at:
            peeks.add(index)
    return peeks


def _last_focus(
    item: Item, indexes: Sequence[int], spans: Sequence[SpanRow], peeks: set[int], now: datetime
) -> tuple[datetime | None, bool]:
    """The item's last focus with peeks left out, and whether a peek was left out."""
    if not indexes or indexes[-1] not in peeks:
        return item.last_focus_at, False
    kept = [spans[i].ended_at or now for i in indexes if i not in peeks]
    return (max(kept) if kept else None), True


def _open(item: Item) -> bool:
    return any(place.open for place in item.places)


def rank(data: RankInput, settings: BaseModel) -> list[Ranked]:
    """The items most recently focused first, peeks left out (see the module docstring)."""
    if not isinstance(settings, RecentSettings):
        raise TypeError("recent needs RecentSettings")
    activity = data.activity
    spans = activity.spans
    peeks = _peeks(spans, settings.min_peek_seconds)
    grouped = spans_by_item(data) if peeks else {}
    never_score = -(activity.active_seconds / 60 + 1)
    rows: list[tuple[tuple[int, int, float, str], Ranked]] = []
    for item in data.items:
        last, peeked = _last_focus(item, grouped.get(item.key, []), spans, peeks, data.now)
        label = (Part(PEEK, 0.0),) if peeked else ()
        if last is None:
            ranked = Ranked(item.key, never_score, (Part(NEVER_FOCUSED, 0.0), *label))
            rows.append(((1, 0 if _open(item) else 1, 0.0, item.key), ranked))
            continue
        minutes = activity.active_since(last) / 60
        part = Part(LATEST_FOCUS, 0.0) if minutes == 0 else Part(SINCE_FOCUS, minutes)
        rows.append(((0, 0, minutes, item.key), Ranked(item.key, 0.0 - minutes, (part, *label))))
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
    config_model=RecentSettings,
    order_workspaces=order_workspaces,
)
