"""The plugin descriptor of the OpenCode collector."""

from __future__ import annotations

from pydantic import BaseModel

from fylgja.collectors.base import (
    COLLECTOR_API_MAJOR,
    Capability,
    Collector,
    CollectorContext,
    CollectorPlugin,
)
from fylgja.collectors.opencode import adapter
from fylgja.collectors.opencode.collector import OpenCodeCollector, OpenCodeSettings

__all__ = ["NAME", "plugin"]

NAME = "opencode"


def _factory(settings: BaseModel, context: CollectorContext) -> Collector:
    if not isinstance(settings, OpenCodeSettings):
        raise TypeError("opencode collector got foreign settings")
    db_path = settings.db_path
    # Looked up at call time so tests can replace the seams and HOME.
    return OpenCodeCollector(
        settings,
        context,
        db_path=adapter.default_db_path() if db_path is None else db_path,
        seams=adapter.default_seams(),
    )


plugin = CollectorPlugin(
    name=NAME,
    api_major=COLLECTOR_API_MAJOR,
    capabilities=frozenset({Capability.SESSIONS}),
    config_model=OpenCodeSettings,
    factory=_factory,
)
