"""Deterministic inferred workspace labels (WP1.8; ``PLAN.md`` §7): each rule and the fallback."""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from api_support import workspace_snapshot

from fylgja.core.labels import (
    EMPTY_LABEL,
    MAX_LABEL,
    MAX_MANUAL_LABEL,
    LabelInput,
    LabelService,
    LabelValidationError,
    WindowFacts,
    WorkspaceNotFound,
    effective_label,
    infer_label,
    project_name,
    title_keywords,
)
from fylgja.core.live_view import LiveView
from fylgja.core.writer import Writer
from fylgja.privacy import PrivacyFilter
from fylgja.testing.fakes import FakeClock

# --- project_name -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "repo", "group", "expected"),
    [
        ("/work/fylgja", None, None, "fylgja"),
        ("/work/fylgja/main", None, None, "fylgja"),
        ("/work/fylgja/master", None, None, "fylgja"),
        ("/work/fylgja/main", "fylgja-repo", None, "fylgja-repo"),
        ("/work/draupnir/feature-x", "draupnir", "draupnir-group", "draupnir-group"),
        ("/work/draupnir/feature-x", None, "draupnir", "draupnir"),
        ("/main", None, None, "main"),
        ("/", None, None, ""),
        ("", None, None, ""),
        ("/work/Feature-Review/", None, None, "Feature-Review"),
    ],
)
def test_project_name(path: str, repo: str | None, group: str | None, expected: str) -> None:
    assert project_name(path, repo_name=repo, group_name=group) == expected


# --- rule 1: the dominant project -----------------------------------------------------------


def test_the_dominant_project_names_the_workspace() -> None:
    label = infer_label(LabelInput(projects=["fylgja", "norn", "fylgja"]))
    assert (label.text, label.rule) == ("fylgja", "project")


def test_a_project_beats_the_app_rule() -> None:
    windows = [WindowFacts("Safari", "docs")]
    label = infer_label(LabelInput(projects=["fylgja"], windows=windows))
    assert label.rule == "project"


def test_a_project_tie_goes_to_the_name_that_sorts_first_ignoring_case() -> None:
    label = infer_label(LabelInput(projects=["norn", "Fylgja", "norn", "Fylgja"]))
    assert label.text == "Fylgja"


@pytest.mark.parametrize("order", list(itertools.permutations(["b", "a", "c"])))
def test_the_project_label_does_not_depend_on_order(order: tuple[str, ...]) -> None:
    assert infer_label(LabelInput(projects=order)).text == "a"


def test_an_empty_project_name_is_ignored() -> None:
    label = infer_label(LabelInput(projects=["", "norn"]))
    assert label.text == "norn"


def test_a_long_project_name_is_cut() -> None:
    label = infer_label(LabelInput(projects=["x" * 100]))
    assert len(label.text) == MAX_LABEL
    assert label.text.endswith("…")


# --- rule 2: the dominant app plus a title keyword -------------------------------------------


def test_the_dominant_app_and_the_shared_title_keyword() -> None:
    windows = [
        WindowFacts("Safari", "Kubernetes docs - Safari"),
        WindowFacts("Safari", "Kubernetes pods"),
        WindowFacts("Safari", "Other thing"),
        WindowFacts("Slack", "general"),
    ]
    label = infer_label(LabelInput(windows=windows))
    assert (label.text, label.rule) == ("Safari Kubernetes", "app-title")


def test_the_dominant_app_is_the_one_with_most_windows() -> None:
    windows = [
        WindowFacts("Slack", "general"),
        WindowFacts("Mail", "inbox"),
        WindowFacts("Mail", ""),
    ]
    assert infer_label(LabelInput(windows=windows)).text == "Mail inbox"


def test_an_app_tie_goes_to_the_name_that_sorts_first() -> None:
    windows = [WindowFacts("Slack", "chat"), WindowFacts("Mail", "inbox")]
    assert infer_label(LabelInput(windows=windows)).text == "Mail inbox"


def test_the_keyword_tie_prefers_the_longer_then_the_earlier_word() -> None:
    windows = [WindowFacts("Notes", "alpha beta"), WindowFacts("Notes", "alpha beta")]
    assert infer_label(LabelInput(windows=windows)).text == "Notes alpha"
    windows = [WindowFacts("Notes", "ab cde"), WindowFacts("Notes", "cde ab")]
    assert infer_label(LabelInput(windows=windows)).text == "Notes cde"


def test_only_the_dominant_apps_titles_give_the_keyword() -> None:
    windows = [
        WindowFacts("Safari", "alpha"),
        WindowFacts("Safari", "alpha"),
        WindowFacts("Slack", "beta"),
    ]
    assert infer_label(LabelInput(windows=windows)).text == "Safari alpha"


