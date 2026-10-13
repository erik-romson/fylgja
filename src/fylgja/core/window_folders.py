"""The working folder of a window, from its context items (``docs/decisions/0049``).

A focus span is attributed to a project through the session placed in its window. An IDE or a
terminal window without such a session still names its folder: a context item of one of the
:data:`FOLDER_CONTEXT_KINDS` carries it in ``data.path`` or ``data.cwd``. This module picks that
path and maps it to its git root with the daemon's shared
:class:`~fylgja.core.project_roots.ProjectLocator`, so session, IDE and terminal time land on one
key: the git root, as a session's ``project_path`` is.

The rule is keyed on context **kinds** and ``data`` keys, never on a collector name: any plugin
that attaches an ``ide_project``, ``cmux_tab`` or ``terminal_session`` item to a window names its
folder. A ``note`` item (a vault path) is not a working folder, and a folder that is not in a git
repository gives no answer.

Only absolute paths are used; a ``~/…`` or relative value is ignored. Nothing here logs.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Final

from fylgja.core.context_items import ContextEntry, ContextMemory
from fylgja.core.project_roots import ProjectLocator

__all__ = ["FOLDER_CONTEXT_KINDS", "FOLDER_DATA_KEYS", "WindowFolders", "folder_hint"]

FOLDER_CONTEXT_KINDS: Final = frozenset({"ide_project", "cmux_tab", "terminal_session"})
"""Context kinds whose ``data.path`` or ``data.cwd`` names the window's working folder. Kinds, not
collector names."""

FOLDER_DATA_KEYS: Final = ("path", "cwd")
"""The ``data`` keys that hold the folder, in the order they are read."""

_CONFIDENCE: Final = {"high": 2, "medium": 1, "low": 0}


def _path_of(entry: ContextEntry) -> str | None:
    """The first absolute path among the entry's folder keys."""
    for name in FOLDER_DATA_KEYS:
        value = entry.data.get(name)
        if isinstance(value, str) and value.startswith("/"):
            return value
    return None


def _order(entry: ContextEntry) -> tuple[int, int, float, str]:
    """Tier 2 before tier 1, then high > medium > low (no confidence last), then the newest
    observation, then the key."""
    return (
        -entry.tier,
        -_CONFIDENCE.get(entry.confidence or "", -1),
        -entry.observed_at.timestamp(),
        entry.key,
    )


def folder_hint(entries: Sequence[ContextEntry]) -> str | None:
    """The absolute path the window's context names: tier 2 before tier 1, then confidence
    high > medium > low, then the newest ``observed_at``, then ``key``.

    Only entries of a :data:`FOLDER_CONTEXT_KINDS` kind count, and only an absolute ``str`` value
    of ``data.path`` or ``data.cwd``. ``None`` when no entry names one."""
    candidates = [
        (entry, path)
        for entry in entries
        if entry.kind in FOLDER_CONTEXT_KINDS and (path := _path_of(entry)) is not None
    ]
    if not candidates:
        return None
    _, path = min(candidates, key=lambda candidate: _order(candidate[0]))
    return path


class WindowFolders:
    """The git root of each window's working folder, read from the live context items."""

    def __init__(self, contexts: ContextMemory, locator: ProjectLocator) -> None:
        self._contexts = contexts
        self._locator = locator

    def _root(self, entries: Sequence[ContextEntry]) -> str | None:
        hint = folder_hint(entries)
        return None if hint is None else self._locator.locate(hint).git_root

    def folder_of(self, window_id: str) -> str | None:
        """The git root of the window's hinted path; ``None`` outside a repository."""
        entries = self._contexts.by_window((window_id,)).get(window_id, ())
        return self._root(entries)

    def snapshot(self, window_ids: Iterable[str]) -> dict[str, str]:
        """The git root of every listed window that has one."""
        found: dict[str, str] = {}
        for window_id, entries in self._contexts.by_window(window_ids).items():
            root = self._root(entries)
            if root is not None:
                found[window_id] = root
        return found
