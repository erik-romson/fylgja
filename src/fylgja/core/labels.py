"""Deterministic inferred workspace labels (``PLAN-final-plan.md`` WP1.8; ``PLAN.md`` §7).

A workspace without a manual label is named by what is in it, by the first rule that applies:

1. **project**: the dominant project or repository name among the live sessions placed there (the
   most sessions; a tie goes to the name that sorts first, ignoring case);
2. **app-title**: the dominant app by window count plus the keyword that most of that app's window
   titles share (``Safari docs``). An app whose titles give no keyword is named alone;
3. **none**: ``—``.

The same input always gives the same label, so it never flickers. **Manual labels** (WP4.2) always
win: :class:`LabelService` stores one in ``workspaces.manual_label`` (with ``manual_label_at``)
through the writer, and clearing it restores inference. Inference keeps updating
``workspaces.inferred_label`` underneath a manual label and never touches it.
:func:`effective_label` picks the label ``/now`` shows (and the workspace one-liner of §4.6
uses): the manual label, else the inferred one, else ``—``; ``label_source`` says which.

Titles are redacted before they reach this module, and nothing here logs them.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from fylgja.collectors.base import Clock
from fylgja.core.live_view import Change, Collection
from fylgja.core.writer import Tx, Writer
from fylgja.privacy import PrivacyFilter
from fylgja.store import db, queries
from fylgja.store.rows import WorkspaceRow

__all__ = [
    "EMPTY_LABEL",
    "GENERIC_DIRECTORIES",
    "LABEL_INFERRED",
    "LABEL_MANUAL",
    "LABEL_NONE",
    "MAX_LABEL",
    "MAX_MANUAL_LABEL",
    "InferredLabel",
    "LabelError",
    "LabelInput",
    "LabelService",
    "LabelValidationError",
    "WorkspaceNotFound",
    "WindowFacts",
    "effective_label",
    "infer_label",
    "project_name",
    "title_keywords",
]

EMPTY_LABEL: Final = "—"
MAX_LABEL: Final = 40
MAX_MANUAL_LABEL: Final = 80
"""The longest manual label (after whitespace is collapsed)."""

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
    """The project name of each live session placed in the workspace, or, with none, of each
    window whose context names a folder (see :func:`project_name`)."""
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


# --- manual labels (WP4.2) ------------------------------------------------------------------------


class LabelError(Exception):
    """Base of what the label service refuses."""


class WorkspaceNotFound(LabelError):
    """No open workspace has that name."""


class LabelValidationError(LabelError):
    """The label is empty or too long (the message never repeats it)."""


def _clean_label(text: str, privacy: PrivacyFilter) -> str:
    cleaned = " ".join(text.split())  # one line: no control characters, no stray whitespace
    if not cleaned:
        raise LabelValidationError("the label is empty")
    if len(cleaned) > MAX_MANUAL_LABEL:
        raise LabelValidationError(f"the label is longer than {MAX_MANUAL_LABEL} characters")
    return privacy.redact(cleaned)


class LabelService:
    """Sets and clears the manual label of a workspace; every change runs in the writer."""

    def __init__(
        self, writer: Writer, *, clock: Clock, privacy: PrivacyFilter | None = None
    ) -> None:
        self._writer = writer
        self._clock = clock
        self._privacy = PrivacyFilter() if privacy is None else privacy

    async def set_manual(self, workspace: str, label: str) -> WorkspaceRow:
        """Name ``workspace`` by hand. The label is redacted (A5) and never logged."""
        cleaned = _clean_label(label, self._privacy)
        return await self._apply(workspace, cleaned)

    async def clear_manual(self, workspace: str) -> WorkspaceRow:
        """Remove the manual label; the inferred label shows again. Clearing none is fine."""
        return await self._apply(workspace, None)

    async def _apply(self, workspace: str, label: str | None) -> WorkspaceRow:
        stamp = db.to_db_time(self._clock.now())

        def job(tx: Tx) -> WorkspaceRow:
            current = queries.get_workspace(tx.connection, workspace)
            row = queries.set_manual_label(tx.connection, name=workspace, label=label, at=stamp)
            if row is None:
                raise WorkspaceNotFound(workspace)
            if current is None or current.manual_label != row.manual_label:
                tx.changed(Change(Collection.WORKSPACES, workspace, row))
            return row

        return await self._writer.execute(job)
