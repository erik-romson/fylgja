"""Ranker API v1 (``docs/decisions/0052``, ``docs/rankers.md``).

This module is public: it ships in the wheel and external ranker packages import it, like
:mod:`fylgja.collectors.base`. It imports neither FastAPI nor Typer.

A ranker is a **pure** function from a :class:`RankInput` (the navigation items, the working time
and the workspace names) and its own validated settings to the items in rank order, each with a
score and the parts that explain it. It reads no clock (``RankInput.now`` is the time), does no I/O
and logs nothing. Ages are measured in active working time (:class:`Activity`), so nothing a ranker
computes changes while the person is away.

A package exports a :class:`RankerPlugin` as ``plugin`` and names it in the ``fylgja.rankers``
entry-point group. The name ``natural`` is reserved for :func:`natural_order`, the floor the daemon
falls back to when no usable ranker is left; it is not a plugin and cannot be disabled.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from fylgja.collectors.base import PLUGIN_NAME_PATTERN
from fylgja.core.activity import Activity, Away, WorkSession
from fylgja.core.items import GoTarget, Item, Place

__all__ = [
    "ENTRY_POINT_GROUP",
    "FLOOR_NAME",
    "MAX_DESCRIPTION",
    "MAX_EXPLANATION",
    "PART_SEPARATOR",
    "PLUGIN_NAME_PATTERN",
    "RANKER_API_MAJOR",
    "Activity",
    "Away",
    "GoTarget",
    "Item",
    "NoSettings",
    "Part",
    "Place",
    "RankFunction",
    "RankInput",
    "Ranked",
    "RankerPlugin",
    "WorkSession",
    "WorkspaceOrder",
    "default_workspace_order",
    "explain",
    "natural_key",
    "natural_order",
    "natural_workspace_order",
    "spans_by_item",
]

RANKER_API_MAJOR: Final = 1
"""The ranker API major version this core supports."""
ENTRY_POINT_GROUP: Final = "fylgja.rankers"
FLOOR_NAME: Final = "natural"
"""Reserved: the floor ordering the daemon uses when no usable ranker is left. No plugin may take
this name."""
MAX_DESCRIPTION: Final = 120
"""A description is one line of at most this many characters."""
MAX_EXPLANATION: Final = 200
"""The contract's limit on :attr:`Ranked.explanation`."""
PART_SEPARATOR: Final = " · "

_MINUS: Final = "\N{MINUS SIGN}"
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_WORKSPACE_PART: Final = "workspace"
_NO_WORKSPACE_PART: Final = "no workspace"
_FOLDER_PREFIX: Final = "folder:"
_APP_PREFIX: Final = "app:"


def natural_key(name: str) -> tuple[int, int, str, str]:
    """Sort key of a workspace name: numbers numerically first, then the rest by name."""
    if name.isdigit():
        return (0, int(name), "", name)
    return (1, 0, name.casefold(), name)


@dataclass(frozen=True)
class RankInput:
    """What every ranker gets. A ranker's own knobs come from its settings, never from here."""

    now: datetime
    """The time of the answer, floored to 5 s by the daemon: equal inputs give equal answers."""
    items: tuple[Item, ...]
    """The items to order, each with a unique ``key``. Their order carries no meaning."""
    activity: Activity
    """Working time as of ``now``: every age a ranker uses is active time from here."""
    workspaces: tuple[str, ...]
    """The workspace names, in natural order (:func:`natural_key`)."""


@dataclass(frozen=True)
class Part:
    """One named reason in a score: ``focus 300``, ``waiting +100``.

    A part whose value is 0 is a label: the explanation shows its name alone.
    """

    name: str
    value: float


def _number(value: float) -> str:
    size = abs(value)
    return f"{size:.0f}" if size >= 10 else f"{size:.1f}".removesuffix(".0")


def explain(parts: Sequence[Part]) -> str:
    """The parts as one line: ``focus 300 · last session +80 · here now −68``.

    The first value is shown as is and the others with a sign; values from 10 up are whole
    numbers, smaller ones keep one decimal. A part with value 0 shows its name alone.
    """
    texts: list[str] = []
    for index, part in enumerate(parts):
        if part.value == 0:
            texts.append(part.name)
            continue
        if part.value < 0:
            sign = _MINUS
        else:
            sign = "" if index == 0 else "+"
        texts.append(f"{part.name} {sign}{_number(part.value)}")
    return PART_SEPARATOR.join(texts)


@dataclass(frozen=True)
class Ranked:
    """One item in rank order: its key, its score and the parts that explain the score."""

    key: str
    score: float
    """Higher ranks first in the default workspace order. Shown next to the explanation."""
    parts: tuple[Part, ...]

    @property
    def explanation(self) -> str:
        """The parts as one line (:func:`explain`)."""
        return explain(self.parts)


class NoSettings(BaseModel):
    """The settings model of a ranker that has none."""

    model_config = ConfigDict(extra="forbid", frozen=True)


RankFunction = Callable[[RankInput, Any], Sequence[Ranked]]
"""``rank(data, settings)``: ``settings`` is an instance of the plugin's ``config_model``."""
WorkspaceOrder = Callable[[RankInput, Sequence[Ranked]], Sequence[str]]
"""``order_workspaces(data, ranked)``: a permutation of ``data.workspaces``."""


