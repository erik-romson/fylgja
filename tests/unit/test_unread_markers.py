"""A15: unread comes only from an explicit title marker confirmed for the app.

Only Slack has a confirmed marker (the ``! `` prefix and the ``N new items`` segment). Every other
listed app never sets ``unread``, however many numbers or asterisks its titles hold.
Fixtures: ``tests/fixtures/appcontext/unread/<app>.json``.
"""

from __future__ import annotations

from typing import Any

import pytest
from title_support import TITLES_DIR, UNREAD_DIR, item_id, load_items

from fylgja.collectors.appcontext.titles import PARSERS, parse_title

ITEMS = load_items(UNREAD_DIR)
CONFIRMED_MARKER_APPS = {"com.tinyspeck.slackmacgap"}


def _unread(bundle_id: str, title: str) -> int | bool | None:
    context = parse_title(bundle_id, title)
    return None if context is None else context.unread


@pytest.mark.parametrize("pair", ITEMS, ids=item_id)
def test_unread_fixture_items(pair: tuple[str, dict[str, Any]]) -> None:
    _, item = pair
    assert _unread(item["bundle_id"], item["title"]) == item["unread"]


def test_every_listed_app_has_marker_fixtures() -> None:
    stems = {stem for stem, _ in ITEMS}
    assert stems == {path.stem for path in TITLES_DIR.glob("*.json")}


def test_every_app_but_slack_has_only_none_expectations() -> None:
    for stem, item in ITEMS:
        if item["bundle_id"] not in CONFIRMED_MARKER_APPS:
            assert item["unread"] is None, stem


def test_apps_without_a_confirmed_marker_never_set_unread_on_any_fixture_title() -> None:
    for _, item in [*ITEMS, *load_items(TITLES_DIR)]:
        if item["bundle_id"] in CONFIRMED_MARKER_APPS:
            continue
        assert _unread(item["bundle_id"], item["title"]) is None


@pytest.mark.parametrize("bundle_id", sorted(set(PARSERS) - CONFIRMED_MARKER_APPS))
@pytest.mark.parametrize(
    "decoration",
    ["(3) {}", "* {}", "! {}", "{} (3)", "{}*", "(1) * {} (2)", "3 new items - {}"],
)
def test_decorating_any_title_never_creates_a_marker(bundle_id: str, decoration: str) -> None:
    samples = [
        item["title"] for _, item in load_items(TITLES_DIR) if item["bundle_id"] == bundle_id
    ]
    for sample in samples or ["project-a – notes.md"]:
        assert _unread(bundle_id, decoration.format(sample)) is None


def test_slack_reads_the_count_from_new_items() -> None:
    title = "channel-1 (Channel) - workspace-1 - 7 new items - Slack"
    assert _unread("com.tinyspeck.slackmacgap", title) == 7


def test_slack_new_item_count_wins_over_the_bang_prefix() -> None:
    title = "! channel-1 (Channel) - workspace-1 - 4 new items - Slack"
    assert _unread("com.tinyspeck.slackmacgap", title) == 4


def test_slack_bang_prefix_without_a_count_is_a_flag() -> None:
    assert _unread("com.tinyspeck.slackmacgap", "! channel-1 - workspace-1 - Slack") is True


def test_slack_asterisk_prefix_is_not_confirmed_so_not_a_marker() -> None:
    assert _unread("com.tinyspeck.slackmacgap", "* channel-1 - workspace-1 - Slack") is None


def test_slack_zero_new_items_is_not_unread() -> None:
    title = "channel-1 - workspace-1 - 0 new items - Slack"
    assert _unread("com.tinyspeck.slackmacgap", title) is None


def test_the_revalidation_log_names_the_confirmed_marker_apps() -> None:
    from pathlib import Path

    log = Path(__file__).resolve().parents[2] / "docs" / "revalidation" / "2026-10-06-app-titles.md"
    text = log.read_text(encoding="utf-8")
    assert "A15" in text
    assert "new items" in text
