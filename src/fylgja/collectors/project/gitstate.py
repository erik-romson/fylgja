"""Pure parsers and command lines for the git state of a project (``PLAN-final-plan.md`` WP5.4).

Everything here is a function over text a command already printed: no I/O and no clock. The
commands all run as ``git --no-optional-locks -C <root> …``, so looking at a repository never
takes the index lock a developer's own ``git`` may be waiting for.

* **Status** is ``git status --porcelain=v2 --branch``: the ``# branch.*`` header lines give the
  commit, the branch (``(detached)`` for a detached HEAD), the upstream and ``+ahead -behind``;
  every ``1``, ``2``, ``u`` and ``?`` line is one changed path (ignored files, ``!``, are not
  listed unless asked for). The *dirty count* is the number of those lines.
* **Last commit time** is ``git log -1 --format=%ct``: epoch seconds, UTC.
* **Failures** are short codes (:func:`classify_failure`); a path or a line of output is never
  part of one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

__all__ = [
    "GIT_FAILED",
    "GIT_MISSING",
    "GIT_NOT_A_REPOSITORY",
    "GIT_PARSE",
    "GIT_TIMEOUT",
    "GitParseError",
    "GitStatus",
    "classify_failure",
    "commit_time_argv",
    "parse_commit_time",
    "parse_status",
    "remote_argv",
    "status_argv",
]

GIT_MISSING: Final = "git_missing"
GIT_TIMEOUT: Final = "git_timeout"
GIT_NOT_A_REPOSITORY: Final = "git_not_a_repository"
GIT_FAILED: Final = "git_failed"
GIT_PARSE: Final = "git_parse"

_CHANGED_ENTRY: Final = ("1 ", "2 ", "u ", "? ")
_DETACHED: Final = "(detached)"
_INITIAL: Final = "(initial)"


class GitParseError(ValueError):
    """The output is not what ``git status --porcelain=v2 --branch`` prints."""

    kind = GIT_PARSE


@dataclass(frozen=True)
class GitStatus:
    """What ``git status`` says about one working tree."""

    oid: str | None
    """The commit HEAD is at; ``None`` before the first commit."""
    branch: str | None
    """The branch name; ``None`` for a detached HEAD."""
    upstream: str | None
    ahead: int | None
    """Commits the branch has that its upstream lacks; ``None`` without an upstream."""
    behind: int | None
    dirty_count: int
    """Changed, staged, conflicted and untracked paths."""

    @property
    def dirty(self) -> bool:
        return self.dirty_count > 0


def _base(root: str, git: str) -> tuple[str, ...]:
    return (git, "--no-optional-locks", "-C", root)


def status_argv(root: str, git: str = "git") -> tuple[str, ...]:
    return (*_base(root, git), "status", "--porcelain=v2", "--branch", "--untracked-files=normal")


def commit_time_argv(root: str, git: str = "git") -> tuple[str, ...]:
    return (*_base(root, git), "log", "-1", "--format=%ct", "HEAD")


def remote_argv(root: str, git: str = "git") -> tuple[str, ...]:
    return (*_base(root, git), "config", "--get", "remote.origin.url")


def _count(token: str, sign: str) -> int:
    if not token.startswith(sign):
        raise GitParseError("branch.ab")
    try:
        value = int(token[1:])
    except ValueError as exc:
        raise GitParseError("branch.ab") from exc
    if value < 0:
        raise GitParseError("branch.ab")
    return value


def parse_status(text: str) -> GitStatus:
    """Parse ``git status --porcelain=v2 --branch``. Raises :class:`GitParseError`."""
    oid: str | None = None
    branch: str | None = None
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    dirty = 0
    seen_head = False
    for line in text.splitlines():
        if line.startswith("# branch.oid "):
            value = line[len("# branch.oid ") :].strip()
            oid = None if value in {_INITIAL, ""} else value
        elif line.startswith("# branch.head "):
            value = line[len("# branch.head ") :].strip()
            branch = None if value in {_DETACHED, ""} else value
            seen_head = True
        elif line.startswith("# branch.upstream "):
            upstream = line[len("# branch.upstream ") :].strip() or None
        elif line.startswith("# branch.ab "):
            parts = line[len("# branch.ab ") :].split()
            if len(parts) != 2:
                raise GitParseError("branch.ab")
            ahead, behind = _count(parts[0], "+"), _count(parts[1], "-")
        elif line.startswith(_CHANGED_ENTRY):
            dirty += 1
    if not seen_head:
        raise GitParseError("branch.head")
    return GitStatus(
        oid=oid, branch=branch, upstream=upstream, ahead=ahead, behind=behind, dirty_count=dirty
    )


def parse_commit_time(text: str) -> datetime | None:
    """``git log -1 --format=%ct`` as a UTC time; empty output (no commit) is ``None``."""
    value = text.strip()
    if not value:
        return None
    try:
        seconds = int(value)
    except ValueError as exc:
        raise GitParseError("commit_time") from exc
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise GitParseError("commit_time") from exc


def classify_failure(stderr: str) -> str:
    """The short code of a ``git`` command that exited non-zero."""
    if "not a git repository" in stderr.lower():
        return GIT_NOT_A_REPOSITORY
    return GIT_FAILED
