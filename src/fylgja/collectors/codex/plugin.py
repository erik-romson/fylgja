"""The plugin descriptor of the Codex collector."""

from __future__ import annotations

from pydantic import BaseModel

from fylgja.collectors.base import (
    COLLECTOR_API_MAJOR,
    Capability,
    Collector,
    CollectorContext,
    CollectorPlugin,
)
from fylgja.collectors.codex import adapter
from fylgja.collectors.codex.collector import CodexCollector, CodexSettings

__all__ = ["NAME", "plugin"]

NAME = "codex"


def _factory(settings: BaseModel, context: CollectorContext) -> Collector:
    if not isinstance(settings, CodexSettings):
        raise TypeError("codex collector got foreign settings")
    home = settings.codex_home
    # Looked up at call time so tests can replace the seams and HOME.
    return CodexCollector(
        settings,
        context,
        codex_home=adapter.default_codex_home() if home is None else home,
        seams=adapter.default_seams(),
    )


plugin = CollectorPlugin(
    name=NAME,
    api_major=COLLECTOR_API_MAJOR,
    capabilities=frozenset({Capability.SESSIONS}),
    config_model=CodexSettings,
    factory=_factory,
)
