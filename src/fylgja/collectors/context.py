"""Per-plugin collector context (``PLAN-final-plan.md`` §3.3, WP1.3).

The supervisor builds one :class:`~fylgja.collectors.base.CollectorContext` per plugin. It carries
the injected sink, a clock, a logger that stamps ``collector=<name>`` on every record, and
read-only paths. The paths object is frozen and names exactly one inbox, the plugin's own; it never
holds another plugin's inbox, so a plugin cannot be handed a write location it does not own. The
core is the only writer of the inbox tree: ``fylgja hook`` writes the files and the ingest path
deletes them after commit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from fylgja import paths
from fylgja.collectors.base import (
    PLUGIN_NAME_PATTERN,
    Clock,
    CollectorContext,
    ReadOnlyPaths,
    RecordSink,
)

__all__ = [
    "LOGGER_NAME",
    "PluginPaths",
    "SystemClock",
    "build_context",
    "plugin_paths",
]

LOGGER_NAME: Final = "fylgja.collector"


class SystemClock:
    """The real clock: wall time in UTC, the monotonic clock and ``asyncio.sleep``."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True)
class PluginPaths:
    """What one plugin may read. Immutable, and it holds only this plugin's inbox."""

    plugin: str
    data_dir: Path
    state_dir: Path
    inbox_dir: Path


def plugin_paths(plugin: str, *, inbox_source: str | None = None) -> PluginPaths:
    """Resolve the paths of ``plugin`` through :mod:`fylgja.paths`.

    The inbox is ``hook-inbox/<source>/`` where ``source`` defaults to the plugin name.
    """
    if PLUGIN_NAME_PATTERN.fullmatch(plugin) is None:
        raise ValueError(f"invalid plugin name {plugin!r}")
    return PluginPaths(
        plugin=plugin,
        data_dir=paths.data_dir(),
        state_dir=paths.state_dir(),
        inbox_dir=paths.inbox_dir(plugin if inbox_source is None else inbox_source),
    )


def build_context(
    plugin: str,
    sink: RecordSink,
    *,
    clock: Clock | None = None,
    inbox_source: str | None = None,
    logger: logging.Logger | None = None,
) -> CollectorContext:
    """The context handed to ``plugin``'s factory."""
    base = logging.getLogger(LOGGER_NAME) if logger is None else logger
    read_only: ReadOnlyPaths = plugin_paths(plugin, inbox_source=inbox_source)
    return CollectorContext(
        sink=sink,
        clock=SystemClock() if clock is None else clock,
        logger=logging.LoggerAdapter(base, {"collector": plugin}, merge_extra=True),
        paths=read_only,
    )
