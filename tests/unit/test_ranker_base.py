"""The ranker API helpers and the two built-in rankers, on the contract's sample worlds."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import BaseModel

from fylgja.api.now import natural_key as now_natural_key
from fylgja.core.activity import build_activity
from fylgja.rankers import recent, workspace
from fylgja.rankers.base import (
    FLOOR_NAME,
    MAX_DESCRIPTION,
    NoSettings,
    Part,
    Ranked,
    RankerPlugin,
    RankInput,
    default_workspace_order,
    explain,
    natural_key,
    natural_order,
    natural_workspace_order,
)
from fylgja.rankers.recent import RecentSettings
from fylgja.testing.rankers import IDLE_THRESHOLD, SESSION_BREAK, sample_inputs

SETTINGS = NoSettings()
RECENT = RecentSettings()
ALPHA = "folder:/work/alpha/main"
BETA = "folder:/work/beta/main"
WORKTREE = "folder:/work/beta/CBS-1"
GAMMA = "folder:/work/gamma/main"
BROWSER = "app:com.example.browser"
MAIL = "app:com.example.mail"


@pytest.fixture
def small() -> RankInput:
    return sample_inputs()["small"]


def _rank(data: RankInput, settings: BaseModel) -> list[Ranked]:
    return []


# -- natural_key ---------------------------------------------------------------------------------


def test_api_now_uses_the_same_natural_key() -> None:
    assert now_natural_key is natural_key
    names = ["10", "web", "2", "Mail", "1", "alpha", "02"]
    assert sorted(names, key=natural_key) == ["1", "02", "2", "10", "alpha", "Mail", "web"]


# -- explanations --------------------------------------------------------------------------------


def test_the_explanation_signs_every_part_but_the_first() -> None:
    parts = (Part("focus", 300.4), Part("last session", 80), Part("here now", -68.2))
    assert explain(parts) == "focus 300 · last session +80 · here now \N{MINUS SIGN}68"


def test_small_values_keep_one_decimal_and_lose_a_trailing_zero() -> None:
    assert (
        explain((Part("a", 5.25), Part("b", 2.0), Part("c", -0.04)))
        == "a 5.2 · b +2 · c \N{MINUS SIGN}0"
    )


def test_a_part_with_value_zero_is_a_label() -> None:
    assert explain((Part("latest focus", 0.0),)) == "latest focus"
    assert explain((Part("focus", -3), Part("waiting", 0))) == "focus \N{MINUS SIGN}3 · waiting"


def test_ranked_explains_its_parts() -> None:
    ranked = Ranked("app:com.example.app", 2.0, (Part("places", 2.0),))
    assert ranked.explanation == "places 2"
    assert explain(()) == ""


# -- the plugin descriptor -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"name": "Bad Name"}, ValueError),
        ({"name": FLOOR_NAME}, ValueError),
        ({"description": ""}, ValueError),
        ({"description": "   "}, ValueError),
        ({"description": "two\nlines"}, ValueError),
        ({"description": "x" * (MAX_DESCRIPTION + 1)}, ValueError),
        ({"rank": "not callable"}, TypeError),
        ({"config_model": dict}, TypeError),
        ({"order_workspaces": 3}, TypeError),
        ({"api_major": True}, TypeError),
        ({"api_major": "1"}, TypeError),
    ],
)
def test_an_invalid_descriptor_is_refused(
    options: dict[str, object], error: type[Exception]
) -> None:
    fields: dict[str, object] = {"name": "mine", "description": "Mine", "rank": _rank, **options}
    with pytest.raises(error):
        RankerPlugin(**fields)  # type: ignore[arg-type]


def test_a_descriptor_defaults_to_no_settings_and_the_current_api() -> None:
    plugin = RankerPlugin("mine", "x" * MAX_DESCRIPTION, _rank)
    assert plugin.config_model is NoSettings
    assert plugin.order_workspaces is None
    assert plugin.api_major == 1


# -- the natural floor and the workspace ranker ----------------------------------------------------


def test_natural_order_follows_the_newest_open_place_workspace(small: RankInput) -> None:
    ranked = natural_order(small)
    assert [(r.key, r.score, r.explanation) for r in ranked] == [
        (ALPHA, 3.0, "workspace 1"),
        (BETA, 2.0, "workspace 2"),
        (BROWSER, 1.0, "workspace 3"),
        (MAIL, 1.0, "workspace 3"),
        (WORKTREE, 0.0, "no workspace"),
        (GAMMA, 0.0, "no workspace"),
    ]


def test_the_workspace_ranker_is_the_floor_with_natural_cards(small: RankInput) -> None:
    assert workspace.plugin.rank(small, SETTINGS) == natural_order(small)
    assert workspace.plugin.order_workspaces is not None
    shuffled = replace(small, workspaces=("10", "1", "2"))
    assert workspace.plugin.order_workspaces(shuffled, []) == ["1", "2", "10"]
    assert natural_workspace_order(shuffled) == ["1", "2", "10"]


def test_a_workspace_no_item_names_still_gets_a_position(small: RankInput) -> None:
    extra = replace(small, workspaces=("1", "2", "3", "10"))
    scores = {r.key: (r.score, r.explanation) for r in natural_order(extra)}
    assert scores[BROWSER] == (1.0, "workspace 4")
    assert scores[ALPHA] == (4.0, "workspace 1")


# -- the default workspace order -------------------------------------------------------------------


def test_the_default_workspace_order_sums_the_scores_of_open_items(small: RankInput) -> None:
    scores = {ALPHA: 1.0, BETA: 5.0, BROWSER: 2.0, MAIL: 2.0, WORKTREE: 100.0, GAMMA: 50.0}
    ranked = [Ranked(key, score, (Part("s", score),)) for key, score in scores.items()]
    # "2": beta 5 (the worktree is closed); "10": browser 2 + mail 2; "1": alpha 1. Gamma has
    # no workspace.
    assert default_workspace_order(small, ranked) == ["2", "10", "1"]


def test_the_default_workspace_order_breaks_ties_by_natural_order(small: RankInput) -> None:
    assert default_workspace_order(small, []) == ["1", "2", "10"]
    flat = [Ranked(key, 1.0, (Part("s", 1.0),)) for key in (ALPHA, BETA, BROWSER)]
    assert default_workspace_order(replace(small, workspaces=("10", "2", "1")), flat) == [
        "1",
        "2",
        "10",
    ]


def test_the_default_workspace_order_ignores_unlisted_workspaces(small: RankInput) -> None:
    ranked = [Ranked(BROWSER, 9.0, (Part("s", 9.0),))]
    assert default_workspace_order(replace(small, workspaces=("1", "2")), ranked) == ["1", "2"]


# -- the recent ranker ---------------------------------------------------------------------------


def test_recent_orders_by_active_minutes_since_the_last_focus(small: RankInput) -> None:
    ranked = recent.rank(small, RECENT)
    assert [(r.key, r.score, r.explanation) for r in ranked] == [
        (ALPHA, 0.0, "latest focus"),
        (BROWSER, -20.0, "active min since focus 20"),
        (WORKTREE, -35.0, "active min since focus 35"),
        (BETA, -50.0, "active min since focus 50"),
        (MAIL, -151.0, "never focused"),
        (GAMMA, -151.0, "never focused"),
    ]


def test_recent_puts_open_never_focused_items_before_closed_ones(small: RankInput) -> None:
    closed = next(i for i in small.items if i.key == MAIL)
    closed = replace(
        closed,
        key="app:com.example.aaa",
        places=tuple(replace(p, open=False) for p in closed.places),
    )
    keys = [r.key for r in recent.rank(replace(small, items=(*small.items, closed)), RECENT)]
    assert keys[-3:] == [MAIL, GAMMA, "app:com.example.aaa"]


def test_recent_does_not_age_while_away(small: RankInput) -> None:
    later = small.now + timedelta(hours=2)
    away = build_activity(
        small.activity.spans,
        now=later,
        suspended=True,
        idle_threshold=IDLE_THRESHOLD,
        session_break=SESSION_BREAK,
    )
    assert recent.rank(replace(small, now=later, activity=away), RECENT) == recent.rank(
        small, RECENT
    )


def test_recent_orders_workspaces_by_their_first_open_item(small: RankInput) -> None:
    ranked = recent.rank(small, RECENT)
    assert recent.order_workspaces(small, ranked) == ["1", "10", "2"]
    assert recent.order_workspaces(small, []) == ["1", "2", "10"]
    assert recent.plugin.order_workspaces is recent.order_workspaces
