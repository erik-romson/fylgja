"""The plugin descriptor of the presence collector."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from fylgja import paths
from fylgja.collectors.base import (
    COLLECTOR_API_MAJOR,
    Capability,
    Collector,
    CollectorContext,
    CollectorPlugin,
)
from fylgja.collectors.presence import adapter, parse
from fylgja.collectors.presence.collector import PresenceCollector, PresenceSettings

__all__ = ["NAME", "plugin"]

NAME = parse.SOURCE


def _factory(settings: BaseModel, context: CollectorContext) -> Collector:
    if not isinstance(settings, PresenceSettings):
        raise TypeError("presence collector got foreign settings")
    database = paths.database_file() if settings.database is None else Path(settings.database)
    # Looked up at call time so tests can replace the signals.
    return PresenceCollector(settings, context, signals=adapter.default_signals(database))


plugin = CollectorPlugin(
    name=NAME,
    api_major=COLLECTOR_API_MAJOR,
    capabilities=frozenset({Capability.CONTEXT}),
    config_model=PresenceSettings,
    factory=_factory,
)
