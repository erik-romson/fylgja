"""Collector plugin registry (``PLAN-final-plan.md`` WP1.2, ``PLAN.md`` §4.1).

Discovery reads entry-point metadata only (``name``, ``value`` and the distribution's name and
version) and never calls ``EntryPoint.load()``. Duplicate names are rejected from that metadata.
Code is imported only for built-ins and for externals with an explicit ``enabled = true``; a
disabled external is never imported.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from fylgja.collectors.base import COLLECTOR_API_MAJOR, CollectorPlugin
from fylgja.config import Config
from fylgja.models import HealthReport, HealthState

__all__ = [
    "ENTRY_POINT_GROUP",
    "NOT_LOADED",
    "CollectorRow",
    "DiscoveredEntry",
    "Discovery",
    "EntryPointLike",
    "Origin",
    "RegisteredCollector",
    "discover",
    "list_collectors",
    "load_registry",
]

ENTRY_POINT_GROUP: Final = "fylgja.collectors"
BUILTIN_DISTRIBUTION: Final = "fylgja"
NOT_LOADED: Final = "not_loaded"


class Origin(StrEnum):
    BUILT_IN = "built-in"
    EXTERNAL = "external"


class DistributionLike(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...


class EntryPointLike(Protocol):
    """The slice of ``importlib.metadata.EntryPoint`` the registry uses."""

    @property
    def name(self) -> str: ...

    @property
    def value(self) -> str: ...

    @property
    def dist(self) -> DistributionLike | None: ...

    def load(self) -> object: ...


EntryPointSource = Callable[[], Iterable[EntryPointLike]]


def _installed_entry_points() -> Iterable[EntryPointLike]:
    from importlib.metadata import entry_points

    return entry_points(group=ENTRY_POINT_GROUP)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class DiscoveredEntry:
    """Metadata of one entry point. ``entry_point`` is kept only so it can be loaded later."""

    name: str
    value: str
    distribution: str | None
    version: str | None
    entry_point: EntryPointLike = field(repr=False, compare=False)

    @property
    def origin(self) -> Origin:
        if self.distribution is not None and _normalize(self.distribution) == BUILTIN_DISTRIBUTION:
            return Origin.BUILT_IN
        return Origin.EXTERNAL


def _entry(entry_point: EntryPointLike) -> DiscoveredEntry:
    dist = entry_point.dist
    return DiscoveredEntry(
        name=entry_point.name,
        value=entry_point.value,
        distribution=None if dist is None else dist.name,
        version=None if dist is None else dist.version,
        entry_point=entry_point,
    )


@dataclass(frozen=True)
class Discovery:
    """Result of reading metadata: usable entries and entries rejected as duplicates."""

    entries: tuple[DiscoveredEntry, ...]
    duplicates: tuple[DiscoveredEntry, ...]


def discover(source: EntryPointSource | None = None) -> Discovery:
    """Read the ``fylgja.collectors`` entry points from metadata, importing nothing.

    A name claimed by several distributions is a duplicate. A built-in keeps the name and the
    external claimants are rejected; when no built-in claims it, every claimant is rejected.
    """
    found = (_installed_entry_points if source is None else source)()
    unique: dict[tuple[str, str, str | None], DiscoveredEntry] = {}
    for entry_point in found:
        entry = _entry(entry_point)
        unique.setdefault((entry.name, entry.value, entry.distribution), entry)
    by_name: dict[str, list[DiscoveredEntry]] = defaultdict(list)
    for entry in unique.values():
        by_name[entry.name].append(entry)
    accepted: list[DiscoveredEntry] = []
    rejected: list[DiscoveredEntry] = []
    for name in sorted(by_name):
        group = by_name[name]
        if len(group) == 1:
            accepted.append(group[0])
            continue
        builtin = [e for e in group if e.origin is Origin.BUILT_IN]
        if len(builtin) == 1:
            accepted.append(builtin[0])
            rejected.extend(e for e in group if e is not builtin[0])
        else:
            rejected.extend(group)
    return Discovery(entries=tuple(accepted), duplicates=tuple(rejected))


@dataclass(frozen=True)
class RegisteredCollector:
    """One collector after the registry applied the configuration."""

    entry: DiscoveredEntry
    enabled: bool
    plugin: CollectorPlugin | None = None
    settings: BaseModel | None = None
    health: HealthReport | None = None
    rejected: bool = False

    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def loaded(self) -> bool:
        return self.plugin is not None

    @property
    def runnable(self) -> bool:
        """True when the supervisor may start it: enabled, loaded and without a load failure."""
        return self.enabled and self.plugin is not None and self.health is None


def _report(state: HealthState, kind: str | None, detail: str) -> HealthReport:
    return HealthReport(
        state=state, detail=detail[:300], error_kind=kind, checked_at=datetime.now(UTC)
    )


def _failed(kind: str, detail: str) -> HealthReport:
    return _report(HealthState.FAILED, kind, detail)


def _is_enabled(entry: DiscoveredEntry, config: Config) -> bool:
    flag = config.collector_enabled(entry.name)
    if entry.origin is Origin.BUILT_IN:
        return flag is not False
    return flag is True


def _validation_detail(name: str, exc: ValidationError) -> str:
    parts = []
    for error in exc.errors(include_url=False, include_input=False):
        loc = ".".join(str(p) for p in error["loc"])
        parts.append(f"[collectors.{name}].{loc}: {error['msg']}" if loc else error["msg"])
    return "; ".join(parts)


def _load(entry: DiscoveredEntry, config: Config, enabled: bool) -> RegisteredCollector:
    try:
        loaded = entry.entry_point.load()
    except Exception as exc:
        return RegisteredCollector(
            entry, enabled, health=_failed("import", f"{type(exc).__name__}: {exc}")
        )
    if not isinstance(loaded, CollectorPlugin):
        return RegisteredCollector(
            entry, enabled, health=_failed("entry_point", "object is not a CollectorPlugin")
        )
    if loaded.name != entry.name:
        detail = f"plugin name {loaded.name!r} does not match entry point {entry.name!r}"
        return RegisteredCollector(entry, enabled, plugin=loaded, health=_failed("name", detail))
    if loaded.api_major != COLLECTOR_API_MAJOR:
        detail = f"plugin API major {loaded.api_major}, core supports {COLLECTOR_API_MAJOR}"
        return RegisteredCollector(
            entry, enabled, plugin=loaded, health=_failed("api_major", detail)
        )
    try:
        settings = loaded.config_model.model_validate(config.collector_settings(entry.name))
    except ValidationError as exc:
        return RegisteredCollector(
            entry,
            enabled,
            plugin=loaded,
            health=_failed("config", _validation_detail(entry.name, exc)),
        )
    health = None if enabled else _report(HealthState.DISABLED, None, "disabled in configuration")
    return RegisteredCollector(entry, enabled, plugin=loaded, settings=settings, health=health)


def load_registry(
    config: Config, source: EntryPointSource | None = None
) -> tuple[RegisteredCollector, ...]:
    """Apply ``config`` to the discovered entry points, importing only what the rules allow."""
    discovery = discover(source)
    result: list[RegisteredCollector] = []
    for entry in discovery.entries:
        enabled = _is_enabled(entry, config)
        if entry.origin is Origin.EXTERNAL and not enabled:
            result.append(RegisteredCollector(entry, enabled=False))
        else:
            result.append(_load(entry, config, enabled))
    for entry in discovery.duplicates:
        detail = f"entry point name {entry.name!r} is claimed by more than one distribution"
        result.append(
            RegisteredCollector(
                entry, enabled=False, health=_failed("duplicate", detail), rejected=True
            )
        )
    return tuple(sorted(result, key=lambda c: (c.name, c.entry.distribution or "")))


class CollectorRow(BaseModel):
    """One row of ``collectors list``. Fields that need code that was not loaded say so."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    origin: Origin
    distribution: str | None
    distribution_version: str | None
    state: Literal["enabled", "disabled", "rejected"]
    enabled: bool
    api_version: int | Literal["not_loaded"]
    capabilities: list[str] | Literal["not_loaded"]
    health: HealthReport | Literal["not_loaded"]


