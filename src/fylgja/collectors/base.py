"""Collector API v1 (``PLAN-final-plan.md`` §3.3, ``PLAN.md`` §4.1).

This module is public: it ships in the wheel and external plugin authors import it. It is fully
typed and imports neither FastAPI nor Typer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from pydantic import BaseModel

from fylgja.models import EntityKind, HealthReport, Record

__all__ = [
    "CAPABILITY_ENTITY_KINDS",
    "COLLECTOR_API_MAJOR",
    "PLUGIN_NAME_PATTERN",
    "Capability",
    "Clock",
    "Collector",
    "CollectorContext",
    "CollectorPlugin",
    "ReadOnlyPaths",
    "RecordSink",
    "allowed_entity_kinds",
]

COLLECTOR_API_MAJOR: Final = 1

PLUGIN_NAME_PATTERN: Final = re.compile(r"[a-z0-9_-]+")


class Capability(StrEnum):
    """What a plugin may emit. The ingest gate checks records against these."""

    WINDOWS = "windows"
    SESSIONS = "sessions"
    TODOS = "todos"
    CONTEXT = "context"
    PROJECT_STATE = "project_state"


CAPABILITY_ENTITY_KINDS: Final[Mapping[Capability, frozenset[EntityKind]]] = MappingProxyType(
    {
        Capability.WINDOWS: frozenset({EntityKind.WINDOW, EntityKind.WORKSPACE, EntityKind.FOCUS}),
        Capability.SESSIONS: frozenset(
            {EntityKind.SESSION, EntityKind.SESSION_ACTIVITY, EntityKind.HOOK_EVENT}
        ),
        Capability.TODOS: frozenset({EntityKind.TODO}),
        Capability.CONTEXT: frozenset({EntityKind.CONTEXT_ITEM, EntityKind.PRESENCE}),
        Capability.PROJECT_STATE: frozenset(
            {EntityKind.PROJECT, EntityKind.BUILD, EntityKind.USAGE}
        ),
    }
)
"""Entity kinds each capability allows. Every kind belongs to exactly one capability."""


def allowed_entity_kinds(capabilities: Iterable[Capability]) -> frozenset[EntityKind]:
    """The union of the entity kinds that ``capabilities`` allow."""
    kinds: set[EntityKind] = set()
    for capability in capabilities:
        kinds |= CAPABILITY_ENTITY_KINDS[capability]
    return frozenset(kinds)


@runtime_checkable
class RecordSink(Protocol):
    """Where a collector sends records."""

    def emit(self, record: Record) -> None:
        """Queue a record without waiting for it to be committed."""
        ...

    async def emit_committed(self, record: Record) -> None:
        """Queue a record and return once it is durably committed (delete-after-commit)."""
        ...


@runtime_checkable
class Clock(Protocol):
    """Time source; tests substitute a fake."""

    def now(self) -> datetime:
        """The current time as a timezone-aware datetime."""
        ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


@runtime_checkable
class ReadOnlyPaths(Protocol):
    """Directories a plugin may read. It never receives a write handle from the core."""

    @property
    def data_dir(self) -> Path: ...

    @property
    def state_dir(self) -> Path: ...

    @property
    def inbox_dir(self) -> Path:
        """The plugin's own subdirectory of the hook inbox (``hook-inbox/<source>/``)."""
        ...


@dataclass(frozen=True)
class CollectorContext:
    sink: RecordSink
    clock: Clock
    logger: logging.LoggerAdapter[logging.Logger]
    paths: ReadOnlyPaths


@runtime_checkable
class Collector(Protocol):
    async def run(self) -> None:
        """Run until cancelled."""
        ...

    async def doctor(self) -> HealthReport:
        """Report health. Must have no side effects."""
        ...


@dataclass(frozen=True)
class CollectorPlugin:
    """Descriptor a plugin exports as ``plugin`` from its package."""

    name: str
    api_major: int
    capabilities: frozenset[Capability]
    config_model: type[BaseModel]
    factory: Callable[[BaseModel, CollectorContext], Collector]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or PLUGIN_NAME_PATTERN.fullmatch(self.name) is None:
            raise ValueError(f"invalid plugin name {self.name!r}: must match [a-z0-9_-]+")
        if isinstance(self.api_major, bool) or not isinstance(self.api_major, int):
            raise TypeError("api_major must be an int")
        for capability in self.capabilities:
            if not isinstance(capability, Capability):
                raise ValueError(f"invalid capability {capability!r}")
        if not (isinstance(self.config_model, type) and issubclass(self.config_model, BaseModel)):
            raise TypeError("config_model must be a pydantic BaseModel subclass")
        if not callable(self.factory):
            raise TypeError("factory must be callable")

    @property
    def allowed_kinds(self) -> frozenset[EntityKind]:
        """Entity kinds the ingest gate lets this plugin emit."""
        return allowed_entity_kinds(self.capabilities)