def test_the_app_name_is_not_its_own_keyword() -> None:
    label = infer_label(LabelInput(windows=[WindowFacts("Safari", "Safari docs")]))
    assert label.text == "Safari docs"


def test_an_app_with_no_usable_title_word_is_named_alone() -> None:
    windows = [WindowFacts("Finder", ""), WindowFacts("Finder", "12 of 34")]
    label = infer_label(LabelInput(windows=windows))
    assert (label.text, label.rule) == ("Finder", "app")


def test_keyword_comparison_ignores_case_and_keeps_the_first_spelling() -> None:
    windows = [WindowFacts("Notes", "Plan"), WindowFacts("Notes", "plan")]
    assert infer_label(LabelInput(windows=windows)).text == "Notes Plan"


def test_a_long_app_label_is_cut() -> None:
    label = infer_label(LabelInput(windows=[WindowFacts("A" * 60, "word")]))
    assert len(label.text) == MAX_LABEL


# --- rule 3: the fallback ---------------------------------------------------------------------


def test_an_empty_workspace_is_a_dash() -> None:
    label = infer_label(LabelInput())
    assert (label.text, label.rule) == (EMPTY_LABEL, "none") == ("—", "none")


def test_windows_without_an_app_name_give_the_dash() -> None:
    label = infer_label(LabelInput(windows=[WindowFacts("", "title")]))
    assert label.text == EMPTY_LABEL


# --- title_keywords -------------------------------------------------------------------------


def test_title_keywords_drop_short_generic_numeric_and_repeated_words() -> None:
    words = title_keywords("The new Tab: fylgja-core v2 fylgja-core 2026 README.md - zsh")
    assert words == ["fylgja-core", "README"]


def test_title_keywords_ignore_the_given_words() -> None:
    assert title_keywords("Safari docs", ignore=["safari"]) == ["docs"]


# --- effective_label ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("manual", "inferred", "expected"),
    [
        ("Billing", "fylgja", ("Billing", "manual")),
        ("Billing", None, ("Billing", "manual")),
        (None, "fylgja", ("fylgja", "inferred")),
        ("  ", "fylgja", ("fylgja", "inferred")),
        (None, EMPTY_LABEL, (EMPTY_LABEL, "none")),
        (None, "", (EMPTY_LABEL, "none")),
        (None, None, (EMPTY_LABEL, "none")),
    ],
)
def test_effective_label(
    manual: str | None, inferred: str | None, expected: tuple[str, str]
) -> None:
    assert effective_label(manual, inferred) == expected


# --- manual labels: what the service refuses (WP4.2) ---------------------------------------


_T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
async def labels(isolated_env: Path) -> AsyncIterator[tuple[LabelService, Writer]]:
    clock = FakeClock(_T0)
    writer = Writer(isolated_env / "labels.db", clock=clock, on_commit=LiveView().apply)
    await writer.start()
    await writer.submit_committed(workspace_snapshot("wm", ["1"], _T0))
    try:
        yield (
            LabelService(writer, clock=clock, privacy=PrivacyFilter(redact_patterns=["sk-x+"])),
            writer,
        )
    finally:
        await writer.stop()


async def test_the_service_refuses_empty_and_long_labels(
    labels: tuple[LabelService, Writer],
) -> None:
    service, _ = labels
    for bad in ("", "  \n\t ", "a" * (MAX_MANUAL_LABEL + 1)):
        with pytest.raises(LabelValidationError) as caught:
            await service.set_manual("1", bad)
        assert "aaaa" not in str(caught.value)
    assert (await service.set_manual("1", "a" * MAX_MANUAL_LABEL)).manual_label == "a" * 80


async def test_the_service_redacts_and_collapses(labels: tuple[LabelService, Writer]) -> None:
    service, _ = labels
    row = await service.set_manual("1", "  go  sk-xxxx\n now ")
    assert row.manual_label == "go [redacted] now"


async def test_the_service_names_only_open_workspaces(
    labels: tuple[LabelService, Writer],
) -> None:
    service, writer = labels
    with pytest.raises(WorkspaceNotFound):
        await service.set_manual("missing", "x")
    with pytest.raises(WorkspaceNotFound):
        await service.clear_manual("missing")
    await service.set_manual("1", "kept")
    await writer.submit_committed(workspace_snapshot("wm", [], _T0))  # the workspace closes
    with pytest.raises(WorkspaceNotFound):
        await service.set_manual("1", "again")
    with pytest.raises(WorkspaceNotFound):
        await service.clear_manual("1")