def _row(item: RegisteredCollector, health: Mapping[str, HealthReport]) -> CollectorRow:
    entry = item.entry
    state: Literal["enabled", "disabled", "rejected"]
    if item.rejected:
        state = "rejected"
    else:
        state = "enabled" if item.enabled else "disabled"
    plugin = item.plugin
    shown: HealthReport | Literal["not_loaded"]
    if item.health is not None:
        shown = item.health
    elif plugin is None:
        shown = NOT_LOADED
    else:
        shown = health.get(item.name) or _report(HealthState.STARTING, None, "not yet started")
    return CollectorRow(
        name=item.name,
        origin=entry.origin,
        distribution=entry.distribution,
        distribution_version=entry.version,
        state=state,
        enabled=item.enabled,
        api_version=NOT_LOADED if plugin is None else plugin.api_major,
        capabilities=(
            NOT_LOADED if plugin is None else sorted(c.value for c in plugin.capabilities)
        ),
        health=shown,
    )


def list_collectors(
    config: Config,
    *,
    source: EntryPointSource | None = None,
    health: Mapping[str, HealthReport] | None = None,
    registry: Sequence[RegisteredCollector] | None = None,
) -> list[CollectorRow]:
    """Rows for ``collectors list`` (WP1.2's done-condition shape).

    Loaded plugins show API version, capabilities and health; ``health`` is the supervisor's live
    view and defaults to ``starting``. A disabled external shows only its metadata and
    ``not_loaded``. Pass ``registry`` to reuse an already loaded registry.
    """
    items = load_registry(config, source) if registry is None else registry
    live = {} if health is None else health
    return [_row(item, live) for item in items]
