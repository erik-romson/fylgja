"""The repo-root ``TODO.md`` parser (WP4.3): checkboxes, the ``next`` marker, nested lists,
non-task lines, code fences, duplicate lines and their ordinals, and the snapshot it becomes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fylgja.collectors.project import parse
from fylgja.collectors.todo_lists import split_list_entity_id
from fylgja.models import TodoPayload, TodoSnapshot

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "project" / "todo-md"
AT = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)

Expected = list[tuple[int, str, str]]
"""``(line, status, text)`` of each task, top to bottom."""

EXPECTED: dict[str, Expected] = {
    "basic.md": [
        (5, "open", "Write the parser tests"),
        (6, "done", "Set up the repository"),
        (7, "done", "Pick a licence"),
        (8, "next", "Ship the first release"),
        (9, "next", "Review the API"),
        (10, "done", "Done things stay done"),
        (11, "open", "Collapse the whitespace"),
        (12, "open", "A star bullet"),
        (13, "open", "A plus bullet"),
        (24, "open", "Nextcloud sync is not a next marker"),
        (25, "open", "tag#next is not a tag either"),
    ],
    "nested.md": [
        (3, "open", "Prepare the release"),
        (4, "done", "Update the changelog"),
        (5, "open", "Tag the commit"),
        (6, "next", "Push the tag"),
        (8, "open", "A task under a plain bullet"),
    ],
    "tabs.md": [
        (1, "open", "A tab-indented task"),
        (2, "open", "Under it, with a tab"),
    ],
    "fences.md": [
        (3, "open", "A real task"),
        (19, "open", "Another real task"),
    ],
    "duplicates.md": [
        (1, "open", "Run the tests"),
        (2, "done", "Run the tests"),
        (3, "open", "Run the tests"),
        (4, "next", "Run the tests"),
        (5, "open", "Deploy"),
        (6, "next", "Run the tests"),
    ],
    "no-tasks.md": [],
}


def _parse_file(name: str) -> list[parse.TodoMdItem]:
    return parse.parse_todo_md(parse.decode((FIXTURES / name).read_bytes()))


def test_every_fixture_has_an_expectation() -> None:
    assert sorted(p.name for p in FIXTURES.glob("*.md")) == sorted(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_the_fixture_parses_to_its_tasks(name: str) -> None:
    items = _parse_file(name)
    assert [(i.line, i.status, i.text) for i in items] == EXPECTED[name]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_keys_are_the_text_hash_plus_an_ordinal_among_identical_lines(name: str) -> None:
    items = _parse_file(name)
    seen: dict[str, int] = {}
    for item in items:
        ordinal = seen.get(item.text, 0)
        seen[item.text] = ordinal + 1
        assert item.key == parse.item_key(item.text, ordinal)
    assert len({item.key for item in items}) == len(items)


def test_the_key_format_is_stable() -> None:
    # Pinned: a changed hash would turn every stored TODO.md row into a removal plus a new row.
    assert parse.item_key("Write the parser tests", 0) == "881dc446b267f76a:0"
    assert [i.key for i in _parse_file("duplicates.md")] == [
        "cfa685e12bdaf829:0",
        "cfa685e12bdaf829:1",
        "cfa685e12bdaf829:2",
        "cfa685e12bdaf829:3",
        "4c236daafb445f86:0",
        "cfa685e12bdaf829:4",
    ]


def test_the_key_never_holds_the_text() -> None:
    secret = "rotate the sk-abcdefghijklmnopqrstuvwx key"
    (item,) = parse.parse_todo_md(f"- [ ] {secret}\n")
    assert "sk-" not in item.key
    assert "#" not in item.key


def test_a_checkbox_or_next_change_keeps_the_key_and_a_text_edit_does_not() -> None:
    (open_,) = parse.parse_todo_md("- [ ] Ship it\n")
    (done,) = parse.parse_todo_md("- [x] Ship it\n")
    (marked,) = parse.parse_todo_md("- [ ] next: Ship it\n")
    (tagged,) = parse.parse_todo_md("- [ ] Ship   it #next\n")
    (edited,) = parse.parse_todo_md("- [ ] Ship it now\n")
    assert open_.key == done.key == marked.key == tagged.key
    assert (open_.status, done.status, marked.status, tagged.status) == (
        "open",
        "done",
        "next",
        "next",
    )
    assert edited.key != open_.key


def test_moving_a_line_keeps_its_key() -> None:
    before = parse.parse_todo_md("- [ ] a\n- [ ] b\n")
    after = parse.parse_todo_md("# moved\n\n- [ ] b\n\n- [ ] a\n")
    assert {i.text: i.key for i in before} == {i.text: i.key for i in after}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("next: ship", ("ship", True)),
        ("NEXT:ship", ("ship", True)),
        ("Next:  ship it", ("ship it", True)),
        ("ship #next", ("ship", True)),
        ("#NEXT ship", ("ship", True)),
        ("next: #next  both   markers ", ("both markers", True)),
        ("nextcloud: sync", ("nextcloud: sync", False)),
        ("ship #nextjs", ("ship #nextjs", False)),
        ("a#next", ("a#next", False)),
        ("plain", ("plain", False)),
    ],
)
def test_split_next(text: str, expected: tuple[str, bool]) -> None:
    assert parse.split_next(text) == expected


def test_windows_line_endings_and_a_bom() -> None:
    data = "﻿- [ ] first\r\n- [x] second\r\n".encode()
    items = parse.parse_todo_md(parse.decode(data))
    assert [(i.text, i.status) for i in items] == [("first", "open"), ("second", "done")]


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (b"- [ ] caf\xe9\n", "todo_md_not_utf8"),
        (b"- [ ] x\x00y\n", "todo_md_binary"),
        (b"x" * (parse.MAX_BYTES + 1), "todo_md_too_large"),
    ],
)
def test_bytes_that_are_not_a_todo_file_are_refused(data: bytes, kind: str) -> None:
    with pytest.raises(parse.TodoMdError) as raised:
        parse.decode(data)
    assert raised.value.kind == kind
    assert str(raised.value) == kind  # a code, never content


def test_too_many_tasks_is_a_failed_read() -> None:
    text = "".join(f"- [ ] task {n}\n" for n in range(parse.MAX_ITEMS + 1))
    with pytest.raises(parse.TodoMdError) as raised:
        parse.parse_todo_md(text)
    assert raised.value.kind == "todo_md_too_many_items"
    assert len(parse.parse_todo_md(text.rsplit("- [ ]", 1)[0])) == parse.MAX_ITEMS


def test_an_unclosed_fence_hides_the_rest_and_a_shorter_fence_does_not_close() -> None:
    text = "- [ ] before\n````\n- [ ] hidden\n```\n- [ ] still hidden\n"
    assert [i.text for i in parse.parse_todo_md(text)] == ["before"]
    closed = "~~~ python\n- [ ] hidden\n~~~~   \n- [ ] after\n"
    assert [i.text for i in parse.parse_todo_md(closed)] == ["after"]
    mixed = "```\n- [ ] hidden\n~~~\n- [ ] hidden too\n```\n- [ ] after\n"
    assert [i.text for i in parse.parse_todo_md(mixed)] == ["after"]


def test_the_path_is_the_repo_root_todo_md() -> None:
    assert parse.todo_md_path("/work/app") == "/work/app/TODO.md"
    assert parse.todo_md_path("/work/app/") == "/work/app/TODO.md"


def test_the_snapshot_is_the_whole_file_scoped_to_the_project() -> None:
    items = _parse_file("duplicates.md")
    snapshot = parse.todo_md_snapshot("/work/app/TODO.md", "/work/app", items, AT)
    assert isinstance(snapshot, TodoSnapshot)
    assert snapshot.source == "project"
    assert snapshot.complete is True
    assert snapshot.observed_at == AT
    assert split_list_entity_id(snapshot.entity_id) == ("project-todo-md", "/work/app/TODO.md")
    assert [i.entity_id for i in snapshot.items] == [i.key for i in items]
    first = snapshot.items[0].payload
    assert first == TodoPayload(
        text="Run the tests",
        status="open",
        scope_kind="project",
        scope_ref="/work/app",
        link="/work/app/TODO.md",
    )
    assert [i.payload.status for i in snapshot.items] == [i.status for i in items]


def test_an_empty_read_is_a_complete_snapshot_without_items() -> None:
    snapshot = parse.todo_md_snapshot("/work/app/TODO.md", "/work/app", [], AT)
    assert snapshot.complete is True
    assert snapshot.items == ()
