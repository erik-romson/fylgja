"""The journal service (docs/decisions/0057). This module loads no framework.

It grows over the journal steps; for now it reads the commit subjects of a window.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from fylgja.collectors.seams import ProcessRunner
from fylgja.core.journal import COMMIT_HORIZON, CommitPart, in_window, log_argv, parse_log

__all__ = ["GIT_TIMEOUT", "read_commits"]

log = logging.getLogger("fylgja.journal")

GIT_TIMEOUT = 5.0
"""Seconds ``git log`` may take."""


async def read_commits(
    runner: ProcessRunner,
    git: str,
    root: str,
    branch: str,
    since: datetime,
    as_of: datetime,
    redact: Callable[[str], str],
) -> list[CommitPart]:
    """The commits of ``branch`` in ``(since, as_of]``, never from before ``as_of`` minus
    :data:`~fylgja.core.journal.COMMIT_HORIZON` (J8 section 3, D3).

    A repo with no commits, a missing or slow ``git`` is no commits, not a failure.
    """
    since = max(since, as_of - COMMIT_HORIZON)
    try:
        result = await runner.run(log_argv(git, root, branch, since), timeout=GIT_TIMEOUT)
    except (TimeoutError, OSError) as error:  # FileNotFoundError is an OSError
        log.debug("reading commit subjects failed: %s", type(error).__name__)
        return []
    if result.returncode != 0:
        log.debug("reading commit subjects failed: exit %d", result.returncode)
        return []
    stdout = result.stdout.decode("utf-8", errors="replace")
    return in_window(parse_log(stdout, redact), since, as_of)
