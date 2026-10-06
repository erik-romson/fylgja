"""The §4.7 usage and token sections of ``/now`` (``PLAN-final-plan.md`` §4.7).

Pure functions over the stored rows and what the daemon remembers about their observation
(:class:`~fylgja.core.usage_view.UsageMeta`):

* :func:`build_tokens`: ``sessions[].tokens``, the token sums of a session's open activity interval.
  They are read from the transcript with the session itself, so the section is as fresh as the
  session's last observation, judged against the collector's heartbeat.
* :func:`build_usage`: ``usage``, provider windows with a used percentage and reset time
  (``usage.providers[]``) and the spend (``usage.cost``), each part with its source, observation
  time and freshness.

A provider whose last attempt failed keeps its last reading and is ``unavailable`` with the error
kind; a provider that only ever failed is listed with that and no reading. Before any observation
the section is absent. The section as a whole is as good as its best part: ``fresh`` when any part
is, else ``stale`` when any is, else ``unavailable``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from datetime import datetime
from typing import Final

from fylgja.api.now_models import Freshness, Tokens, Usage, UsageCost, UsageProvider
from fylgja.core.freshness import freshness
from fylgja.core.usage_view import UsageMeta, UsageObservation
from fylgja.store.rows import ActivityRow, UsageRow

__all__ = [
    "DEFAULT_POLL_SECONDS",
    "TOKENS_POLL_SECONDS",
    "TOKENS_SOURCE",
    "build_tokens",
    "build_usage",
]

TOKENS_SOURCE: Final = "transcript"
TOKENS_POLL_SECONDS: Final = 30.0
"""The Claude collector re-emits a session's activity at least this often (its heartbeat)."""
DEFAULT_POLL_SECONDS: Final = 300.0
"""The interval a usage row falls back to when its source did not name one."""

_RANK: Final = {Freshness.FRESH: 0, Freshness.STALE: 1, Freshness.UNAVAILABLE: 2}


def build_tokens(
    activity: ActivityRow | None, last_seen: datetime | None, now: datetime
) -> Tokens | None:
    """The token sums of a session, or ``None`` before any transcript line carried usage."""
    if activity is None or last_seen is None:
        return None
    input_, output, cache_read, cache_write = (
        activity.tokens_input,
        activity.tokens_output,
        activity.tokens_cache_read,
        activity.tokens_cache_write,
    )
    if input_ is None or output is None or cache_read is None or cache_write is None:
        return None
    return Tokens(
        input=input_,
        output=output,
        cache_read=cache_read,
        cache_write=cache_write,
        source=TOKENS_SOURCE,
        observed_at=last_seen,
        freshness=Freshness(freshness(last_seen, now, TOKENS_POLL_SECONDS)),
    )


class _Judged:
    """A row with its observation: when, how fresh, and why not."""

    __slots__ = ("error_kind", "freshness", "observed_at", "source")

    def __init__(
        self,
        source: str,
        observed_at: datetime,
        fresh: Freshness,
        error_kind: str | None,
    ) -> None:
        self.source = source
        self.observed_at = observed_at
        self.freshness = fresh
        self.error_kind = error_kind


def _judge(row: UsageRow, kind: str, meta: UsageMeta, now: datetime) -> _Judged:
    seen: UsageObservation | None = meta.readings.get(row.key)
    observed_at = row.valid_from if seen is None else seen.observed_at
    interval = DEFAULT_POLL_SECONDS if seen is None else (seen.poll_interval_s or 0.0)
    if interval <= 0:
        interval = DEFAULT_POLL_SECONDS
    failure = meta.failures.get((row.collector, row.provider, kind))
    error_kind = None
    if failure is not None and failure.observed_at >= observed_at:
        observed_at, error_kind = failure.observed_at, failure.error_kind
    return _Judged(
        row.source,
        observed_at,
        Freshness(freshness(observed_at, now, interval, error_kind)),
        error_kind,
    )


def _best(parts: Iterable[_Judged]) -> _Judged:
    """The part that makes the section: the freshest, the most recent among equals."""
    return min(parts, key=lambda p: (_RANK[p.freshness], -p.observed_at.timestamp()))


def _windowed(row: UsageRow) -> bool:
    return bool(row.window) or row.used_pct is not None or row.resets_at is not None


def build_usage(
    rows: Mapping[str, UsageRow], meta: UsageMeta, available: Collection[str], now: datetime
) -> Usage | None:
    """``usage`` of ``/now``, or ``None`` while there is nothing to show (no rows, no failures).

    Only rows and failures of ``available`` collectors count.
    """
    shown = [row for key, row in sorted(rows.items()) if row.collector in available]
    providers: list[UsageProvider] = []
    judged: list[_Judged] = []
    reading_providers: set[tuple[str, str]] = set()
    for row in shown:
        if not _windowed(row):
            continue
        part = _judge(row, "usage", meta, now)
        judged.append(part)
        reading_providers.add((row.collector, row.provider))
        providers.append(
            UsageProvider(
                provider=row.provider,
                window=row.window or None,
                used_pct=row.used_pct,
                resets_at=row.resets_at,
                source=part.source,
                observed_at=part.observed_at,
                freshness=part.freshness,
                error_kind=part.error_kind,
            )
        )
    for (collector, provider, kind), failure in sorted(meta.failures.items()):
        if (
            kind != "usage"
            or collector not in available
            or (collector, provider) in reading_providers
        ):
            continue
        part = _Judged(
            failure.source, failure.observed_at, Freshness.UNAVAILABLE, failure.error_kind
        )
        judged.append(part)
        providers.append(
            UsageProvider(
                provider=provider,
                source=part.source,
                observed_at=part.observed_at,
                freshness=part.freshness,
                error_kind=part.error_kind,
            )
        )
    spend = _cost([row for row in shown if row.cost_usd is not None], meta, now)
    cost = None
    if spend is not None:
        cost, part = spend
        judged.append(part)
    if not judged:
        return None
    best = _best(judged)
    return Usage(
        providers=providers,
        cost=cost,
        source=",".join(sorted({part.source for part in judged if part.source})) or best.source,
        observed_at=best.observed_at,
        freshness=best.freshness,
        error_kind=best.error_kind if best.freshness is Freshness.UNAVAILABLE else None,
    )


def _cost(rows: list[UsageRow], meta: UsageMeta, now: datetime) -> tuple[UsageCost, _Judged] | None:
    """The spend: the sum of the cost rows of the first period (they are all one period in
    practice), judged by its best row."""
    if not rows:
        return None
    period = min((row.cost_period or "") for row in rows)
    chosen = [row for row in rows if (row.cost_period or "") == period]
    parts = [_judge(row, "cost", meta, now) for row in chosen]
    best = _best(parts)
    source = ",".join(sorted({part.source for part in parts}))
    error_kind = best.error_kind if best.freshness is Freshness.UNAVAILABLE else None
    cost = UsageCost(
        period=period or None,
        usd=round(sum(row.cost_usd or 0.0 for row in chosen), 6),
        source=source,
        observed_at=best.observed_at,
        freshness=best.freshness,
        error_kind=error_kind,
    )
    return cost, _Judged(source, best.observed_at, best.freshness, error_kind)
