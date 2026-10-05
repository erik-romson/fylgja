"""The plugin descriptor of the AeroSpace collector."""

from __future__ import annotations

from pydantic import BaseModel

from fylgja.collectors.aerospace import adapter
from fylgja.collectors.aerospace.collector import AerospaceCollector, AerospaceSettings
from fylgja.collectors.base import (
    COLLECTOR_API_MAJOR,
    Capability,
    Collector,
    CollectorContext,
    CollectorPlugin,
)

__all__ = ["NAME", "plugin"]

NAME = "aerospace"


def _factory(settings: BaseModel, context: CollectorContext) -> Collector:
    if not isinstance(settings, AerospaceSettings):
        raise TypeError("aerospace collector got foreign settings")
    # Looked up at call time so tests can replace the seams.
    return AerospaceCollector(
        settings, context, binary=adapter.resolve_binary(), seams=adapter.default_seams()
    )


plugin = CollectorPlugin(
    name=NAME,
    api_major=COLLECTOR_API_MAJOR,
    capabilities=frozenset({Capability.WINDOWS}),
    config_model=AerospaceSettings,
    factory=_factory,
)
