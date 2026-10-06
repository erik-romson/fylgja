"""The plugin descriptor of the ``project`` collector."""

from __future__ import annotations

from pydantic import BaseModel

from fylgja import paths
from fylgja.collectors.base import (
    COLLECTOR_API_MAJOR,
    Capability,
    Collector,
    CollectorContext,
    CollectorPlugin,
)
from fylgja.collectors.project import adapter, parse
from fylgja.collectors.project.collector import ProjectCollector, ProjectSettings

__all__ = ["NAME", "plugin"]

NAME = parse.SOURCE


def _factory(settings: BaseModel, context: CollectorContext) -> Collector:
    if not isinstance(settings, ProjectSettings):
        raise TypeError("project collector got foreign settings")
    database = settings.database
    # Looked up at call time so tests can point the data directory elsewhere.
    return ProjectCollector(
        settings,
        context,
        database=paths.database_file() if database is None else database,
        fs=adapter.default_fs(),
    )


plugin = CollectorPlugin(
    name=NAME,
    api_major=COLLECTOR_API_MAJOR,
    # TODOS: repo-root TODO.md files (WP4.3). Step 41 adds PROJECT_STATE.
    capabilities=frozenset({Capability.TODOS}),
    config_model=ProjectSettings,
    factory=_factory,
)
