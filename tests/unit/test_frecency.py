"""The ``frecency`` ranker, one test per rule, on hand-built inputs (``docs/decisions/0052``)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from frecency_support import (
    ALPHA,
    APP,
    BETA,
    DELTA,
    GAMMA,
    at,
    by_key,
    item,
    parts,
    ranked,
    span,
    world,
)
from pydantic import ValidationError

from fylgja.core.ranking import SCORES
from fylgja.rankers.base import RankInput
from fylgja.rankers.frecency import FrecencySettings, plugin


def test_friday_afternoon_focus_ranks_as_if_just_left_on_monday_morning() -> None:
    spans = [span(ALPHA, at(14), at(15)), span(BETA, at(15), at(15, 30))]
    items = [item(ALPHA), item(BETA)]
    friday = by_key(ranked(world(at(15, 31), items, spans)))
    monday = by_key(ranked(world(at(15, 31) + timedelta(hours=65), items, spans)))
    for key in (ALPHA, BETA):
        assert parts(monday[key])["focus"] == pytest.approx(parts(friday[key])["focus"])


def test_a_two_hour_idle_gap_changes_no_score() -> None:
    items = [item(ALPHA), item(BETA)]
    near = [span(ALPHA, at(9), at(10)), span(BETA, at(10), at(10, 30))]
    far = [span(ALPHA, at(9), at(10)), span(BETA, at(12), at(12, 30))]
    before = by_key(ranked(world(at(10, 31), items, near)))
    after = by_key(ranked(world(at(12, 31), items, far)))
    for key in (ALPHA, BETA):
        assert parts(after[key])["focus"] == pytest.approx(parts(before[key])["focus"])


def test_focus_halves_for_each_half_life_of_active_time() -> None:
    spans = [span(ALPHA, at(9), at(10)), span(BETA, at(10), at(12))]
    rows = by_key(ranked(world(at(12, 1), [item(ALPHA), item(BETA)], spans)))
    assert parts(rows[ALPHA])["focus"] == pytest.approx(60 * 0.5 ** (120 / 120))
    short = by_key(
        ranked(world(at(12, 1), [item(ALPHA), item(BETA)], spans), half_life_active_minutes=60)
    )
    assert parts(short[ALPHA])["focus"] == pytest.approx(60 * 0.5 ** (120 / 60))


def test_the_last_session_part_fades_by_half_after_one_active_hour() -> None:
    def last(current: timedelta) -> float:
        spans = [span(ALPHA, at(8), at(9)), span(BETA, at(10), at(10) + current)]
        data = world(at(10) + current + timedelta(minutes=1), [item(ALPHA), item(BETA)], spans)
        return parts(by_key(ranked(data))[ALPHA])["last session"]

    assert last(timedelta(minutes=70)) == pytest.approx(last(timedelta(minutes=10)) * 0.5)


def test_the_last_session_part_is_a_share_plus_a_bonus_for_the_last_span() -> None:
    spans = [
        span(ALPHA, at(8), at(8, 40)),
        span(BETA, at(8, 40), at(9)),
        span(GAMMA, at(11), at(11, 5)),
    ]
    data = world(at(11, 6), [item(ALPHA), item(BETA), item(GAMMA)], spans)
    rows = by_key(ranked(data))
    assert parts(rows[ALPHA])["last session"] == pytest.approx(40 / 60 * 60 * 0.5 ** (5 / 60))
    assert parts(rows[BETA])["last session"] == pytest.approx(
        (20 / 60 + 0.25) * 60 * 0.5 ** (5 / 60)
    )
    assert "last session" not in parts(rows[GAMMA])


def test_the_last_session_weight_scales_it_and_zero_turns_it_off() -> None:
    spans = [span(ALPHA, at(8), at(9)), span(BETA, at(11), at(11, 10))]
    data = world(at(11, 11), [item(ALPHA), item(BETA)], spans)
    base = parts(by_key(ranked(data))[ALPHA])["last session"]
    double = parts(by_key(ranked(data, last_session_weight=2))[ALPHA])["last session"]
    assert double == pytest.approx(2 * base)
    assert "last session" not in parts(by_key(ranked(data, last_session_weight=0))[ALPHA])


def test_a_waiting_session_beats_three_hours_of_decayed_focus() -> None:
    spans = [span(ALPHA, at(8), at(11)), span(GAMMA, at(11), None)]
    items = [item(ALPHA), item(BETA, state={"permission"}), item(GAMMA)]
    rows = ranked(world(at(15, 1), items, spans), last_session_weight=0)
    scores = {row.key: row.score for row in rows}
    assert scores[BETA] == SCORES["permission"]
    assert scores[BETA] > scores[ALPHA]
    assert parts(by_key(rows)[BETA]) == {"waiting": 100.0}


@pytest.mark.parametrize(
    ("state", "name", "value"),
    [
        ({"input"}, "waiting", 100.0),
        ({"turn_done"}, "turn done", 90.0),
        ({"build_failed"}, "build", 70.0),
        ({"review_requested"}, "build", 70.0),
        ({"todo_next"}, "todo", 80.0),
        ({"dirty"}, "unfinished", 10.0),
        ({"pr_open"}, "unfinished", 10.0),
    ],
)
def test_state_boosts_use_the_next_strip_scores(state: set[str], name: str, value: float) -> None:
    rows = ranked(world(at(9), [item(ALPHA, state=state)], []))
    assert parts(rows[0]) == {name: value}


def test_while_away_items_lead_for_300_active_seconds_and_then_drop_back() -> None:
    items = [
        item(ALPHA, state={"permission"}, last_focus_at=at(9, 30)),
        item(GAMMA, state={"turn_done", "todo_next"}, last_focus_at=at(10)),
        item(BETA, last_focus_at=at(11)),
    ]
    spans = [span(ALPHA, at(9), at(9, 30)), span(BETA, at(11), None)]
    early = ranked(world(at(11, 4), items, spans), last_session_weight=0)
    assert [row.key for row in early][0] == ALPHA
    assert parts(by_key(early)[ALPHA])["while away"] == 1000.0
    assert "while away" not in parts(by_key(early)[GAMMA])
    late = ranked(world(at(11, 6), items, spans), last_session_weight=0)
    assert [row.key for row in late][0] == GAMMA
    assert "while away" not in parts(by_key(late)[ALPHA])


def test_while_away_skips_an_item_focused_since_the_absence_began() -> None:
    spans = [span(ALPHA, at(9), at(9, 30)), span(BETA, at(11), None)]
    items = [item(ALPHA, state={"permission"}, last_focus_at=at(11, 2)), item(BETA)]
    assert "while away" not in parts(by_key(ranked(world(at(11, 4), items, spans)))[ALPHA])


def test_a_glance_does_not_count() -> None:
    spans = [span(ALPHA, at(9), at(9, 1)), span(BETA, at(9, 1), at(9, 1, 30))]
    rows = by_key(ranked(world(at(9, 1, 30), [item(ALPHA), item(BETA)], spans)))
    assert parts(rows[ALPHA]) == {"focus": 0.0}
    assert parts(rows[BETA]) == {"focus": 0.0}


def test_a_short_span_counts_once_it_is_no_longer_recent() -> None:
    spans = [span(ALPHA, at(9), at(9, 1)), span(BETA, at(9, 1), at(9, 5))]
    rows = by_key(ranked(world(at(9, 6), [item(ALPHA), item(BETA)], spans)))
    assert parts(rows[ALPHA])["focus"] == pytest.approx(1 * 0.5 ** (4 / 120))


def test_the_item_holding_the_open_span_loses_half_its_focus() -> None:
    spans = [span(ALPHA, at(9), at(10)), span(BETA, at(10), None)]
    rows = by_key(ranked(world(at(10, 30), [item(ALPHA), item(BETA)], spans)))
    focus = parts(rows[BETA])["focus"]
    assert parts(rows[BETA])["here now"] == pytest.approx(-0.5 * focus)
    assert rows[BETA].score == pytest.approx(0.5 * focus)
    assert "here now" not in parts(rows[ALPHA])


def test_stale_items_sink() -> None:
    spans = [span(ALPHA, at(0), at(1)), span(BETA, at(1), at(18))]
    items = [item(ALPHA, last_focus_at=at(1)), item(BETA, last_focus_at=at(18))]
    rows = by_key(ranked(world(at(18, 1), items, spans)))
    focus = parts(rows[ALPHA])["focus"]
    assert parts(rows[ALPHA])["stale"] == pytest.approx(-0.75 * focus)
    assert "stale" not in parts(rows[BETA])


def test_nothing_is_stale_while_a_span_is_open() -> None:
    spans = [span(ALPHA, at(0), at(1)), span(BETA, at(1), None)]
    rows = by_key(ranked(world(at(18), [item(ALPHA), item(BETA)], spans)))
    assert "stale" not in parts(rows[ALPHA])


def _forgotten_world(*, previous_item: str = DELTA, state: set[str] | None = None) -> RankInput:
    spans = [
        span(ALPHA, at(8), at(8, 40)),
        span(DELTA, at(10), at(10, 20)),
        span(previous_item, at(12), at(12, 20)),
        span(BETA, at(14), at(14, 10)),
    ]
    items = [item(ALPHA, state=state or {"dirty"}), item(BETA), item(DELTA)]
    return world(at(14, 11), items, spans)


def test_unfinished_work_untouched_for_two_sessions_is_forgotten() -> None:
    row = by_key(ranked(_forgotten_world()))[ALPHA]
    assert parts(row)["forgotten"] == 40.0
    assert parts(row)["unfinished"] == 10.0


def test_forgotten_needs_unfinished_work() -> None:
    assert "forgotten" not in parts(by_key(ranked(_forgotten_world(state={"turn_done"})))[ALPHA])


def test_forgotten_needs_thirty_active_minutes() -> None:
    spans = [
        span(ALPHA, at(8), at(8, 20)),
        span(DELTA, at(12), at(12, 20)),
        span(BETA, at(14), at(14, 10)),
    ]
    data = world(at(14, 11), [item(ALPHA, state={"dirty"}), item(BETA), item(DELTA)], spans)
    assert "forgotten" not in parts(by_key(ranked(data))[ALPHA])


def test_forgotten_is_off_after_work_in_the_previous_session() -> None:
    assert "forgotten" not in parts(by_key(ranked(_forgotten_world(previous_item=ALPHA)))[ALPHA])


def test_ties_are_broken_by_the_last_focus_then_by_key() -> None:
    items = [item(BETA), item(ALPHA), item(APP), item(GAMMA, last_focus_at=at(8))]
    spans = [span(DELTA, at(8), at(9))]
    rows = ranked(world(at(9, 1), items, spans))
    assert [row.key for row in rows] == [GAMMA, APP, ALPHA, BETA]
    assert all(row.score == 0 for row in rows)
    assert all(row.parts for row in rows)


def test_spans_are_attributed_by_window_before_folder_and_app() -> None:
    other = span(ALPHA, at(9), at(10))
    windowed = other.__class__(**{**other.__dict__, "window_id": "w-1", "project_path": None})
    data = world(at(10, 1), [item(BETA, window="w-1"), item(ALPHA)], [windowed])
    rows = by_key(ranked(data))
    assert "focus" in parts(rows[BETA])
    assert parts(rows[ALPHA]) == {"focus": 0.0}


def test_a_span_of_a_folder_with_no_item_is_ignored() -> None:
    rows = ranked(world(at(10), [item(ALPHA)], [span(BETA, at(9), at(10))]))
    assert parts(rows[0]) == {"focus": 0.0}


def test_an_app_span_is_attributed_to_the_app_item() -> None:
    rows = by_key(ranked(world(at(10, 1), [item(APP)], [span(APP, at(9), at(10))])))
    assert parts(rows[APP])["focus"] > 0


def test_the_settings_have_bounds_and_defaults() -> None:
    settings = FrecencySettings()
    assert (settings.half_life_active_minutes, settings.last_session_weight) == (120, 1.0)
    for bad in (
        {"half_life_active_minutes": 0},
        {"half_life_active_minutes": 10081},
        {"last_session_weight": -1},
        {"last_session_weight": 11},
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            FrecencySettings(**bad)


def test_the_plugin_describes_itself() -> None:
    assert plugin.name == "frecency"
    assert plugin.config_model is FrecencySettings
    assert len(plugin.description) <= 120
