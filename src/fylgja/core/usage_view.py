"""What the daemon remembers about each usage observation (``PLAN-final-plan.md`` §4.7).

``usage_snapshots`` stores provider windows and cost as **change intervals** (A20): a row whose
reading did not change is not rewritten, so its ``valid_from`` says when the reading *changed* and
nothing about when it was last looked at. ``/now`` needs the latter (``observed_at`` and
``freshness``), together with the poll interval of the source and the reason of a failed attempt,
which have no column. :class:`UsageMemory` keeps them in memory, fed after the ingest gate by every
usage record, like :class:`~fylgja.core.context_items.ContextMemory` for context items:

* a **reading** (an item with a used percentage, a reset time or a cost) is remembered per
  ``(source, provider, window)`` with its ``observed_at`` and the poll interval its source named;
* an item with an ``error_kind`` is a **failed attempt**. It is remembered per
  ``(source, provider, kind)`` (``usage`` for a quota attempt, ``cost`` for a cost attempt) and
  cleared by the next reading of the same provider and kind.

After a restart the memory is empty until the collectors observe again; ``/now`` then falls back to
the stored row's ``valid_from``, which can only make a section look older than it is.

Bounded: at most :data:`MAX_ENTRIES` readings and failures are kept, the oldest forgotten first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

from fylgja.models import Record, UsageEvent, UsagePayload, UsageSnapshot

__all__ = ["MAX_ENTRIES", "UsageMemory", "UsageMeta", "UsageObservation", "error_kind_of"]

MAX_ENTRIES: Final = 256

type AttemptKind = Literal["usage", "cost"]


@dataclass(frozen=True)
class UsageObservation:
    """One look at a provider: a reading or a failed attempt."""

    observed_at: datetime
    source: str
    """The source that was asked (``statusline``, ``codexbar``, ...)."""
    poll_interval_s: float | None
    error_kind: str | None = None


@dataclass(frozen=True)
class UsageMeta:
    """What ``/now`` joins with the stored usage rows."""

    readings: dict[str, UsageObservation] = field(default_factory=dict)
    """By the row key ``<collector>:<provider>:<window>``."""
    failures: dict[tuple[str, str, str], UsageObservation] = field(default_factory=dict)
    """By ``(collector, provider, kind)``; ``kind`` is ``usage`` or ``cost``."""


def error_kind_of(payload: UsagePayload) -> AttemptKind:
    """Which attempt an item reports on: a cost attempt names its period, a quota attempt does
    not."""
    return "cost" if payload.cost_period else "usage"


class UsageMemory:
    """Readings and failed attempts by provider, from the records themselves."""

    def __init__(self) -> None:
        self._readings: dict[str, UsageObservation] = {}
        self._failures: dict[tuple[str, str, str], UsageObservation] = {}

    def observe(self, record: Record) -> None:
        if isinstance(record, UsageSnapshot):
            for item in record.items:
                self._item(record.source, record.observed_at, item.payload)
        elif isinstance(record, UsageEvent):
            self._item(record.source, record.observed_at, record.payload)

    def _item(self, collector: str, observed_at: datetime, payload: UsagePayload) -> None:
        source = payload.usage_source or collector
        observation = UsageObservation(
            observed_at=observed_at,
            source=source,
            poll_interval_s=payload.poll_interval_s,
            error_kind=payload.error_kind,
        )
        if payload.error_kind is not None:
            slot = (collector, payload.provider, error_kind_of(payload))
            known = self._failures.get(slot)
            if known is None or known.observed_at <= observed_at:
                self._failures[slot] = observation
            self._trim(self._failures)
            return
        key = f"{collector}:{payload.provider}:{payload.window or ''}"
        seen = self._readings.get(key)
        if seen is None or seen.observed_at <= observed_at:
            self._readings[key] = observation
        self._trim(self._readings)
        kind = "cost" if payload.cost_usd is not None else "usage"
        failed = self._failures.get((collector, payload.provider, kind))
        if failed is not None and failed.observed_at <= observed_at:
            del self._failures[(collector, payload.provider, kind)]  # the provider answers again

    @staticmethod
    def _trim[K](table: dict[K, UsageObservation]) -> None:
        while len(table) > MAX_ENTRIES:
            del table[next(iter(table))]

    def meta(self) -> UsageMeta:
        return UsageMeta(readings=dict(self._readings), failures=dict(self._failures))
