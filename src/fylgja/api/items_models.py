"""The ``fylgja.items/1`` and ``fylgja.rankers/1`` contracts: the models behind ``GET /items`` and
``GET /rankers`` (``docs/api/items.md``, ``docs/decisions/0051``, ``docs/decisions/0052``).

Both answers are page-ready: the order, the day groups, the Go targets, the durations, the search
and the cap are computed by the daemon, and so are the counts (``total``, ``shown``) and the ranker
choices (``choices``, ``selected``, ``stale_choice``). A client formats times and seconds and
renders the rest as it comes.

The serialization rule of ``fylgja.now/1`` holds here too: every model is a
:class:`~fylgja.api.now_models.NowModel`, dumped with :func:`~fylgja.api.now_models.dump_json`, so a
field without a value is absent and ``null`` never appears. ``/now`` is unchanged.

This module imports only the standard library, Pydantic and :mod:`fylgja.api.now_models`.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import AwareDatetime, Field

from fylgja.api.now_models import NowModel

__all__ = [
    "ITEMS_SCHEMA",
    "RANKERS_SCHEMA",
    "ActivityInfo",
    "AwayChange",
    "DayGroup",
    "GoTarget",
    "ItemPlace",
    "Items",
    "LastSession",
    "NavItem",
    "RankerChoice",
    "RankerInfo",
    "Rankers",
    "ScorePart",
]

ITEMS_SCHEMA: Final = "fylgja.items/1"
"""The version string ``/items`` carries in ``schema``."""
RANKERS_SCHEMA: Final = "fylgja.rankers/1"
"""The version string ``/rankers`` carries in ``schema``."""


class ScorePart(NowModel):
    """One named reason in an item's score (``focus 300``, ``waiting +100``)."""

    name: str
    value: float
    """0 for a part that is a label only."""


class GoTarget(NowModel):
    """What a Go button focuses: a session (``POST /focus/session/{ref}``) or a window
    (``POST /focus/window/{ref}``)."""

    kind: Literal["session", "window"]
    ref: str
    """The session key or the window id."""


class ItemPlace(NowModel):
    """Where an item is open, or was: a session or a window."""

    kind: Literal["session", "window"]
    ref: str
    """The session key or the window id."""
    role: Literal["agent", "ide", "terminal", "app"]
    label: str
    """The session kind or the app name."""
    title: str | None = None
    """The redacted window title, or the session one-liner."""
    workspace: str | None = None
    open: bool
    waiting: bool
    last_focus_at: AwareDatetime | None = None


class NavItem(NowModel):
    """One navigation item: a git-root folder or an app (``docs/decisions/0051``)."""

    id: str
    """16 hex digits of ``sha256(key)``: stable, and carries no path."""
    key: str
    """``folder:<path>`` or ``app:<bundle id>``."""
    kind: Literal["folder", "app"]
    name: str
    """A folder: ``<parent>/<folder>``. An app: its name."""
    path: str | None = None
    group: str | None = None
    """The draupnir group of the project, shown only."""
    tickets: list[str]
    score: float
    """The ranker's score; higher is better for most rankers. Shown next to the explanation."""
    explanation: str
    """The parts as one line: ``focus 300 · waiting +100``."""
    parts: list[ScorePart]
    places: list[ItemPlace]
    """Open first, then the most recently focused; at most eight."""
    state: list[str]
    """Waiting kinds, ``todo_next``, ``dirty`` and ``pr_open``, sorted."""
    last_focus_at: AwareDatetime | None = None
    seconds_today: float
    """Focus time since local midnight."""
    summary: str
    """The deterministic summary: tickets, branch, PR, ``dirty``, IDE file, one-liner."""
    go: GoTarget | None = None
    """The Go button; absent when nothing is open."""
    go_ide: GoTarget | None = None
    go_agent: GoTarget | None = None
    go_terminal: GoTarget | None = None


class DayGroup(NowModel):
    """The items whose last focus fell on one local day (never-focused items: today)."""

    day: str
    """``YYYY-MM-DD``, local time."""
    items: list[str]
    """Item ids, in rank order."""


class AwayChange(NowModel):
    """Something that started waiting while the person was away, on an item not focused since."""

    item: str
    """The item id."""
    name: str
    """The item name."""
    kind: str
    """The waiting kind (``permission``, ``input``, ``turn_done``, ...)."""
    reason: str
    since: AwareDatetime | None = None
    go: GoTarget | None = None
    """The item's Go button; absent when nothing is open."""


class LastSession(NowModel):
    """The last complete work session before the current one (``docs/decisions/0050``)."""

    start: AwareDatetime
    end: AwareDatetime
    active_seconds: float
    items: list[str]
    """The ids of the items focused in it, in order of first focus."""


class ActivityInfo(NowModel):
    """Working time as of ``generated_at``."""

    working: bool
    """A focus span is open and neither idle nor a lock holds."""
    away_since: AwareDatetime | None = None
    """The start of the latest absence (a not-working gap of at least the idle threshold)."""
    away_until: AwareDatetime | None = None
    """Its end; absent while still away or when there was no absence."""
    session_started_at: AwareDatetime | None = None
    """The start of the current work session; absent when there is none."""
    active_seconds_session: float
    """Active seconds of the current work session."""


class Items(NowModel):
    """The navigation items in rank order (``GET /items``)."""

    schema_: Literal["fylgja.items/1"] = Field(default=ITEMS_SCHEMA, alias="schema")
    """The contract version."""
    generated_at: AwareDatetime
    """The time of the answer, floored to 5 s: what the ranker got."""
    ranker: str
    """The ranker asked for (``?rank`` or the default in effect)."""
    ranker_fallback: str | None = None
    """``natural`` when that ranker failed and the floor answered instead."""
    activity: ActivityInfo
    while_away: list[AwayChange]
    """Independent of the ranker, of ``q`` and of ``limit``."""
    last_session: LastSession | None = None
    items: list[NavItem]
    """Rank order, after ``q`` and ``limit``."""
    days: list[DayGroup]
    """The items of ``items`` by local day of their last focus, newest day first."""
    summaries_enabled: bool
    query: str | None = None
    """``q`` as given."""
    total: int
    """How many items there were before ``q`` and ``limit``."""
    shown: int
    """How many items ``items`` holds (after ``q`` and ``limit``)."""


class RankerInfo(NowModel):
    """One discovered ranker, as the registry left it (diagnostics)."""

    name: str
    description: str | None = None
    """Known only for a loaded ranker: a disabled one is never imported."""
    origin: Literal["built-in", "external"]
    distribution: str | None = None
    version: str | None = None
    enabled: bool
    loaded: bool
    error: str | None = None
    """``<kind>: <detail>`` when it could not be loaded."""


class RankerChoice(NowModel):
    """A ranker a client may offer: one that is loaded."""

    name: str
    description: str | None = None
    default: bool
    """It is the default in effect."""


class Rankers(NowModel):
    """Every discovered ranker, and the choices a client offers (``GET /rankers``)."""

    schema_: Literal["fylgja.rankers/1"] = Field(default=RANKERS_SCHEMA, alias="schema")
    """The contract version."""
    default: str
    """The default in effect: ``[items] default_ranker`` when it is loaded, else ``natural``."""
    configured_default: str
    """``[items] default_ranker``."""
    rankers: list[RankerInfo]
    """Every discovered ranker, loaded or not, by name."""
    choices: list[RankerChoice]
    """The loaded rankers only, in ``rankers`` order."""
    selected: str
    """``?rank`` when it names a choice, else ``default``."""
    stale_choice: bool
    """``?rank`` was given and names no choice: a client drops its stored choice."""
