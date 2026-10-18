"""The workspace journal's pure core (docs/decisions/0057).

This module loads no framework and does no I/O. It holds the thread identity of J1: a thread is
one checkout plus its current branch.
"""

from __future__ import annotations

from typing import NamedTuple

from fylgja.core.items import item_id
from fylgja.store.rows import ProjectRow

__all__ = ["ThreadKey", "item_seq_id", "thread_id", "thread_key"]


class ThreadKey(NamedTuple):
    """The identity of a thread: the checkout's root and its current branch."""

    repo_root: str
    branch: str


def thread_key(project: ProjectRow, branch: str | None) -> ThreadKey | None:
    """The thread of a checkout on ``branch`` (the tracker's current branch), or ``None``.

    A checkout is a project whose ``repo_root`` equals its ``path``. There is no thread for a
    path that is not a checkout (no repository, or a subfolder), nor while ``branch`` is ``None``
    (a detached HEAD, or no branch seen yet).
    """
    if project.repo_root is None or project.repo_root != project.path:
        return None
    if not branch:
        return None
    return ThreadKey(project.repo_root, branch)


def thread_id(key: ThreadKey) -> str:
    """The stable 16-hex id of a thread."""
    return item_id(f"thread\0{key.repo_root}\0{key.branch}")


def item_seq_id(thread: str, seq: int) -> str:
    """The id of the ``seq``-th item of a thread."""
    return item_id(f"journal-item\0{thread}\0{seq}")
