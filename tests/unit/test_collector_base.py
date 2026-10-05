from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from fylgja.collectors.base import (
    CAPABILITY_ENTITY_KINDS,
    COLLECTOR_API_MAJOR,
    Capability,
    Clock,
    Collector,
    CollectorContext,
    CollectorPlugin,
    ReadOnlyPaths,
    RecordSink,
    allowed_entity_kinds,
)
from fylgja.models import EntityKind, HealthReport, HealthState, Record


class _Config(BaseModel):
    enabled: bool = True


class _Sink:
    def __init__(self) -> None:
        self.records: list[Record] = []

    def emit(self, record: Record) -> None:
        self.records.append(record)

    async def emit_committed(self, record: Record) -> None:
        self.records.append(record)


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 10, 5, tzinfo=UTC)

    def monotonic(self) -> float:
        return 1.0

    async def sleep(self, seconds: float) -> None:
        return None


class _Paths:
    data_dir = Path("/data")
    state_dir = Path("/state")
    inbox_dir = Path("/data/hook-inbox/x")


class _MinimalCollector:
    def __init__(self, config: BaseModel, ctx: CollectorContext) -> None:
        self.ctx = ctx
        self.ran = False

    async def run(self) -> None:
        self.ran = True

    async def doctor(self) -> HealthReport:
        return HealthReport(state=HealthState.OK, checked_at=self.ctx.clock.now())


def _factory(config: BaseModel, ctx: CollectorContext) -> Collector:
    return _MinimalCollector(config, ctx)


def _plugin(**overrides: object) -> CollectorPlugin:
    args: dict[str, object] = {
        "name": "demo",
        "api_major": COLLECTOR_API_MAJOR,
        "capabilities": frozenset({Capability.WINDOWS}),
        "config_model": _Config,
        "factory": _factory,
    }
    args.update(overrides)
    return CollectorPlugin(**args)  # type: ignore[arg-type]


def test_api_major_is_one() -> None:
    assert COLLECTOR_API_MAJOR == 1


def test_capability_values() -> None:
    assert {c.name for c in Capability} == {
        "WINDOWS",
        "SESSIONS",
        "TODOS",
        "CONTEXT",
        "PROJECT_STATE",
    }
    assert Capability("windows") is Capability.WINDOWS


@pytest.mark.parametrize("name", ["demo", "a", "my-plugin_2", "0abc", "x_y-z"])
def test_valid_names(name: str) -> None:
    assert _plugin(name=name).name == name


@pytest.mark.parametrize("name", ["", "Demo", "has space", "dot.ted", "a/b", "ünï", "x\n", "a b"])
def test_invalid_names(name: str) -> None:
    with pytest.raises(ValueError, match="invalid plugin name"):
        _plugin(name=name)


def test_non_string_name_rejected() -> None:
    with pytest.raises(ValueError, match="invalid plugin name"):
        _plugin(name=5)


def test_invalid_capability_rejected() -> None:
    with pytest.raises(ValueError, match="invalid capability"):
        _plugin(capabilities=frozenset({"windows"}))
    with pytest.raises(ValueError, match="invalid capability"):
        _plugin(capabilities=frozenset({Capability.WINDOWS, "teapot"}))


def test_other_field_validation() -> None:
    with pytest.raises(TypeError, match="api_major"):
        _plugin(api_major="1")
    with pytest.raises(TypeError, match="api_major"):
        _plugin(api_major=True)
    with pytest.raises(TypeError, match="config_model"):
        _plugin(config_model=dict)
    with pytest.raises(TypeError, match="config_model"):
        _plugin(config_model=_Config())
    with pytest.raises(TypeError, match="factory"):
        _plugin(factory=None)


def test_plugin_is_frozen_and_hashable_fields() -> None:
    plugin = _plugin()
    with pytest.raises(AttributeError):
        plugin.name = "other"  # type: ignore[misc]
    assert plugin.api_major == 1


def test_mapping_covers_every_kind_exactly_once() -> None:
    assert set(CAPABILITY_ENTITY_KINDS) == set(Capability)
    seen: list[EntityKind] = []
    for kinds in CAPABILITY_ENTITY_KINDS.values():
        seen.extend(kinds)
    assert set(seen) == set(EntityKind)
    assert len(seen) == len(set(seen))


def test_mapping_is_read_only_and_specific_kinds() -> None:
    with pytest.raises(TypeError):
        CAPABILITY_ENTITY_KINDS[Capability.TODOS] = frozenset()  # type: ignore[index]
    assert EntityKind.PRESENCE in CAPABILITY_ENTITY_KINDS[Capability.CONTEXT]
    assert EntityKind.USAGE in CAPABILITY_ENTITY_KINDS[Capability.PROJECT_STATE]
    assert EntityKind.BUILD in CAPABILITY_ENTITY_KINDS[Capability.PROJECT_STATE]
    assert EntityKind.SESSION_ACTIVITY in CAPABILITY_ENTITY_KINDS[Capability.SESSIONS]


def test_allowed_kinds() -> None:
    assert allowed_entity_kinds([]) == frozenset()
    plugin = _plugin(capabilities=frozenset({Capability.TODOS, Capability.CONTEXT}))
    assert plugin.allowed_kinds == {
        EntityKind.TODO,
        EntityKind.CONTEXT_ITEM,
        EntityKind.PRESENCE,
    }


def test_protocols_are_satisfied_structurally() -> None:
    assert isinstance(_Sink(), RecordSink)
    assert isinstance(_Clock(), Clock)
    assert isinstance(_Paths(), ReadOnlyPaths)
    assert not isinstance(object(), Collector)


async def test_minimal_collector_satisfies_protocol() -> None:
    ctx = CollectorContext(
        sink=_Sink(),
        clock=_Clock(),
        logger=logging.LoggerAdapter(logging.getLogger("t")),
        paths=_Paths(),
    )
    plugin = _plugin()
    collector = plugin.factory(plugin.config_model(), ctx)
    assert isinstance(collector, Collector)
    await collector.run()
    report = await collector.doctor()
    assert report.state is HealthState.OK
    assert report.checked_at.tzinfo is not None


def test_context_is_frozen() -> None:
    ctx = CollectorContext(
        sink=_Sink(),
        clock=_Clock(),
        logger=logging.LoggerAdapter(logging.getLogger("t")),
        paths=_Paths(),
    )
    with pytest.raises(AttributeError):
        ctx.sink = _Sink()  # type: ignore[misc]
