"""The ``recent`` ranker leaves peeks out: a short stay after a workspace switch is not a focus
(``docs/decisions/0054``)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest
from frecency_support import ALPHA, APP, BETA, DELTA, GAMMA, at, item, span, world
from pydantic import ValidationError

from fylgja.rankers.base import RankInput
from fylgja.rankers.recent import RecentSettings, plugin
from fylgja.store.rows import SpanRow

NOW = at(9, 45)


def stay(key: str, workspace: str, start: datetime, end: datetime | None) -> SpanRow:
    return replace(span(key, start, end), workspace=workspace)


def order(data: RankInput, min_peek_seconds: float = 20) -> list[tuple[str, str]]:
    ranked = plugin.rank(data, RecentSettings(min_peek_seconds=min_peek_seconds))
    return [(row.key, row.explanation) for row in ranked]


def peek_world(*extra: SpanRow) -> RankInput:
    """GAMMA held on workspace 3; ALPHA on 1; a 12 s look at workspace 9, where APP had focus;
    back to ALPHA; then BETA on 2, open now."""
    spans = sorted(
        [
            *extra,
            stay(GAMMA, "3", at(9), at(9, 20)),
            stay(ALPHA, "1", at(9, 20), at(9, 30)),
            stay(APP, "9", at(9, 30), at(9, 30, 12)),
            stay(ALPHA, "1", at(9, 30, 12), at(9, 40)),
            stay(BETA, "2", at(9, 40), None),
        ],
        key=lambda s: s.started_at,
    )
    items = [
        item(GAMMA, last_focus_at=at(9, 20)),
        item(ALPHA, last_focus_at=at(9, 40)),
        item(APP, last_focus_at=at(9, 30, 12)),
        item(BETA, last_focus_at=NOW),
    ]
    return world(NOW, items, spans)


def test_a_short_look_at_another_workspace_is_not_a_focus() -> None:
    assert order(peek_world()) == [
        (BETA, "latest focus"),
        (ALPHA, "active min since focus 5"),
        (GAMMA, "active min since focus 25"),
        (APP, "never focused · peek"),
    ]


def test_with_min_peek_seconds_0_every_stay_counts() -> None:
    assert order(peek_world(), min_peek_seconds=0) == [
        (BETA, "latest focus"),
        (ALPHA, "active min since focus 5"),
        (APP, "active min since focus 15"),
        (GAMMA, "active min since focus 25"),
    ]


def test_a_peeked_item_keeps_its_last_real_focus() -> None:
    earlier = stay(APP, "9", at(8, 40), at(9))
    assert order(peek_world(earlier))[2:] == [
        (GAMMA, "active min since focus 25"),
        (APP, "active min since focus 45 · peek"),
    ]


def test_the_window_clicked_after_landing_counts_and_the_landing_does_not() -> None:
    spans = [
        stay(ALPHA, "1", at(9), at(9, 30)),
        stay(APP, "9", at(9, 30), at(9, 30, 5)),
        stay(DELTA, "9", at(9, 30, 5), at(9, 31)),
        stay(ALPHA, "1", at(9, 31), None),
    ]
    items = [
        item(ALPHA, last_focus_at=NOW),
        item(APP, last_focus_at=at(9, 30, 5)),
        item(DELTA, last_focus_at=at(9, 31)),
    ]
    assert order(world(NOW, items, spans)) == [
        (ALPHA, "latest focus"),
        (DELTA, "active min since focus 14"),
        (APP, "never focused · peek"),
    ]


@pytest.mark.parametrize(
    ("label", "landing"),
    [
        ("a stay of min_peek_seconds", stay(APP, "9", at(9, 30), at(9, 30, 20))),
        ("a switch inside the workspace", stay(APP, "1", at(9, 30), at(9, 30, 12))),
        ("a stay that ended in idle", stay(APP, "9", at(9, 30), at(9, 30, 5))),
    ],
)
def test_these_stays_are_focus(label: str, landing: SpanRow) -> None:
    assert landing.ended_at is not None
    resume = at(9, 30, 20) if label == "a stay that ended in idle" else landing.ended_at
    spans = [
        stay(ALPHA, "1", at(9), at(9, 30)),
        landing,
        stay(ALPHA, "1", resume, at(9, 40)),
        stay(BETA, "2", at(9, 40), None),
    ]
    items = [
        item(ALPHA, last_focus_at=at(9, 40)),
        item(APP, last_focus_at=landing.ended_at),
        item(BETA, last_focus_at=NOW),
    ]
    explained = dict(order(world(NOW, items, spans)))
    assert "peek" not in explained[APP], label


def test_the_open_span_and_the_first_span_are_never_peeks() -> None:
    spans = [stay(APP, "9", at(9, 44, 50), at(9, 44, 55)), stay(ALPHA, "1", at(9, 44, 55), None)]
    items = [item(APP, last_focus_at=at(9, 44, 55)), item(ALPHA, last_focus_at=NOW)]
    assert dict(order(world(NOW, items, spans))) == {
        ALPHA: "latest focus",
        APP: "active min since focus 0.1",
    }


@pytest.mark.parametrize("table", [{"min_peek_seconds": -1}, {"min_peek_seconds": 601}, {"x": 1}])
def test_bad_recent_settings_are_rejected(table: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        RecentSettings(**table)


def test_recent_refuses_settings_of_another_model() -> None:
    with pytest.raises(TypeError):
        plugin.rank(peek_world(), object())  # type: ignore[arg-type]
