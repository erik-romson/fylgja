"""The built-in ``frecency`` ranker: focus decayed in active working time (``docs/decisions/0052``).

Every age is **active** time (:meth:`Activity.active_since`), so nothing changes while the person is
away: Friday afternoon's work ranks as if it were left on Monday morning, and a two-hour idle gap
changes no score. The score of an item is::

    recency(span) = 0.5 ^ (active_since(span.end) / (half_life_active_minutes * 60))
    focus         = sum of  span minutes * recency(span)  over the item's spans (glances left out)
    last session  = (share of the previous work session's active time spent in the item
                     + LAST_SESSION_BONUS if it held the last span of that session)
                    * LAST_SESSION_SCALE * last_session_weight
                    * 0.5 ^ (active seconds of the current work session / 3600)
    boosts        = SCORES[kind] for a waiting kind (permission or input, turn done, build failed
                    or review requested), the todo score for a ``next`` todo, UNFINISHED_BOOST for
                    a dirty folder or an open pull request
    forgotten     = FORGOTTEN_BOOST when the item has at least FORGOTTEN_MINUTES active minutes in
                    the last FORGOTTEN_SESSIONS work sessions, none in the current or the previous
                    one, and is unfinished (dirty, open pull request or a ``next`` todo)
    while away    = WHILE_AWAY_BOOST for a waiting item not focused since the latest absence began,
                    until WHILE_AWAY_WINDOW active seconds have passed since the return
    here now      = HERE_NOW_FACTOR * focus for the item holding the open span
    stale         = STALE_FACTOR * focus when no span is open and the last focus is more than
                    STALE_ACTIVE_HOURS active hours ago
    score         = focus + last session + boosts + forgotten + while away + penalties

The order is the score descending, then the active time since the last focus ascending (an item
never focused last), then the key. A window's spans belong to the item that holds the window as a
place; spans of a window that is no longer a place belong to the folder or app they were recorded
in. The settings are ``half_life_active_minutes`` and ``last_session_weight``; the work-session
break comes from ``[items]`` through :class:`Activity`.

The ``Item`` carries no time for a waiting entry, so "changed while away" is read as "waiting, and
not focused since the absence began".
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from fylgja.core.activity import Activity
from fylgja.core.items import Item
from fylgja.core.ranking import SCORES, TODO
from fylgja.core.waiting import BUILD_FAILED, INPUT, PERMISSION, REVIEW_REQUESTED, TURN_DONE
from fylgja.rankers.base import Part, Ranked, RankerPlugin, RankInput, spans_by_item

__all__ = ["FrecencySettings", "plugin", "rank"]

LAST_SESSION_SCALE: Final = 60.0
"""A share of 1.0 of the previous work session is worth this many points (times the weight)."""
LAST_SESSION_BONUS: Final = 0.25
"""Added to the share of the item that held the last span of the previous work session."""
LAST_SESSION_FADE_SECONDS: Final = 3600.0
"""The last-session part halves for each active hour worked in the current work session."""
UNFINISHED_BOOST: Final = 10.0
"""A dirty folder or an open pull request."""
FORGOTTEN_BOOST: Final = 40.0
"""Unfinished work that was busy a while ago and untouched since."""
FORGOTTEN_MINUTES: Final = 30.0
"""Active minutes in the last work sessions that make unfinished work worth remembering."""
FORGOTTEN_SESSIONS: Final = 5
"""How many of the latest work sessions the forgotten rule looks at."""
WHILE_AWAY_BOOST: Final = 1000.0
"""Leads the list for a waiting item that changed while the person was away."""
WHILE_AWAY_WINDOW: Final = 300.0
"""Active seconds after the return during which the while-away boost lasts."""
HERE_NOW_FACTOR: Final = -0.5
"""The item holding the open span loses half its focus: it is already in front of the person."""
STALE_FACTOR: Final = -0.75
"""An item with nothing open and no focus for a long time loses three quarters of its focus."""
STALE_ACTIVE_HOURS: Final = 16.0
"""Active hours since the last focus after which an item is stale."""
GLANCE_SECONDS: Final = 120.0
"""A span shorter than this that ended less than this many active seconds ago is a glance."""

FOCUS: Final = "focus"
LAST_SESSION: Final = "last session"
WAITING: Final = "waiting"
TURN_DONE_PART: Final = "turn done"
BUILD: Final = "build"
TODO_PART: Final = "todo"
UNFINISHED: Final = "unfinished"
FORGOTTEN: Final = "forgotten"
WHILE_AWAY: Final = "while away"
HERE_NOW: Final = "here now"
STALE: Final = "stale"

_TODO_NEXT: Final = "todo_next"
_DIRTY: Final = "dirty"
_PR_OPEN: Final = "pr_open"
_WAITING_KINDS: Final = (PERMISSION, INPUT)
_BUILD_KINDS: Final = (BUILD_FAILED, REVIEW_REQUESTED)
_NEVER: Final = float("inf")


class FrecencySettings(BaseModel):
    """``[rankers.frecency]``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    half_life_active_minutes: float = Field(120, gt=0, le=10080)
    """Focus loses half its weight after this many active minutes."""
    last_session_weight: float = Field(1.0, ge=0, le=10)
    """Scales the last-session part; 0 turns it off."""


