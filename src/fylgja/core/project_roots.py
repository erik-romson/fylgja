"""Where a working directory's project is: its git root and its draupnir group (WP1.8).

The resolver is pure, so the lookup lives here, behind the
:class:`~fylgja.collectors.seams.FileSystem` seam, and the resolver gets the answers as plain
values. A lookup only checks that paths exist (``.git``, ``draupnir.toml``); it never reads a file
and never runs ``git``.

A draupnir workspace is a directory with a ``draupnir.toml`` whose children are git clones
(``main/`` and one directory per feature clone). A clone belongs to the group named after that
directory, so the clones of one project are grouped under it. A session started in the workspace
directory itself (no git root of its own) belongs to the group too.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from fylgja.collectors.seams import FileSystem

__all__ = ["DRAUPNIR_FILE", "MAX_DEPTH", "ProjectFact", "ProjectLocator"]

DRAUPNIR_FILE: Final = "draupnir.toml"
MAX_DEPTH: Final = 40
MAX_CACHE: Final = 1024


@dataclass(frozen=True)
class ProjectFact:
    """What the filesystem says about one working directory."""

    git_root: str | None = None
    """The nearest ancestor (or the directory itself) that holds ``.git``."""
    group: str | None = None
    """The draupnir workspace the project belongs to: the name of its directory."""


class ProjectLocator:
    """Finds the git root and draupnir group of a cwd, and remembers the answer."""

    def __init__(self, fs: FileSystem) -> None:
        self._fs = fs
        self._cache: dict[str, ProjectFact] = {}

    def locate(self, cwd: str) -> ProjectFact:
        cached = self._cache.get(cwd)
        if cached is not None:
            return cached
        fact = self._lookup(cwd)
        if len(self._cache) >= MAX_CACHE:
            self._cache.clear()
        self._cache[cwd] = fact
        return fact

    def forget(self) -> None:
        """Drop every remembered answer (a repository was created or removed)."""
        self._cache.clear()

    def _exists(self, path: PurePosixPath) -> bool:
        try:
            return self._fs.exists(str(path))
        except OSError:
            return False

    def _lookup(self, cwd: str) -> ProjectFact:
        start = PurePosixPath(cwd)
        if not start.is_absolute():
            return ProjectFact()
        for depth, directory in enumerate((start, *start.parents)):
            if depth >= MAX_DEPTH or directory == PurePosixPath("/"):
                break
            if self._exists(directory / ".git"):
                return ProjectFact(git_root=str(directory), group=self._group(directory.parent))
        # Not in a repository: a draupnir workspace directory still names its own group.
        return ProjectFact(group=self._group(start))

    def _group(self, directory: PurePosixPath) -> str | None:
        if directory.name and self._exists(directory / DRAUPNIR_FILE):
            return directory.name
        return None