@dataclass(frozen=True)
class RankerPlugin:
    """Descriptor a ranker package exports as ``plugin``; modelled on ``CollectorPlugin``."""

    name: str
    """Matches :data:`PLUGIN_NAME_PATTERN` and the entry-point name; never :data:`FLOOR_NAME`."""
    description: str
    """One line of at most :data:`MAX_DESCRIPTION` characters."""
    rank: RankFunction
    """Every input key exactly once, in rank order. Pure and deterministic."""
    config_model: type[BaseModel] = NoSettings
    """Validates ``[rankers.<name>]`` minus ``enabled``. Every field needs a default."""
    order_workspaces: WorkspaceOrder | None = None
    """The order of the workspace cards; ``None`` uses :func:`default_workspace_order`."""
    api_major: int = RANKER_API_MAJOR

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or PLUGIN_NAME_PATTERN.fullmatch(self.name) is None:
            raise ValueError(f"invalid ranker name {self.name!r}: must match [a-z0-9_-]+")
        if self.name == FLOOR_NAME:
            raise ValueError(f"the ranker name {FLOOR_NAME!r} is reserved for the floor ordering")
        description = self.description
        if (
            not isinstance(description, str)
            or not description.strip()
            or "\n" in description
            or len(description) > MAX_DESCRIPTION
        ):
            raise ValueError(
                f"description must be one non-empty line of at most {MAX_DESCRIPTION} characters"
            )
        if not callable(self.rank):
            raise TypeError("rank must be callable")
        if not (isinstance(self.config_model, type) and issubclass(self.config_model, BaseModel)):
            raise TypeError("config_model must be a pydantic BaseModel subclass")
        if self.order_workspaces is not None and not callable(self.order_workspaces):
            raise TypeError("order_workspaces must be callable or None")
        if isinstance(self.api_major, bool) or not isinstance(self.api_major, int):
            raise TypeError("api_major must be an int")


def spans_by_item(data: RankInput) -> dict[str, list[int]]:
    """The indexes of ``data.activity.spans`` of each item, in span order.

    A span belongs to the item that holds its window as a place; a span of a window that is no
    longer a place belongs to the folder or the app it was recorded in (its bundle id, else its
    name). A span of none of the items
    is left out.
    """
    by_window: dict[str, str] = {}
    for item in sorted(data.items, key=lambda i: i.key):
        for place in item.places:
            if place.kind == "window":
                by_window.setdefault(place.ref, item.key)
    keys = {item.key for item in data.items}
    grouped: dict[str, list[int]] = defaultdict(list)
    for index, span in enumerate(data.activity.spans):
        key = by_window.get(span.window_id) if span.window_id else None
        if key is None and span.project_path:
            key = _FOLDER_PREFIX + span.project_path
        app = span.app_bundle_id or span.app_name
        if (key is None or key not in keys) and app:
            key = _APP_PREFIX + app
        if key is not None and key in keys:
            grouped[key].append(index)
    return grouped


def _moment(place: Place) -> float:
    return (place.last_focus_at or _EPOCH).timestamp()


def _home(item: Item) -> str | None:
    """The workspace of the item's newest open place that has one (ties by kind and ref)."""
    candidates = [place for place in item.places if place.open and place.workspace is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda p: (-_moment(p), p.kind, p.ref)).workspace


def _positions(names: Iterable[str]) -> dict[str, int]:
    """1-based positions of ``names`` in natural order."""
    ordered = sorted(set(names), key=natural_key)
    return {name: index for index, name in enumerate(ordered, start=1)}


def natural_order(data: RankInput) -> list[Ranked]:
    """The floor: items by the natural order of their newest open place's workspace, then key.

    Not a plugin. An item with no open place in a workspace comes after the others. The part is
    ``workspace <n>``, the 1-based position of that workspace in natural order (``no workspace``
    otherwise); the score is the number of workspaces minus ``n`` plus 1, so the first workspace
    scores highest and an item with no workspace scores 0.
    """
    homes = {item.key: _home(item) for item in data.items}
    positions = _positions([*data.workspaces, *(h for h in homes.values() if h is not None)])
    count = len(positions)
    rows: list[tuple[int, str, Ranked]] = []
    for item in data.items:
        home = homes[item.key]
        if home is None:
            part = Part(_NO_WORKSPACE_PART, 0.0)
            rows.append((count + 1, item.key, Ranked(item.key, 0.0, (part,))))
            continue
        position = positions[home]
        part = Part(_WORKSPACE_PART, float(position))
        rows.append((position, item.key, Ranked(item.key, float(count - position + 1), (part,))))
    rows.sort(key=lambda row: (row[0], row[1]))
    return [ranked for _, _, ranked in rows]


def natural_workspace_order(data: RankInput, ranked: Sequence[Ranked] = ()) -> list[str]:
    """``data.workspaces`` in natural order: the order of today's workspace cards."""
    return sorted(data.workspaces, key=natural_key)


def default_workspace_order(data: RankInput, ranked: Sequence[Ranked]) -> list[str]:
    """Workspaces by the summed score of the items with an open place in them; ties by natural
    order. An item counts once per workspace. Returns a permutation of ``data.workspaces``."""
    scores: Mapping[str, float] = {r.key: r.score for r in ranked}
    totals = dict.fromkeys(data.workspaces, 0.0)
    for item in sorted(data.items, key=lambda i: i.key):
        score = scores.get(item.key)
        if score is None:
            continue
        shown = {p.workspace for p in item.places if p.open and p.workspace is not None}
        for workspace in sorted(shown):
            if workspace in totals:
                totals[workspace] += score
    return sorted(data.workspaces, key=lambda name: (-totals[name], natural_key(name)))
