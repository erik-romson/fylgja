"""The navigation item: one per git-root folder and one per app (``docs/decisions/0051``).

An item is the unit the rankers order and the page lists. It gathers the *places* where a folder or
an app is open (windows and agent sessions), the Go targets that focus them, the ticket keys they
mention, a deterministic summary, and the time worked today. This module holds the plain types and
the three small pure helpers the builder in :mod:`fylgja.api.item_inputs` and the rankers share. It
reads no clock, does no I/O and logs nothing.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

__all__ = [
    "MAX_PLACES",
    "GoTarget",
    "Item",
    "ItemKind",
    "Place",
    "PlaceKind",
    "Role",
    "choose_go",
    "folder_name",
    "item_id",
    "order_places",
    "role_of_kinds",
    "ticket_keys",
]

MAX_PLACES: Final = 8
"""The most places an item keeps: open ones first, then the most recently focused."""

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

PlaceKind = Literal["session", "window"]
ItemKind = Literal["folder", "app"]
Role = Literal["agent", "ide", "terminal", "app"]

_ROLE_BY_CONTEXT_KIND: Final[Mapping[str, Role]] = {
    "ide_project": "ide",
    "cmux_tab": "terminal",
    "terminal_session": "terminal",
}
"""A window's role from the kinds of its context items. Kinds, never collector names."""


@dataclass(frozen=True)
class Place:
    """Where an item is open (or was): a session or a window."""

    kind: PlaceKind
    ref: str
    """The session key or the window id."""
    role: Role
    label: str
    """The session kind or the app name, shown on the button."""
    title: str | None
    """The redacted window title, or the session one-liner."""
    workspace: str | None
    open: bool
    waiting: bool
    last_focus_at: datetime | None


@dataclass(frozen=True)
class GoTarget:
    """What a Go button focuses."""

    kind: PlaceKind
    ref: str


@dataclass(frozen=True)
class Item:
    """A folder or an app with its places (see the module docstring)."""

    id: str
    """The first 16 hex digits of ``sha256(key)``: stable, and carries no path."""
    key: str
    """``folder:/work/alpha/main`` or ``app:com.example.app``."""
    kind: ItemKind
    name: str
    """A folder: ``<parent>/<folder>``. An app: its name."""
    path: str | None
    group: str | None
    """The draupnir group of the project. Shown, never grouped by."""
    tickets: tuple[str, ...]
    places: tuple[Place, ...]
    """Open first, then the most recently focused; at most :data:`MAX_PLACES`."""
    state: frozenset[str]
    """Waiting kinds, ``todo_next``, ``dirty`` and ``pr_open``."""
    last_focus_at: datetime | None
    seconds_today: float
    """Focus time since local midnight. Display only."""
    summary: str
    """Deterministic: built from fixed parts in a fixed order."""
    go: GoTarget | None
    """``None`` when nothing is open: there is no button."""
    go_by_role: Mapping[str, GoTarget]
    """``ide``, ``agent`` and ``terminal`` to the best open place of that role."""


def item_id(key: str) -> str:
    """The stable id of an item key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def ticket_keys(texts: Iterable[str], prefixes: Sequence[str]) -> tuple[str, ...]:
    """Keys matching ``\\b(?:P1|P2)-\\d+\\b``, case-insensitive, upper-cased, deduplicated, in
    first-seen order. No prefixes match nothing."""
    if not prefixes:
        return ()
    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(prefix) for prefix in prefixes) + r")-\d+\b", re.IGNORECASE
    )
    found: dict[str, None] = {}
    for text in texts:
        for match in pattern.finditer(text):
            found.setdefault(match.group(0).upper())
    return tuple(found)


def folder_name(path: str) -> str:
    """The last two components of a path: ``/work/cbslink/CBS-512`` gives ``cbslink/CBS-512``."""
    parts = [part for part in path.split("/") if part]
    if not parts:
        return path or "/"
    return "/".join(parts[-2:])


def _moment(place: Place) -> float:
    return (place.last_focus_at or _EPOCH).timestamp()


def order_places(places: Iterable[Place]) -> tuple[Place, ...]:
    """Open places first, then the most recently focused, then by ``ref``; at most
    :data:`MAX_PLACES`."""
    ordered = sorted(places, key=lambda p: (not p.open, -_moment(p), p.kind, p.ref))
    return tuple(ordered[:MAX_PLACES])


def choose_go(places: Sequence[Place]) -> GoTarget | None:
    """A waiting session first, then the open place focused most recently, then any open place by
    ``ref``. ``None`` when no place is open."""
    candidates = [place for place in places if place.open]
    if not candidates:
        return None
    best = min(
        candidates,
        key=lambda p: (not (p.kind == "session" and p.waiting), -_moment(p), p.kind, p.ref),
    )
    return GoTarget(kind=best.kind, ref=best.ref)


def role_of_kinds(kinds: Iterable[str]) -> Role:
    """A window's role: ``ide`` or ``terminal`` when a context item says so, else ``app``."""
    roles = {_ROLE_BY_CONTEXT_KIND[kind] for kind in kinds if kind in _ROLE_BY_CONTEXT_KIND}
    if "ide" in roles:
        return "ide"
    return "terminal" if "terminal" in roles else "app"
