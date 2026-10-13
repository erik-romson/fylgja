"""Golden orders of the ``frecency`` ranker: four worlds, written out in full (placeholder paths).

Each case is a fixed set of items and focus spans at a fixed ``now``; the expected order, the
scores (to 2 decimals) and the explanations are literal. There is no fixture file and no generator.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from frecency_support import ALPHA, APP, BETA, DELTA, GAMMA, at, item, ranked, span, world

from fylgja.rankers.base import RankInput

Golden = list[tuple[str, float, str]]


def _order(data: RankInput) -> Golden:
    return [(row.key, round(row.score, 2), row.explanation) for row in ranked(data)]


def _friday(hours: float, minutes: float = 0.0) -> datetime:
    return at(hours, minutes) - timedelta(days=3)


def test_monday_morning() -> None:
    """Friday's work, nothing open: the last session and the dirty folder lead."""
    spans = [
        span(ALPHA, _friday(9), _friday(11)),
        span(BETA, _friday(11), _friday(12)),
        span(ALPHA, _friday(13), _friday(15, 30)),
        span(BETA, _friday(15, 30), _friday(16)),
    ]
    items = [
        item(ALPHA, state={"dirty"}, last_focus_at=_friday(15, 30)),
        item(BETA, last_focus_at=_friday(16)),
        item(GAMMA, state={"todo_next"}),
        item(APP),
    ]
    assert _order(world(at(9), items, spans)) == [
        (ALPHA, 216.13, "focus 156 · last session +50 · unfinished +10"),
        (GAMMA, 80.0, "todo 80"),
        (BETA, 76.21, "focus 51 · last session +25"),
        (APP, 0.0, "focus"),
    ]


def test_after_lunch() -> None:
    """A morning session, 90 minutes away, and the afternoon session just begun in BETA."""
    spans = [
        span(ALPHA, at(8), at(10, 30)),
        span(BETA, at(10, 30), at(11, 30)),
        span(APP, at(11, 30), at(12)),
        span(BETA, at(13), None),
    ]
    items = [
        item(ALPHA, state={"dirty"}, last_focus_at=at(10, 30)),
        item(BETA, last_focus_at=at(13)),
        item(APP, last_focus_at=at(12)),
        item(GAMMA),
    ]
    assert _order(world(at(13, 10), items, spans)) == [
        (ALPHA, 127.59, "focus 84 · last session +33 · unfinished +10"),
        (APP, 48.36, "focus 28 · last session +20"),
        (BETA, 42.17, "focus 58 · last session +13 · here now −29"),
        (GAMMA, 0.0, "focus"),
    ]


def _call_world(now: datetime) -> RankInput:
    spans = [
        span(ALPHA, at(9), at(10)),
        span(BETA, at(10), at(10, 20)),
        span(ALPHA, at(11), None),
    ]
    items = [
        item(ALPHA, last_focus_at=at(11)),
        item(BETA, state={"permission"}, last_focus_at=at(10, 20)),
        item(GAMMA, state={"todo_next", "pr_open"}, last_focus_at=at(8)),
        item(APP),
    ]
    return world(now, items, spans)


def test_back_from_a_call_that_went_idle() -> None:
    """A waiting agent that changed during the 40 minutes away leads for 300 active seconds."""
    assert _order(_call_world(at(11, 2))) == [
        (BETA, 1149.09, "focus 20 · last session +29 · waiting +100 · while away +1000"),
        (GAMMA, 90.0, "todo 80 · unfinished +10"),
        (ALPHA, 71.39, "focus 55 · last session +44 · here now −27"),
        (APP, 0.0, "focus"),
    ]


def test_the_while_away_lead_ends_after_300_active_seconds() -> None:
    assert _order(_call_world(at(11, 10))) == [
        (BETA, 145.6, "focus 19 · last session +27 · waiting +100"),
        (GAMMA, 90.0, "todo 80 · unfinished +10"),
        (ALPHA, 70.32, "focus 60 · last session +40 · here now −30"),
        (APP, 0.0, "focus"),
    ]


def test_an_agent_is_waiting() -> None:
    """Waiting and finished agents and a failed build beat three hours of decayed focus."""
    spans = [
        span(ALPHA, at(9), at(12)),
        span(BETA, at(12), at(12, 30)),
        span(ALPHA, at(12, 30), None),
    ]
    items = [
        item(ALPHA, state={"dirty"}, last_focus_at=at(12, 30)),
        item(BETA, state={"turn_done"}, last_focus_at=at(12, 30)),
        item(GAMMA, state={"permission"}),
        item(DELTA, state={"build_failed"}, last_focus_at=at(8)),
    ]
    assert _order(world(at(12, 40), items, spans)) == [
        (BETA, 118.32, "focus 28 · turn done +90"),
        (GAMMA, 100.0, "waiting 100"),
        (ALPHA, 86.43, "focus 153 · unfinished +10 · here now −76"),
        (DELTA, 70.0, "build 70"),
    ]
