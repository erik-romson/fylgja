"""Per-app title parsers (``PLAN-final-plan.md`` WP5.3 first tier).

Fixtures under ``tests/fixtures/appcontext/titles/<app>.json`` keep the real title formats observed
on the installed app versions, with placeholder content.
"""

from __future__ import annotations

from typing import Any

import pytest
from title_support import TITLES_DIR, as_expected, item_id, load_items

from fylgja.collectors.appcontext import titles
from fylgja.collectors.appcontext.titles import (
    JETBRAINS_BUNDLES,
    MAX_TITLE_CHARS,
    PARSERS,
    TitleContext,
    parse_title,
)

ITEMS = load_items(TITLES_DIR)
LISTED_APPS = {
    "cmux",
    "jetbrains",
    "datagrip",
    "chrome",
    "teams",
    "obsidian",
    "outlook",
    "slack",
}


@pytest.mark.parametrize("pair", ITEMS, ids=item_id)
def test_every_fixture_item_parses_to_its_expected_context(
    pair: tuple[str, dict[str, Any]],
) -> None:
    _, item = pair
    assert as_expected(parse_title(item["bundle_id"], item["title"])) == item["expected"]


def test_there_is_a_fixture_file_for_every_listed_app() -> None:
    assert {stem for stem, _ in ITEMS} == LISTED_APPS


def test_every_parser_in_the_table_is_exercised_by_a_fixture() -> None:
    covered = {item["bundle_id"] for _, item in ITEMS}
    # Only the observed JetBrains bundle ids need an item each; the rest share the parser.
    assert covered <= set(PARSERS)
    for bundle_id in (
        "com.cmuxterm.app",
        "com.jetbrains.intellij",
        "com.jetbrains.pycharm",
        "com.jetbrains.datagrip",
        "com.google.Chrome",
        "com.microsoft.teams2",
        "md.obsidian",
        "com.microsoft.Outlook",
        "com.tinyspeck.slackmacgap",
    ):
        assert bundle_id in covered


def test_the_table_covers_every_jetbrains_bundle_id_with_one_parser() -> None:
    parsers = {PARSERS[bundle_id] for bundle_id in JETBRAINS_BUNDLES}
    assert parsers == {titles.parse_jetbrains}
    assert PARSERS["com.jetbrains.datagrip"] is titles.parse_datagrip


@pytest.mark.parametrize("bundle_id", sorted(PARSERS))
def test_an_empty_or_blank_title_gives_none(bundle_id: str) -> None:
    assert parse_title(bundle_id, "") is None
    assert parse_title(bundle_id, "   \t ") is None


@pytest.mark.parametrize(
    "bundle_id",
    ["", "com.example.unknown", "com.apple.Terminal", "com.googlex.Chrome", "COM.GOOGLE.CHROME"],
)
def test_an_unknown_bundle_id_gives_none(bundle_id: str) -> None:
    assert parse_title(bundle_id, "project-a – notes.md") is None


def test_the_parser_is_chosen_by_bundle_id_not_by_the_shape_of_the_title() -> None:
    title = "Chat | Person, First | Microsoft Teams"
    assert parse_title("com.microsoft.teams2", title) is not None
    assert parse_title("com.google.Chrome", title) is None
    assert parse_title("com.tinyspeck.slackmacgap", title) is None


def test_surrounding_whitespace_is_ignored() -> None:
    context = parse_title("com.jetbrains.pycharm", "  project-b – handler.py \n")
    assert context == TitleContext(
        kind="ide", fields={"project": "project-b", "file": "handler.py"}, confidence="high"
    )


def test_a_huge_title_is_cut_before_matching() -> None:
    title = "project-a – " + "x" * (MAX_TITLE_CHARS * 4)
    context = parse_title("com.jetbrains.intellij", title)
    assert context is not None
    assert len(context.fields["file"]) < MAX_TITLE_CHARS


def test_a_context_is_immutable_and_unread_defaults_to_none() -> None:
    context = TitleContext(kind="ide")
    assert context.unread is None
    assert context.fields == {}
    assert context.confidence == "high"
    with pytest.raises(AttributeError):
        context.kind = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("bundle_id", "title"),
    [
        ("com.jetbrains.intellij", " – notes.md"),
        ("com.jetbrains.intellij", "[~/work] – notes.md"),
        ("com.jetbrains.intellij", "project-a – "),
        ("com.jetbrains.datagrip", " – console-a"),
        ("com.tinyspeck.slackmacgap", " (Channel) - workspace-1 - Slack"),
        ("com.tinyspeck.slackmacgap", "! (Channel) - workspace-1 - Slack"),
    ],
)
def test_titles_with_an_empty_part_do_not_crash(bundle_id: str, title: str) -> None:
    parse_title(bundle_id, title)


def test_a_jetbrains_title_with_an_empty_file_part_is_project_only() -> None:
    context = parse_title("com.jetbrains.intellij", "project-a – ")
    assert context is not None
    assert context.fields == {"project": "project-a"}


def test_a_jetbrains_title_without_a_project_gives_none() -> None:
    assert parse_title("com.jetbrains.intellij", "– ") is None


def test_the_en_dash_is_the_separator_not_a_hyphen() -> None:
    context = parse_title("com.jetbrains.intellij", "project-a - notes.md")
    assert context is not None
    assert context.fields == {"project": "project-a - notes.md"}


def test_a_square_bracket_that_is_not_a_path_stays_in_the_project_name() -> None:
    context = parse_title("com.jetbrains.intellij", "project [a] – notes.md")
    assert context is not None
    assert context.fields == {"project": "project [a]", "file": "notes.md"}


def test_a_file_name_with_a_colon_but_no_capital_is_not_a_view() -> None:
    context = parse_title("com.jetbrains.intellij", "project-a – notes: a.md")
    assert context is not None
    assert context.fields == {"project": "project-a", "file": "notes: a.md"}
    assert context.confidence == "high"


def test_chrome_uses_the_last_browser_name_as_the_split() -> None:
    context = parse_title("com.google.Chrome", "A - Google Chrome - B - Google Chrome - person-1")
    assert context is not None
    assert context.fields == {
        "page_title": "A - Google Chrome - B",
        "profile": "person-1",
    }


def test_the_welcome_screens_have_no_project() -> None:
    for bundle_id, kind in (
        ("com.jetbrains.pycharm", "ide"),
        ("com.jetbrains.datagrip", "database"),
    ):
        context = parse_title(bundle_id, "Welcome to Something")
        assert context == TitleContext(kind=kind, fields={"screen": "welcome"}, confidence="low")


def test_parsers_do_not_mutate_or_share_field_dicts() -> None:
    first = parse_title("com.cmuxterm.app", "project-a")
    second = parse_title("com.cmuxterm.app", "project-b")
    assert first is not None
    assert second is not None
    assert first.fields is not second.fields
    assert first.fields == {"workspace": "project-a"}
