"""The plugin descriptor of the Claude collector."""

from __future__ import annotations

from pydantic import BaseModel

from fylgja.collectors.base import (
    COLLECTOR_API_MAJOR,
    Capability,
    Collector,
    CollectorContext,
    CollectorPlugin,
)
from fylgja.collectors.claude import adapter
from fylgja.collectors.claude.collector import ClaudeCollector, ClaudeSettings, ignore_list

__all__ = ["NAME", "plugin"]

NAME = "claude"


def _factory(settings: BaseModel, context: CollectorContext) -> Collector:
    if not isinstance(settings, ClaudeSettings):
        raise TypeError("claude collector got foreign settings")
    registry_dir = settings.registry_dir
    projects_dir = settings.projects_dir
    # Looked up at call time so tests can replace the seams and HOME.
    return ClaudeCollector(
        settings,
        context,
        registry_dir=adapter.default_registry_dir() if registry_dir is None else registry_dir,
        seams=adapter.default_seams(),
        projects_dir=adapter.default_projects_dir() if projects_dir is None else projects_dir,
        ignore_cwd=ignore_list(settings),
    )


plugin = CollectorPlugin(
    name=NAME,
    api_major=COLLECTOR_API_MAJOR,
    # SESSIONS: sessions, activity and hook events. PROJECT_STATE: the usage rows (rate limits) the
    # statusline reader emits. TODOS: each live session's task list (WP4.3). The ingest gate rejects
    # kinds a plugin did not declare.
    capabilities=frozenset({Capability.SESSIONS, Capability.PROJECT_STATE, Capability.TODOS}),
    config_model=ClaudeSettings,
    factory=_factory,
)
