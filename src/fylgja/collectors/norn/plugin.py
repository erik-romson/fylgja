"""The plugin descriptor of the norn collector."""

from __future__ import annotations

from pydantic import BaseModel

from fylgja.collectors.base import (
    COLLECTOR_API_MAJOR,
    Capability,
    Collector,
    CollectorContext,
    CollectorPlugin,
)
from fylgja.collectors.norn import adapter
from fylgja.collectors.norn.collector import NornCollector, NornSettings

__all__ = ["NAME", "plugin"]

NAME = "norn"


def _factory(settings: BaseModel, context: CollectorContext) -> Collector:
    if not isinstance(settings, NornSettings):
        raise TypeError("norn collector got foreign settings")
    # Looked up at call time so tests can replace the seams and HOME.
    return NornCollector(settings, context, seams=adapter.default_seams())


plugin = CollectorPlugin(
    name=NAME,
    api_major=COLLECTOR_API_MAJOR,
    capabilities=frozenset({Capability.SESSIONS}),
    config_model=NornSettings,
    factory=_factory,
)
