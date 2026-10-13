"""Ranker registry (``docs/decisions/0052``).

The same rules as the collector registry (:mod:`fylgja.collectors.registry`), whose metadata
discovery it reuses for the ``fylgja.rankers`` group:

* **Discovery reads metadata only** (``name``, ``value`` and the distribution's name and version)
  and never calls ``EntryPoint.load()``. A name claimed by several distributions is a duplicate:
  a built-in keeps it and the external claimants are rejected; with no built-in among them,
  every claimant is rejected.
* **The enable rule.** A built-in (distribution ``fylgja``) loads unless
  ``[rankers.<name>] enabled = false``; an external loads only with ``enabled = true``. A disabled
  ranker, built-in or external, is **never imported**.
* **Settings.** ``[rankers.<name>]`` minus ``enabled`` is validated against the plugin's
  ``config_model`` at load; the instance is passed to every ``rank`` call.
* **Failure is reported, never raised.** An import that fails, an object that is not a
  :class:`~fylgja.rankers.base.RankerPlugin`, a name that does not match the entry point, the
  reserved name ``natural``, another API major or settings that fail validation give a
  :class:`RegisteredRanker` with an ``error`` and no plugin. The daemon keeps running.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ValidationError

from fylgja.collectors.registry import (
    DiscoveredEntry,
    Discovery,
    EntryPointLike,
    Origin,
    discover,
)
from fylgja.config import Config
from fylgja.rankers.base import (
    ENTRY_POINT_GROUP,
    FLOOR_NAME,
    RANKER_API_MAJOR,
    Ranked,
    RankerPlugin,
    RankInput,
    default_workspace_order,
)

__all__ = [
    "MAX_ERROR",
    "EntryPointSource",
    "Origin",
    "RegisteredRanker",
    "discover_rankers",
    "load_rankers",
]

MAX_ERROR: Final = 300
"""An error message is cut to this many characters."""

EntryPointSource = Callable[[], Iterable[EntryPointLike]]


def _installed_entry_points() -> Iterable[EntryPointLike]:
    from importlib.metadata import entry_points

    return entry_points(group=ENTRY_POINT_GROUP)


def discover_rankers(source: EntryPointSource | None = None) -> Discovery:
    """Read the ``fylgja.rankers`` entry points from metadata, importing nothing."""
    return discover(_installed_entry_points if source is None else source)


@dataclass(frozen=True)
class RegisteredRanker:
    """One ranker after the registry applied the configuration."""

    name: str
    origin: Origin
    distribution: str | None
    version: str | None
    enabled: bool
    plugin: RankerPlugin | None = None
    settings: BaseModel | None = None
    """The validated ``[rankers.<name>]`` settings; set exactly when ``plugin`` is."""
    error: str | None = None
    """``<kind>: <detail>`` when the ranker could not be loaded."""

    @property
    def loaded(self) -> bool:
        """True when it is enabled, imported and its settings are valid: it can rank."""
        return self.plugin is not None and self.settings is not None

    def _require(self) -> tuple[RankerPlugin, BaseModel]:
        if self.plugin is None or self.settings is None:
            raise LookupError(f"ranker {self.name!r} is not loaded")
        return self.plugin, self.settings

    def rank(self, data: RankInput) -> list[Ranked]:
        """Run the ranker with its settings. Raises ``LookupError`` when it is not loaded."""
        plugin, settings = self._require()
        return list(plugin.rank(data, settings))

    def order_workspaces(self, data: RankInput, ranked: Sequence[Ranked]) -> list[str]:
        """The ranker's workspace order, or :func:`default_workspace_order` when it has none."""
        plugin, _ = self._require()
        if plugin.order_workspaces is None:
            return default_workspace_order(data, ranked)
        return list(plugin.order_workspaces(data, ranked))


def _is_enabled(entry: DiscoveredEntry, config: Config) -> bool:
    flag = config.ranker_enabled(entry.name)
    if entry.origin is Origin.BUILT_IN:
        return flag is not False
    return flag is True


def _validation_detail(name: str, exc: ValidationError) -> str:
    parts = []
    for error in exc.errors(include_url=False, include_input=False):
        loc = ".".join(str(p) for p in error["loc"])
        parts.append(f"[rankers.{name}].{loc}: {error['msg']}" if loc else error["msg"])
    return "; ".join(parts)


def _registered(
    entry: DiscoveredEntry,
    enabled: bool,
    *,
    plugin: RankerPlugin | None = None,
    settings: BaseModel | None = None,
    error: str | None = None,
) -> RegisteredRanker:
    return RegisteredRanker(
        name=entry.name,
        origin=entry.origin,
        distribution=entry.distribution,
        version=entry.version,
        enabled=enabled,
        plugin=plugin,
        settings=settings,
        error=None if error is None else error[:MAX_ERROR],
    )


def _load(entry: DiscoveredEntry, config: Config) -> RegisteredRanker:
    try:
        loaded = entry.entry_point.load()
    except Exception as exc:
        return _registered(entry, True, error=f"import: {type(exc).__name__}: {exc}")
    if not isinstance(loaded, RankerPlugin):
        return _registered(entry, True, error="entry_point: object is not a RankerPlugin")
    if loaded.name != entry.name:
        detail = f"plugin name {loaded.name!r} does not match entry point {entry.name!r}"
        return _registered(entry, True, error=f"name: {detail}")
    if loaded.api_major != RANKER_API_MAJOR:
        detail = f"plugin API major {loaded.api_major}, core supports {RANKER_API_MAJOR}"
        return _registered(entry, True, error=f"api_major: {detail}")
    try:
        settings = loaded.config_model.model_validate(config.ranker_settings(entry.name))
    except ValidationError as exc:
        return _registered(entry, True, error=f"config: {_validation_detail(entry.name, exc)}")
    return _registered(entry, True, plugin=loaded, settings=settings)


def load_rankers(
    config: Config, source: EntryPointSource | None = None
) -> tuple[RegisteredRanker, ...]:
    """Apply ``config`` to the discovered rankers, importing only the enabled ones.

    Sorted by name, then distribution.
    """
    discovery = discover_rankers(source)
    result: list[RegisteredRanker] = []
    for entry in discovery.entries:
        enabled = _is_enabled(entry, config)
        if entry.name == FLOOR_NAME:
            detail = f"the name {FLOOR_NAME!r} is reserved for the floor ordering"
            result.append(_registered(entry, enabled, error=f"reserved: {detail}"))
        elif enabled:
            result.append(_load(entry, config))
        else:
            result.append(_registered(entry, False))
    for entry in discovery.duplicates:
        detail = f"entry point name {entry.name!r} is claimed by more than one distribution"
        result.append(_registered(entry, False, error=f"duplicate: {detail}"))
    return tuple(sorted(result, key=lambda r: (r.name, r.distribution or "")))
