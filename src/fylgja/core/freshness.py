"""Freshness of a ``/now`` section (``PLAN-final-plan.md`` §4.7).

Every section that a collector polls carries ``observed_at`` and a freshness: ``fresh`` within
twice the source's poll interval, ``stale`` after that, and ``unavailable`` (together with an
``error_kind``) when the last attempt failed. A section with no data yet is absent, so it never
gets here.

Pure: the caller supplies the times.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

__all__ = ["FRESH_FACTOR", "FreshnessState", "freshness"]

FRESH_FACTOR: Final = 2.0
"""A section is fresh for this many poll intervals."""

type FreshnessState = Literal["fresh", "stale", "unavailable"]


def freshness(
    observed_at: datetime,
    now: datetime,
    poll_interval_seconds: float,
    error_kind: str | None = None,
) -> FreshnessState:
    """The freshness of a section observed at ``observed_at``, judged at ``now``.

    A failed last attempt (``error_kind``) is ``unavailable`` however recent it is. Otherwise the
    section is ``fresh`` while its age is at most twice the poll interval (an observation from
    the future, through clock skew, counts as age zero) and ``stale`` after that.
    """
    if error_kind:
        return "unavailable"
    age = (now - observed_at).total_seconds()
    return "fresh" if age <= FRESH_FACTOR * poll_interval_seconds else "stale"