def _session_seconds(activity: Activity, indexes: Sequence[int]) -> dict[int, float]:
    """Active seconds of the spans at ``indexes`` in each work session (by session index)."""
    starts = [session.start for session in activity.sessions]
    seconds: dict[int, float] = defaultdict(float)
    for index in indexes:
        span = activity.spans[index]
        end = span.ended_at or activity.now
        position = bisect_right(starts, span.started_at) - 1
        if position >= 0:
            seconds[position] += (end - span.started_at).total_seconds()
    return seconds


def _unfinished(item: Item) -> bool:
    return bool(item.state & {_DIRTY, _PR_OPEN, _TODO_NEXT})


def rank(data: RankInput, settings: BaseModel) -> list[Ranked]:
    """The items by decayed focus plus the state boosts (see the module docstring)."""
    assert isinstance(settings, FrecencySettings)
    activity = data.activity
    half_life = settings.half_life_active_minutes * 60.0
    grouped = spans_by_item(data)
    sessions = activity.sessions
    current_index = len(sessions) - 1 if activity.current is not None else None
    previous_index = None
    if activity.previous is not None:
        previous_index = len(sessions) - 2 if activity.current is not None else len(sessions) - 1
    recent_indexes = range(max(0, len(sessions) - FORGOTTEN_SESSIONS), len(sessions))
    fade = 0.5 ** (activity.active_in_current / LAST_SESSION_FADE_SECONDS)
    previous_total = activity.previous.active_seconds if activity.previous is not None else 0.0
    last_span_item: str | None = None
    if previous_index is not None:
        previous_end = sessions[previous_index].end
        for key, indexes in grouped.items():
            if any(activity.spans[i].ended_at == previous_end for i in indexes):
                last_span_item = key if last_span_item is None else min(last_span_item, key)
    holding = activity.open_span
    open_key: str | None = None
    if holding is not None:
        for key, indexes in grouped.items():
            if any(activity.spans[i] is holding for i in indexes):
                open_key = key
                break
    returned = activity.away is not None and (
        activity.away.until is None
        or activity.active_since(activity.away.until) < WHILE_AWAY_WINDOW
    )

    rows: list[tuple[float, float, str, Ranked]] = []
    for item in data.items:
        indexes = grouped.get(item.key, [])
        focus = 0.0
        for i in indexes:
            span = activity.spans[i]
            end = span.ended_at or activity.now
            length = (end - span.started_at).total_seconds()
            age = activity.active_since(end)
            if length < GLANCE_SECONDS and age < GLANCE_SECONDS:
                continue
            focus += (length / 60.0) * 0.5 ** (age / half_life)
        parts: list[Part] = []
        if focus > 0:
            parts.append(Part(FOCUS, focus))
        score = focus

        by_session = _session_seconds(activity, indexes) if indexes else {}
        if previous_index is not None and previous_total > 0:
            share = by_session.get(previous_index, 0.0) / previous_total
            if item.key == last_span_item:
                share += LAST_SESSION_BONUS
            last = share * LAST_SESSION_SCALE * settings.last_session_weight * fade
            if last > 0:
                parts.append(Part(LAST_SESSION, last))
                score += last

        state = item.state
        boosts = (
            (WAITING, max((SCORES[k] for k in _WAITING_KINDS if k in state), default=0.0)),
            (TURN_DONE_PART, SCORES[TURN_DONE] if TURN_DONE in state else 0.0),
            (BUILD, max((SCORES[k] for k in _BUILD_KINDS if k in state), default=0.0)),
            (TODO_PART, SCORES[TODO] if _TODO_NEXT in state else 0.0),
            (UNFINISHED, UNFINISHED_BOOST if state & {_DIRTY, _PR_OPEN} else 0.0),
        )
        for name, value in boosts:
            if value:
                parts.append(Part(name, value))
                score += value

        if _unfinished(item) and by_session:
            total = sum(by_session[i] for i in recent_indexes if i in by_session)
            seen_lately = any(
                by_session.get(i, 0.0) > 0 for i in (current_index, previous_index) if i is not None
            )
            if total >= FORGOTTEN_MINUTES * 60.0 and not seen_lately:
                parts.append(Part(FORGOTTEN, FORGOTTEN_BOOST))
                score += FORGOTTEN_BOOST

        if returned and activity.away is not None:
            changed = any(k in state for k in (*_WAITING_KINDS, TURN_DONE, *_BUILD_KINDS))
            untouched = item.last_focus_at is None or item.last_focus_at <= activity.away.since
            if changed and untouched:
                parts.append(Part(WHILE_AWAY, WHILE_AWAY_BOOST))
                score += WHILE_AWAY_BOOST

        age_since_focus = (
            _NEVER if item.last_focus_at is None else activity.active_since(item.last_focus_at)
        )
        if item.key == open_key and focus > 0:
            here = HERE_NOW_FACTOR * focus
            parts.append(Part(HERE_NOW, here))
            score += here
        elif holding is None and focus > 0 and age_since_focus > STALE_ACTIVE_HOURS * 3600.0:
            stale = STALE_FACTOR * focus
            parts.append(Part(STALE, stale))
            score += stale

        if not parts:
            parts.append(Part(FOCUS, 0.0))
        rows.append((-score, age_since_focus, item.key, Ranked(item.key, score, tuple(parts))))
    rows.sort(key=lambda row: row[:3])
    return [ranked for *_, ranked in rows]


plugin = RankerPlugin(
    name="frecency",
    description="Focus on working time, decayed; last work session; waiting and unfinished work",
    rank=rank,
    config_model=FrecencySettings,
)
