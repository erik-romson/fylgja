"""The ``fylgja.inspect.*`` contracts: the models behind the ``GET /inspect/*`` read routes
(``PLAN-final-plan.md`` §4.2, ``docs/api/inspect.md``, ``docs/decisions/0048``).

An inspect answer is diagnostic: it shows how collection works right now (state, redacted
settings, what came in per record kind and when), read from the daemon's memory only. Each route
has its own schema string, ``fylgja.inspect.<name>/1``. ``/now`` (``fylgja.now/1``) and
``/collectors`` stay as they are.

The serialization rule of ``fylgja.now/1`` holds here too: every model is a
:class:`~fylgja.api.now_models.NowModel`, dumped with :func:`~fylgja.api.now_models.dump_json`, so a
field without a value is absent and ``null`` never appears.

This module imports only the standard library, Pydantic, :mod:`fylgja.models` and
:mod:`fylgja.api.now_models`.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import AwareDatetime, Field, JsonValue

from fylgja.api.now_models import NowModel
from fylgja.models import HealthState

__all__ = [
    "INSPECT_COLLECTORS_SCHEMA",
    "AppleScriptSwitch",
    "HealthDetail",
    "InspectCollector",
    "InspectCollectors",
    "KindCount",
    "RecordCounts",
]

INSPECT_COLLECTORS_SCHEMA: Final = "fylgja.inspect.collectors/1"
"""The version string ``/inspect/collectors`` carries in ``schema``."""


class HealthDetail(NowModel):
    """A collector's health as the daemon holds it: the supervisor's periodic ``doctor()`` probe,
    merged with the ingest gate's degraded state."""

    state: HealthState
    since: AwareDatetime | None = None
    """When the collector entered ``state``."""
    last_ok_at: AwareDatetime | None = None
    restarts: int = 0
    """Restarts by the supervisor since the daemon started."""
    last_error_kind: str | None = None
    detail: str | None = None
    """Redacted like the settings (see ``docs/api/inspect.md``)."""
    retry_in_seconds: float | None = None
    """Seconds until the next restart while the collector waits in backoff."""


class KindCount(NowModel):
    """Accepted records of one entity kind and record type."""

    entity_kind: str
    record_type: Literal["event", "snapshot"]
    accepted: int
    last_at: AwareDatetime | None = None
    """When the last one was accepted (the daemon's clock)."""


class RecordCounts(NowModel):
    """What the ingest gate counted for one collector since the daemon started."""

    accepted: int
    dropped: int
    """Dropped by the privacy filter (ignored app or title)."""
    lost: int
    """Accepted, but not queued because the writer was stopped."""
    rejected: dict[str, int]
    """Per gate reason: ``schema``, ``source``, ``entity_kind``, ``timestamp`` (every reason, zero
    included)."""
    kinds: list[KindCount]
    """Sorted by entity kind, then record type."""
    last_at: AwareDatetime | None = None
    """When the collector's last record was accepted; absent before the first one."""


class InspectCollector(NowModel):
    """One collector the registry knows."""

    name: str
    origin: Literal["built-in", "external"]
    distribution: str | None = None
    distribution_version: str | None = None
    state: Literal["enabled", "disabled", "rejected"]
    """``rejected``: a duplicate entry-point name, never loaded."""
    enabled: bool
    loaded: bool
    """Its code was imported (a disabled external is not)."""
    api_version: int | None = None
    capabilities: list[str] | None = None
    entity_kinds: list[str] | None = None
    """The entity kinds the ingest gate lets it emit."""
    health: HealthDetail
    settings: dict[str, JsonValue] | None = None
    """Its validated ``[collectors.<name>]`` settings, redacted (see ``docs/api/inspect.md``);
    absent when they were not validated."""
    records: RecordCounts | None = None
    """Absent for a collector that does not run."""


class AppleScriptSwitch(NowModel):
    """One ``[actions.applescript]`` switch and doctor's last recorded check of it."""

    name: str
    configured: bool
    """``[actions.applescript] <name>``."""
    doctor_ok: bool | None = None
    """Absent when ``fylgja doctor`` has not checked this switch."""
    doctor_at: AwareDatetime | None = None
    doctor_reason: str | None = None


class InspectCollectors(NowModel):
    """The answer of ``GET /inspect/collectors``."""

    schema_: Literal["fylgja.inspect.collectors/1"] = Field(
        default=INSPECT_COLLECTORS_SCHEMA, alias="schema"
    )
    """The contract version."""
    generated_at: AwareDatetime
    started_at: AwareDatetime | None = None
    """When the daemon started; the counters count from here."""
    collectors: list[InspectCollector]
    """In registry order: by name, then distribution."""
    not_installed: list[str]
    """``[collectors.<name>]`` tables with no entry point behind them, sorted."""
    applescript: list[AppleScriptSwitch]
    """Reported, never run: in the order doctor checks them."""
