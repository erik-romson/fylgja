"""A small synthetic collector: it emits one window and one todo on every clock tick.

``source_file`` (optional) names a JSON file with ``{"windows": [...], "todos": [...]}``. A
missing file, a permission error or malformed content make the collector report ``degraded``
health and emit nothing until the file is fine again; the collector itself keeps running.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fylgja.collectors.base import (
    COLLECTOR_API_MAJOR,
    Capability,
    Collector,
    CollectorContext,
    CollectorPlugin,
)
from fylgja.models import (
    HealthReport,
    HealthState,
    SnapshotItem,
    TodoPayload,
    TodoSnapshot,
    WindowPayload,
    WindowSnapshot,
)

__all__ = ["FakeCollector", "FakeSettings", "plugin"]

NAME = "fake"
_DEFAULT_WINDOWS = [{"id": "fake-window-1", "title": "Fake window", "workspace": "fake"}]
_DEFAULT_TODOS = [{"id": "fake-todo-1", "text": "Fake todo"}]


class FakeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval: float = Field(default=5.0, gt=0)
    source_file: Path | None = None


class _Window(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    title: str | None = None
    workspace: str | None = None


class _Todo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    text: str


class _Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    windows: list[_Window] = Field(default_factory=list)
    todos: list[_Todo] = Field(default_factory=list)


class _SourceError(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _read(source_file: Path | None) -> _Source:
    if source_file is None:
        return _Source.model_validate({"windows": _DEFAULT_WINDOWS, "todos": _DEFAULT_TODOS})
    try:
        text = source_file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise _SourceError("missing_file", "source file is missing") from exc
    except PermissionError as exc:
        raise _SourceError("permission", "source file is not readable") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise _SourceError(
            "unreadable", f"source file cannot be read: {type(exc).__name__}"
        ) from exc
    try:
        data: Any = json.loads(text)
        return _Source.model_validate(data)
    except (ValueError, ValidationError) as exc:
        raise _SourceError("malformed", "source file is malformed") from exc


class FakeCollector:
    def __init__(self, settings: FakeSettings, context: CollectorContext) -> None:
        self._settings = settings
        self._context = context

    def _tick(self) -> None:
        source = _read(self._settings.source_file)
        now = self._context.clock.now()
        self._context.sink.emit(
            WindowSnapshot(
                source=NAME,
                entity_id="all",
                observed_at=now,
                complete=True,
                items=tuple(
                    SnapshotItem(
                        entity_id=w.id,
                        payload=WindowPayload(
                            workspace=w.workspace, app_name="Fake", title=w.title
                        ),
                    )
                    for w in source.windows
                ),
            )
        )
        self._context.sink.emit(
            TodoSnapshot(
                source=NAME,
                entity_id="all",
                observed_at=now,
                complete=True,
                items=tuple(
                    SnapshotItem(entity_id=t.id, payload=TodoPayload(text=t.text))
                    for t in source.todos
                ),
            )
        )

    async def run(self) -> None:
        while True:
            try:
                self._tick()
            except _SourceError as exc:
                self._context.logger.warning("fake source problem: %s", exc.kind)
            await self._context.clock.sleep(self._settings.interval)

    async def doctor(self) -> HealthReport:
        now = self._context.clock.now()
        try:
            _read(self._settings.source_file)
        except _SourceError as exc:
            return HealthReport(
                state=HealthState.DEGRADED,
                detail=exc.detail,
                error_kind=exc.kind,
                checked_at=now,
            )
        return HealthReport(state=HealthState.OK, checked_at=now)


def _factory(settings: BaseModel, context: CollectorContext) -> Collector:
    if not isinstance(settings, FakeSettings):
        raise TypeError("fake collector got foreign settings")
    return FakeCollector(settings, context)


plugin = CollectorPlugin(
    name=NAME,
    api_major=COLLECTOR_API_MAJOR,
    capabilities=frozenset({Capability.WINDOWS, Capability.TODOS}),
    config_model=FakeSettings,
    factory=_factory,
)
