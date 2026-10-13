"""The working folder of a window, from its context items (``core/window_folders.py``).

Context items reach a :class:`ContextMemory` as the records a collector emits; the folders are
looked up in a :class:`FakeFS` whose ``.git`` directories mark the repositories.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import JsonValue

from fylgja.core.context_items import ContextEntry, ContextMemory
from fylgja.core.project_roots import ProjectLocator
from fylgja.core.window_folders import FOLDER_CONTEXT_KINDS, WindowFolders, folder_hint
from fylgja.models import ContextItemEvent, ContextItemPayload
from fylgja.testing.fakes import FakeFS

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def item(
    window_id: str,
    key: str,
    kind: str,
    fields: dict[str, JsonValue],
    *,
    tier: int = 2,
    confidence: str | None = "high",
    at: datetime = T0,
    source: str = "ctx",
) -> ContextItemEvent:
    data: dict[str, JsonValue] = {
        "window_id": window_id,
        "key": key,
        "tier": tier,
        "fields": dict(fields),
    }
    if confidence is not None:
        data["confidence"] = confidence
    return ContextItemEvent(
        source=source,
        entity_id=f"{window_id}:{key}",
        observed_at=at,
        payload=ContextItemPayload(context_kind=kind, data=data),
    )


def entry(
    key: str,
    kind: str,
    fields: dict[str, JsonValue],
    *,
    tier: int = 2,
    confidence: str | None = "high",
    at: datetime = T0,
) -> ContextEntry:
    return ContextEntry(
        window_id="w",
        key=key,
        source="ctx",
        kind=kind,
        title=None,
        data=fields,
        unread=None,
        confidence=confidence,
        link=None,
        tier=tier,
        observed_at=at,
    )


def world(*repositories: str) -> tuple[ContextMemory, WindowFolders, FakeFS]:
    fs = FakeFS()
    for root in repositories:
        fs.add_dir(f"{root}/.git")
    contexts = ContextMemory()
    return contexts, WindowFolders(contexts, ProjectLocator(fs)), fs


# --- the git root ----------------------------------------------------------------------------


def test_an_ide_project_path_maps_to_its_git_root() -> None:
    contexts, folders, _ = world("/work/alpha/main")
    contexts.observe(item("1", "jetbrains", "ide_project", {"path": "/work/alpha/main"}))
    assert folders.folder_of("1") == "/work/alpha/main"


def test_a_cmux_tab_cwd_in_a_subdirectory_maps_to_the_root() -> None:
    contexts, folders, _ = world("/work/alpha/main")
    contexts.observe(item("1", "cmux", "cmux_tab", {"cwd": "/work/alpha/main/src/pkg"}))
    assert folders.folder_of("1") == "/work/alpha/main"


def test_a_terminal_session_path_maps_to_its_git_root() -> None:
    contexts, folders, _ = world("/work/beta/main")
    contexts.observe(
        item("1", "iterm2", "terminal_session", {"path": "/work/beta/main/docs", "job": "zsh"})
    )
    assert folders.folder_of("1") == "/work/beta/main"


def test_a_note_item_with_a_path_is_ignored() -> None:
    contexts, folders, _ = world("/work/notes")
    contexts.observe(item("1", "obsidian", "note", {"path": "/work/notes", "vault": "notes"}))
    contexts.observe(item("1", "title", "notes", {"path": "/work/notes"}, tier=1))
    assert "note" not in FOLDER_CONTEXT_KINDS
    assert folders.folder_of("1") is None


def test_a_path_outside_any_repository_gives_none() -> None:
    contexts, folders, _ = world("/work/alpha/main")
    contexts.observe(item("1", "jetbrains", "ide_project", {"path": "/work/datagrip/console"}))
    assert folders.folder_of("1") is None


def test_a_window_without_context_or_unknown_gives_none() -> None:
    contexts, folders, _ = world("/work/alpha/main")
    contexts.observe(item("1", "chrome", "tab", {"url": "https://example.com/"}))
    assert folders.folder_of("1") is None
    assert folders.folder_of("never-seen") is None


def test_a_closed_window_forgets_its_folder() -> None:
    contexts, folders, _ = world("/work/alpha/main")
    contexts.observe(item("1", "jetbrains", "ide_project", {"path": "/work/alpha/main"}))
    contexts.forget_window("1")
    assert folders.folder_of("1") is None


def test_the_rule_is_the_kind_not_the_collector() -> None:
    contexts, folders, _ = world("/work/alpha/main")
    contexts.observe(
        item("1", "any", "ide_project", {"path": "/work/alpha/main"}, source="someplugin")
    )
    assert folders.folder_of("1") == "/work/alpha/main"


# --- which item wins -------------------------------------------------------------------------


def test_with_two_items_tier_two_and_higher_confidence_win() -> None:
    contexts, folders, _ = world("/work/alpha/main", "/work/beta/main")
    contexts.observe(
        item("1", "a", "ide_project", {"path": "/work/beta/main"}, tier=1, confidence="high")
    )
    contexts.observe(
        item("1", "b", "ide_project", {"path": "/work/alpha/main"}, tier=2, confidence="low")
    )
    assert folders.folder_of("1") == "/work/alpha/main"  # tier 2 before tier 1

    contexts.observe(item("2", "a", "cmux_tab", {"cwd": "/work/beta/main"}, confidence="medium"))
    contexts.observe(
        item("2", "b", "terminal_session", {"path": "/work/alpha/main"}, confidence="high")
    )
    assert folders.folder_of("2") == "/work/alpha/main"  # then high > medium


def test_the_order_is_tier_confidence_newest_then_key() -> None:
    later = T0 + timedelta(seconds=5)
    assert (
        folder_hint(
            [
                entry("a", "ide_project", {"path": "/one"}, confidence=None),
                entry("b", "ide_project", {"path": "/two"}, confidence="low"),
            ]
        )
        == "/two"
    )  # no confidence ranks below low
    assert (
        folder_hint(
            [
                entry("a", "ide_project", {"path": "/old"}),
                entry("b", "ide_project", {"path": "/new"}, at=later),
            ]
        )
        == "/new"
    )
    assert (
        folder_hint(
            [
                entry("b", "ide_project", {"path": "/second"}),
                entry("a", "ide_project", {"path": "/first"}),
            ]
        )
        == "/first"
    )
    assert folder_hint([]) is None


def test_path_is_read_before_cwd() -> None:
    assert folder_hint([entry("a", "cmux_tab", {"cwd": "/cwd", "path": "/path"})]) == "/path"
    assert folder_hint([entry("a", "cmux_tab", {"cwd": "/cwd", "path": "~/x"})]) == "/cwd"


@pytest.mark.parametrize(
    "value",
    ["~/work/alpha/main", "work/alpha/main", "./alpha", "", 42, None, ["/work/alpha/main"]],
)
def test_a_relative_tilde_or_non_text_value_is_ignored(value: JsonValue) -> None:
    contexts, folders, _ = world("/work/alpha/main")
    contexts.observe(item("1", "jetbrains", "ide_project", {"path": value}))
    assert folders.folder_of("1") is None
    assert folder_hint([entry("a", "ide_project", {"path": value})]) is None


def test_an_unusable_value_falls_through_to_the_next_item() -> None:
    contexts, folders, _ = world("/work/alpha/main")
    contexts.observe(item("1", "a", "ide_project", {"path": "~/work/beta/main"}, tier=2))
    contexts.observe(item("1", "b", "cmux_tab", {"cwd": "/work/alpha/main"}, tier=1))
    assert folders.folder_of("1") == "/work/alpha/main"


# --- the snapshot and the locator ------------------------------------------------------------


def test_the_snapshot_lists_only_the_windows_with_a_git_root() -> None:
    contexts, folders, _ = world("/work/alpha/main", "/work/beta/main")
    contexts.observe(item("1", "jetbrains", "ide_project", {"path": "/work/alpha/main"}))
    contexts.observe(item("2", "cmux", "cmux_tab", {"cwd": "/work/beta/main/src"}))
    contexts.observe(item("3", "cmux", "cmux_tab", {"cwd": "/tmp/scratch"}))
    contexts.observe(item("4", "chrome", "tab", {"url": "https://example.com/"}))
    assert folders.snapshot(["1", "2", "3", "4", "5"]) == {
        "1": "/work/alpha/main",
        "2": "/work/beta/main",
    }
    assert folders.snapshot(["2"]) == {"2": "/work/beta/main"}
    assert folders.snapshot([]) == {}


def test_a_lookup_only_checks_that_paths_exist_and_reads_no_file() -> None:
    contexts, folders, fs = world("/work/alpha/main")
    contexts.observe(item("1", "jetbrains", "ide_project", {"path": "/work/alpha/main/sub"}))
    assert folders.folder_of("1") == "/work/alpha/main"
    assert folders.folder_of("1") == "/work/alpha/main"  # the locator remembers it
    assert fs.reads == []
