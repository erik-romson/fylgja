"""The plugin descriptor of the ``appcontext`` collector."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from fylgja import paths
from fylgja.collectors.appcontext import adapter, parse
from fylgja.collectors.appcontext.collector import AppContextCollector, AppContextSettings
from fylgja.collectors.base import (
    COLLECTOR_API_MAJOR,
    Capability,
    Collector,
    CollectorContext,
    CollectorPlugin,
)

__all__ = ["NAME", "plugin"]

NAME = parse.SOURCE


def _factory(settings: BaseModel, context: CollectorContext) -> Collector:
    if not isinstance(settings, AppContextSettings):
        raise TypeError("appcontext collector got foreign settings")
    database = paths.database_file() if settings.database is None else Path(settings.database)
    # Looked up at call time so tests can replace the inputs.
    return AppContextCollector(settings, context, inputs=adapter.default_inputs(database))


plugin = CollectorPlugin(
    name=NAME,
    api_major=COLLECTOR_API_MAJOR,
    capabilities=frozenset({Capability.CONTEXT}),
    config_model=AppContextSettings,
    factory=_factory,
)
