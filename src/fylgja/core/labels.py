"""Deterministic inferred workspace labels (``PLAN-final-plan.md`` WP1.8; ``PLAN.md`` §7).

A workspace without a manual label is named by what is in it, by the first rule that applies:

1. **project**: the dominant project or repository name among the live sessions placed there (the
   most sessions; a tie goes to the name that sorts first, ignoring case);
2. **app-title**: the dominant app by window count plus the keyword that most of that app's window
   titles share (``Safari docs``). An app whose titles give no keyword is named alone;
3. **none**: ``—``.

The same input always gives the same label, so it never flickers. Manual labels, which always win,
come with the todo and label work (step 34); this module infers, and :func:`effective_label` picks
the label ``/now`` shows. The inferred label is stored in ``workspaces.inferred_label``.

Titles are redacted before they reach this module, and nothing here logs them.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "EMPTY_LABEL",
    "GENERIC_DIRECTORIES",
    "LABEL_INFERRED",
    "LABEL_MANUAL",
    "LABEL_NONE",
    "MAX_LABEL",
    "InferredLabel",
    "LabelInput",
    "WindowFacts",
    "effective_label",
    "infer_label",
    "project_name",
    "title_keywords",
]

EMPTY_LABEL: Final = "—"
MAX_LABEL: Final = 40

LABEL_MANUAL: Final = "manual"
LABEL_INFERRED: Final = "inferred"
LABEL_NONE: Final = "none"
"""The ``label_source`` values of :func:`effective_label`."""

RULE_PROJECT: Final = "project"
RULE_APP_TITLE: Final = "app-title"
RULE_APP: Final = "app"
RULE_NONE: Final = "none"

GENERIC_DIRECTORIES: Final = frozenset({"main", "master", "trunk", "develop", "dev", "src", "app"})
"""A directory with one of these names says nothing about the project: its parent does."""

_WORD: Final = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_STOPWORDS: Final = frozenset(
    {
        "and",
        "the",
        "for",
        "with",
        "new",
        "tab",
        "window",
        "untitled",
        "terminal",
        "zsh",
        "bash",
        "fish",
        "claude",
        "code",
        "main",
        "master",
        "file",
        "edit",
        "view",
        "http",
        "https",
        "www",
        "com",
    }
)


@dataclass(frozen=True)
class WindowFacts:
    """The part of a window the label looks at."""

    app_name: str
    title: str = ""


@dataclass(frozen=True)
class LabelInput:
    """What is in one workspace."""

    projects: Sequence[str] = ()
    """The project name of each live session placed in the workspace (see :func:`project_name`)."""
    windows: Sequence[WindowFacts] = ()


@dataclass(frozen=True)
class InferredLabel:
    text: str
    rule: str
    """``project``, ``app-title``, ``app`` or ``none``."""


def project_name(path: str, *, repo_name: str | None = None, group_name: str | None = None) -> str:
    """The name a project goes by in a label.

    A draupnir clone goes by its group (the draupnir workspace directory); otherwise a known
    repository name; otherwise the directory name, or its parent's when the directory is a generic
    ``main``.
    """
    if group_name:
        return group_name
    if repo_name:
        return repo_name
    parts = [part for part in path.split("/") if part]
    if not parts:
        return ""
    if parts[-1].lower() in GENERIC_DIRECTORIES and len(parts) > 1:
        return parts[-2]
    return parts[-1]


def title_keywords(title: str, *, ignore: Iterable[str] = ()) -> list[str]:
    """The distinct words of a window title that can name a workspace, in order of appearance.

    A word has at least three characters, starts with a letter and is not a generic word or one of
    ``ignore`` (the app's own name). Case is kept; comparison ignores it.
    """
    skipped = _STOPWORDS | {word.casefold() for word in ignore}
    seen: set[str] = set()
    words: list[str] = []
    for match in _WORD.finditer(title):
        word = match[0]
        key = word.casefold()
        if key in skipped or key in seen:
            continue
        seen.add(key)
        words.append(word)
    return words


def _dominant(names: Iterable[str]) -> str | None:
    counts = Counter(name for name in names if name)
    if not counts:
        return None
    return min(counts, key=lambda name: (-counts[name], name.casefold(), name))


def _clip(text: str) -> str:
    return text if len(text) <= MAX_LABEL else text[: MAX_LABEL - 1] + "…"


def effective_label(manual: str | None, inferred: str | None) -> tuple[str, str]:
    """``(label, label_source)`` of a workspace: the manual label when set, else the inferred
    one, else ``—``. ``label_source`` is ``manual``, ``inferred`` or ``none``; an inferred ``—``
    (nothing to name the workspace by) counts as none."""
    if manual and manual.strip():
        return manual, LABEL_MANUAL
    if inferred and inferred.strip() and inferred != EMPTY_LABEL:
        return inferred, LABEL_INFERRED
    return EMPTY_LABEL, LABEL_NONE


def infer_label(inputs: LabelInput) -> InferredLabel:
    """The inferred label of one workspace."""
    project = _dominant(inputs.projects)
    if project is not None:
        return InferredLabel(_clip(project), RULE_PROJECT)
    app = _dominant(window.app_name for window in inputs.windows)
    if app is None:
        return InferredLabel(EMPTY_LABEL, RULE_NONE)
    app_words = app.split()
    counts: Counter[str] = Counter()
    spelling: dict[str, str] = {}
    for window in inputs.windows:
        if window.app_name != app:
            continue
        for word in title_keywords(window.title, ignore=app_words):
            counts[word.casefold()] += 1
            spelling.setdefault(word.casefold(), word)
    if not counts:
        return InferredLabel(_clip(app), RULE_APP)
    best = min(counts, key=lambda key: (-counts[key], -len(key), key))
    return InferredLabel(_clip(f"{app} {spelling[best]}"), RULE_APP_TITLE)
