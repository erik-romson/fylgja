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
    # Looked up at call time so tests can replace the seams and HOME.
    return ClaudeCollector(
        settings,
        context,
        registry_dir=adapter.default_registry_dir() if registry_dir is None else registry_dir,
        seams=adapter.default_seams(),
        ignore_cwd=ignore_list(settings),
    )


plugin = CollectorPlugin(
    name=NAME,
    api_major=COLLECTOR_API_MAJOR,
    capabilities=frozenset({Capability.SESSIONS}),
    config_model=ClaudeSettings,
    factory=_factory,
)
