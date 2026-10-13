"""The plain item types and helpers (``docs/decisions/0051``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fylgja.core.items import (
    MAX_PLACES,
    GoTarget,
    Place,
    choose_go,
    folder_name,
    item_id,
    order_places,
    role_of_kinds,
    ticket_keys,
)

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def place(
    ref: str,
    *,
    kind: str = "window",
    open: bool = True,
    waiting: bool = False,
    minutes: int | None = None,
    role: str = "app",
) -> Place:
    return Place(
        kind=kind,  # type: ignore[arg-type]
        ref=ref,
        role=role,  # type: ignore[arg-type]
        label="App",
        title=None,
        workspace=None,
        open=open,
        waiting=waiting,
        last_focus_at=None if minutes is None else T0 - timedelta(minutes=minutes),
    )


def test_ticket_keys_match_only_the_given_prefixes() -> None:
    assert ticket_keys(["UTF-8 and ISO-8601 are not tickets"], ["CBS"]) == ()
    assert ticket_keys(["CBS-12 fixes CBS-7"], ["CBS"]) == ("CBS-12", "CBS-7")


def test_ticket_keys_are_upper_cased_deduplicated_and_in_first_seen_order() -> None:
    texts = ["fix cbs-12", "abc-3 then CBS-12 and CBS-4"]
    assert ticket_keys(texts, ["CBS", "ABC"]) == ("CBS-12", "ABC-3", "CBS-4")


def test_ticket_keys_need_word_boundaries() -> None:
    assert ticket_keys(["XCBS-12 and CBS-12X"], ["CBS"]) == ()


def test_an_empty_prefix_list_matches_nothing() -> None:
    assert ticket_keys(["CBS-12"], []) == ()


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("/work/cbslink/CBS-512", "cbslink/CBS-512"),
        ("/work/alpha/main", "alpha/main"),
        ("/work", "work"),
        ("/work/alpha/main/", "alpha/main"),
        ("/", "/"),
    ],
)
def test_folder_name(path: str, name: str) -> None:
    assert folder_name(path) == name


def test_choose_go_prefers_a_waiting_session() -> None:
    places = [
        place("w1", minutes=1),
        place("s1", kind="session", waiting=True, minutes=60),
        place("s2", kind="session", minutes=0),
    ]
    assert choose_go(places) == GoTarget(kind="session", ref="s1")


def test_choose_go_then_takes_the_most_recently_focused_open_place() -> None:
    places = [place("w1", minutes=30), place("w2", minutes=5), place("w3", open=False, minutes=0)]
    assert choose_go(places) == GoTarget(kind="window", ref="w2")


def test_choose_go_falls_back_to_the_ref_when_nothing_was_focused() -> None:
    assert choose_go([place("w2"), place("w1")]) == GoTarget(kind="window", ref="w1")


def test_choose_go_is_none_without_an_open_place() -> None:
    assert choose_go([place("w1", open=False, minutes=1)]) is None
    assert choose_go([]) is None


def test_a_waiting_window_does_not_beat_a_recent_place() -> None:
    places = [place("w1", waiting=True, minutes=60), place("w2", minutes=1)]
    assert choose_go(places) == GoTarget(kind="window", ref="w2")


def test_order_places_puts_open_first_then_recent_then_ref() -> None:
    places = [
        place("c", open=False, minutes=1),
        place("b", minutes=10),
        place("a", minutes=10),
        place("d", minutes=2),
    ]
    assert [p.ref for p in order_places(places)] == ["d", "a", "b", "c"]


def test_order_places_keeps_at_most_eight() -> None:
    places = [place(f"w{n:02d}", open=n < 3, minutes=n) for n in range(20)]
    ordered = order_places(places)
    assert len(ordered) == MAX_PLACES
    assert [p.ref for p in ordered[:3]] == ["w00", "w01", "w02"]


def test_item_id_is_sixteen_hex_digits_and_carries_no_path() -> None:
    value = item_id("folder:/work/alpha/main")
    assert len(value) == 16
    assert int(value, 16) >= 0
    assert "alpha" not in value
    assert item_id("folder:/work/alpha/main") == value
    assert item_id("folder:/work/beta/main") != value


def test_roles_come_from_context_kinds() -> None:
    assert role_of_kinds(["ide_project"]) == "ide"
    assert role_of_kinds(["cmux_tab"]) == "terminal"
    assert role_of_kinds(["terminal_session", "note"]) == "terminal"
    assert role_of_kinds(["ide_project", "terminal_session"]) == "ide"
    assert role_of_kinds(["note"]) == "app"
    assert role_of_kinds([]) == "app"
