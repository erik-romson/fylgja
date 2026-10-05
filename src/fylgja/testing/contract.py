"""The collector contract (``PLAN.md`` §4.9, ``PLAN-final-plan.md`` WP1.5).

A plugin's contract module subclasses :class:`CollectorContract` and supplies fixtures::

    class TestAerospaceContract(CollectorContract):
        @pytest.fixture
        def plugin_name(self) -> str:
            return "aerospace"

        @pytest.fixture
        def working_source(self) -> ContractSource:
            return ContractSource(arrange=lambda env: env.process_runner.script("aerospace", ...))

        @pytest.fixture
        def broken_source(self) -> ContractSource:
            return ContractSource(config={"command": "/nonexistent"})

The base class then runs the five checks of ``PLAN.md`` §4.9 against the registered plugin:

1. discoverable through the registry, with a unique name, a compatible API and valid capabilities;
2. enabled and disabled through configuration, with no source-specific daemon code;
3. emits schema-valid records with a stable source, entity identity, ``observed_at`` and
   ``dedup_key``;
4. shuts down cleanly on cancellation and releases its resources;
5. reports a broken source as ``degraded`` health while a neighbouring collector keeps running.

Everything runs on :class:`~fylgja.testing.fakes.FakeClock`; no real time passes and no external
tool is needed.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import BaseModel, ConfigDict

from fylgja.collectors.base import (
    COLLECTOR_API_MAJOR,
    PLUGIN_NAME_PATTERN,
    Capability,
    Collector,
    CollectorContext,
    CollectorPlugin,
)
from fylgja.collectors.context import build_context
from fylgja.collectors.registry import (
    DiscoveredEntry,
    RegisteredCollector,
    discover,
    load_registry,
)
from fylgja.collectors.supervisor import BackoffPolicy, Supervisor
from fylgja.config import Config, parse_config
from fylgja.models import (
    EntityKind,
    HealthReport,
    HealthState,
    PresenceEvent,
    PresencePayload,
    Record,
    parse_record,
)
from fylgja.testing.fakes import FakeClock, FakeFS, FakeProcessRunner, RecordingSink

__all__ = [
    "CollectorContract",
    "ContractEnv",
    "ContractSource",
]

DRIVE_SECONDS: Final = 60
"""Virtual seconds the contract lets a collector run, in one-second steps."""
MAX_FUTURE_SKEW: Final = timedelta(minutes=5)
_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")
_NEIGHBOUR: Final = "contract_neighbour"


@dataclass
class ContractEnv:
    """The fakes of one contract run. ``arrange`` hooks get this to build the fake world."""

    tmp_path: Path
    monkeypatch: pytest.MonkeyPatch
    clock: FakeClock = field(default_factory=FakeClock)
    sink: RecordingSink = field(default_factory=RecordingSink)
    process_runner: FakeProcessRunner = field(default_factory=FakeProcessRunner)
    fs: FakeFS = field(default_factory=FakeFS)

    def fresh(self) -> ContractEnv:
        """A new environment with new fakes that shares ``tmp_path`` and ``monkeypatch``."""
        return ContractEnv(self.tmp_path, self.monkeypatch)


@dataclass(frozen=True)
class ContractSource:
    """One fake source for the plugin: a working one or a broken one.

    ``config`` holds ``[collectors.<name>]`` values. ``arrange`` prepares the fake world (script
    the process runner, write files under ``env.tmp_path``, patch seams through
    ``env.monkeypatch``) and may return more configuration, for example paths under
    ``env.tmp_path``; what it returns wins over ``config``.
    """

    label: str = "source"
    config: Mapping[str, Any] = field(default_factory=dict)
    arrange: Callable[[ContractEnv], Mapping[str, Any] | None] | None = None


class _NeighbourSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _NeighbourEntryPoint:
    name = _NEIGHBOUR
    value = "fylgja.testing.contract:neighbour"
    dist = None

    def load(self) -> object:  # pragma: no cover - the contract builds the registration itself
        raise NotImplementedError


class _Neighbour:
    """A collector that works: it emits a presence event every second."""

    def __init__(self, context: CollectorContext) -> None:
        self._context = context

    async def run(self) -> None:
        while True:
            self._context.sink.emit(
                PresenceEvent(
                    source=_NEIGHBOUR,
                    entity_id="user",
                    observed_at=self._context.clock.now(),
                    payload=PresencePayload(state="active"),
                )
            )
            await self._context.clock.sleep(1.0)

    async def doctor(self) -> HealthReport:
        return HealthReport(
            state=HealthState.OK, checked_at=self._context.clock.now(), detail="neighbour ok"
        )


def _neighbour_factory(settings: BaseModel, context: CollectorContext) -> Collector:
    return _Neighbour(context)


def _neighbour_registration() -> RegisteredCollector:
    plugin = CollectorPlugin(
        name=_NEIGHBOUR,
        api_major=COLLECTOR_API_MAJOR,
        capabilities=frozenset({Capability.CONTEXT}),
        config_model=_NeighbourSettings,
        factory=_neighbour_factory,
    )
    entry = DiscoveredEntry(
        name=_NEIGHBOUR,
        value=_NeighbourEntryPoint.value,
        distribution=None,
        version=None,
        entry_point=_NeighbourEntryPoint(),
    )
    return RegisteredCollector(entry, enabled=True, plugin=plugin, settings=_NeighbourSettings())


def _signature(records: Sequence[Record]) -> list[tuple[str, str, str, str, str]]:
    return [(r.source, r.record_type, r.entity_kind, r.entity_id, r.dedup_key) for r in records]


class CollectorContract:
    """Subclass in ``tests/contract/test_<plugin>_contract.py`` and override the fixtures."""

    # -- fixtures the subclass supplies -------------------------------------------------------

    @pytest.fixture
    def plugin_name(self) -> str:
        raise NotImplementedError("override the plugin_name fixture")

    @pytest.fixture
    def working_source(self) -> ContractSource:
        raise NotImplementedError("override the working_source fixture")

    @pytest.fixture
    def broken_source(self) -> ContractSource | Sequence[ContractSource]:
        """A source that is missing a command or file, denies permission or is malformed.

        Return a sequence to check several of them.
        """
        raise NotImplementedError("override the broken_source fixture")

    @pytest.fixture
    def config_overrides(self) -> Mapping[str, Any]:
        """``[collectors.<name>]`` values applied to every run, working or broken."""
        return {}

    # -- fixtures of the contract itself ------------------------------------------------------

    @pytest.fixture
    def contract_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ContractEnv:
        return ContractEnv(tmp_path, monkeypatch)

    # -- helpers --------------------------------------------------------------------------------

    @staticmethod
    def _config(name: str, enabled: bool, settings: Mapping[str, Any]) -> Config:
        table = {"enabled": enabled, **settings}
        return parse_config({"collectors": {name: table}}, source="contract")

    @staticmethod
    def _settings(
        env: ContractEnv, source: ContractSource, overrides: Mapping[str, Any]
    ) -> dict[str, Any]:
        arranged = {} if source.arrange is None else (source.arrange(env) or {})
        return {**overrides, **source.config, **arranged}

    def _registered(self, name: str, config: Config) -> RegisteredCollector:
        matches = [item for item in load_registry(config) if item.name == name]
        assert len(matches) == 1, f"expected one registration of {name!r}, got {len(matches)}"
        return matches[0]

    def _build(
        self,
        env: ContractEnv,
        name: str,
        source: ContractSource,
        overrides: Mapping[str, Any],
    ) -> tuple[RegisteredCollector, Collector]:
        settings = self._settings(env, source, overrides)
        item = self._registered(name, self._config(name, True, settings))
        assert item.runnable, f"{name} is not runnable: {item.health}"
        assert item.plugin is not None
        assert item.settings is not None
        context = build_context(name, env.sink, clock=env.clock)
        return item, item.plugin.factory(item.settings, context)

    @staticmethod
    async def _drive(env: ContractEnv, seconds: int = DRIVE_SECONDS) -> None:
        for _ in range(seconds):
            await env.clock.advance(1.0)

    @staticmethod
    def _sources(broken: ContractSource | Sequence[ContractSource]) -> list[ContractSource]:
        return [broken] if isinstance(broken, ContractSource) else list(broken)

    async def _records_of_run(
        self,
        env: ContractEnv,
        name: str,
        source: ContractSource,
        overrides: Mapping[str, Any],
    ) -> list[Record]:
        _, collector = self._build(env, name, source, overrides)
        task = asyncio.create_task(collector.run(), name=f"contract:{name}")
        try:
            await self._drive(env)
        finally:
            task.cancel()
            await asyncio.wait({task}, timeout=5)
        assert task.done(), "run() did not stop when cancelled"
        return list(env.sink.records)

    # -- check 1: discoverable --------------------------------------------------------------

    def test_1_discoverable_with_unique_name_compatible_api_and_valid_capabilities(
        self, plugin_name: str, config_overrides: Mapping[str, Any]
    ) -> None:
        assert PLUGIN_NAME_PATTERN.fullmatch(plugin_name), f"invalid plugin name {plugin_name!r}"
        discovery = discover()
        claimed = [e for e in (*discovery.entries, *discovery.duplicates) if e.name == plugin_name]
        assert claimed, f"no fylgja.collectors entry point named {plugin_name!r}"
        assert len(claimed) == 1, f"entry point name {plugin_name!r} is claimed more than once"
        assert not any(e.name == plugin_name for e in discovery.duplicates)

        item = self._registered(plugin_name, self._config(plugin_name, True, config_overrides))
        assert item.health is None, f"{plugin_name} failed to load: {item.health}"
        plugin = item.plugin
        assert plugin is not None
        assert isinstance(plugin, CollectorPlugin)
        assert plugin.name == plugin_name
        assert plugin.api_major == COLLECTOR_API_MAJOR
        assert plugin.capabilities, "a plugin declares at least one capability"
        assert all(isinstance(c, Capability) for c in plugin.capabilities)
        assert plugin.allowed_kinds

    # -- check 2: enable and disable --------------------------------------------------------

    async def test_2_enabled_and_disabled_through_config_without_source_specific_code(
        self,
        plugin_name: str,
        working_source: ContractSource,
        config_overrides: Mapping[str, Any],
        contract_env: ContractEnv,
    ) -> None:
        env = contract_env
        settings = self._settings(env, working_source, config_overrides)

        off = self._registered(plugin_name, self._config(plugin_name, False, settings))
        assert not off.enabled
        assert not off.runnable
        supervisor = Supervisor([off], env.sink, clock=env.clock, rng=random.Random(0))
        supervisor.start()
        assert supervisor.task(plugin_name) is None
        assert supervisor.health()[plugin_name].state is HealthState.DISABLED
        await supervisor.shutdown()

        on = self._registered(plugin_name, self._config(plugin_name, True, settings))
        assert on.enabled
        assert on.runnable, f"{plugin_name} is not runnable when enabled: {on.health}"
        supervisor = Supervisor([on], env.sink, clock=env.clock, rng=random.Random(0))
        supervisor.start()
        try:
            await self._drive(env, 3)
            task = supervisor.task(plugin_name)
            assert task is not None
            assert not task.done(), "the collector stopped while enabled"
            assert supervisor.health()[plugin_name].restarts == 0
        finally:
            await supervisor.shutdown()
        assert supervisor.health()[plugin_name].state is HealthState.DISABLED

    # -- check 3: schema-valid records ------------------------------------------------------

    async def test_3_emits_schema_valid_records_with_stable_identity(
        self,
        plugin_name: str,
        working_source: ContractSource,
        config_overrides: Mapping[str, Any],
        contract_env: ContractEnv,
    ) -> None:
        env = contract_env
        item, collector = self._build(env, plugin_name, working_source, config_overrides)
        assert item.plugin is not None
        allowed = item.plugin.allowed_kinds
        task = asyncio.create_task(collector.run(), name=f"contract:{plugin_name}")
        try:
            await self._drive(env)
            report = await collector.doctor()
        finally:
            task.cancel()
            await asyncio.wait({task}, timeout=5)
        assert report.state is HealthState.OK, f"working source is {report.state}: {report.detail}"
        records = list(env.sink.records)
        assert records, "a working source produced no records"
        for record in records:
            assert record.source == plugin_name, "source must be the plugin name"
            assert record.entity_kind in allowed, f"{record.entity_kind} is not a declared kind"
            assert isinstance(record.entity_kind, EntityKind)
            assert record.entity_id
            assert record.observed_at.utcoffset() is not None
            assert record.observed_at <= env.clock.now() + MAX_FUTURE_SKEW
            assert _SHA256_HEX.fullmatch(record.dedup_key), "dedup_key must be a SHA-256 hex digest"
            assert parse_record(record.model_dump(mode="json")) == record, "not schema-valid"

        # A second identical run gives the same identity and dedup keys.
        second = env.fresh()
        again = await self._records_of_run(second, plugin_name, working_source, config_overrides)
        assert _signature(again) == _signature(records), "identity or dedup_key is not stable"

    # -- check 4: clean cancellation --------------------------------------------------------

    async def test_4_shuts_down_cleanly_on_cancellation_and_releases_resources(
        self,
        plugin_name: str,
        working_source: ContractSource,
        config_overrides: Mapping[str, Any],
        contract_env: ContractEnv,
    ) -> None:
        env = contract_env
        _, collector = self._build(env, plugin_name, working_source, config_overrides)
        before = asyncio.all_tasks()
        task = asyncio.create_task(collector.run(), name=f"contract:{plugin_name}")
        await self._drive(env, 5)
        assert not task.done(), "run() returned before it was cancelled"

        task.cancel()
        done, pending = await asyncio.wait({task}, timeout=5)
        assert not pending, "run() ignored cancellation"
        assert done == {task}
        assert task.cancelled() or task.exception() is None, "run() raised on cancellation"

        await env.clock.advance(1.0)
        leaked = [t for t in asyncio.all_tasks() - before - {task} if not t.done()]
        assert not leaked, f"tasks outlived the collector: {[t.get_name() for t in leaked]}"
        assert env.process_runner.active == 0, "a subprocess was still running after cancel"
        count = len(env.sink.records)
        await self._drive(env, 10)
        assert len(env.sink.records) == count, "records arrived after cancellation"
        assert env.clock.pending == 0, "a sleep was left pending after cancellation"

    # -- check 5: degraded health, neighbour keeps running ------------------------------------

    async def test_5_broken_source_degrades_health_while_a_neighbour_keeps_running(
        self,
        plugin_name: str,
        broken_source: ContractSource | Sequence[ContractSource],
        config_overrides: Mapping[str, Any],
        contract_env: ContractEnv,
    ) -> None:
        sources = self._sources(broken_source)
        assert sources, "broken_source gave no source"
        for source in sources:
            await self._check_degraded(contract_env.fresh(), plugin_name, source, config_overrides)

    async def _check_degraded(
        self,
        env: ContractEnv,
        name: str,
        source: ContractSource,
        overrides: Mapping[str, Any],
    ) -> None:
        label = f"{name}/{source.label}"
        settings = self._settings(env, source, overrides)
        target = self._registered(name, self._config(name, True, settings))
        assert target.runnable, f"{label}: not runnable: {target.health}"
        policy = BackoffPolicy(probe_delay=1.0, probe_interval=5.0, probe_timeout=10.0)
        supervisor = Supervisor(
            [target, _neighbour_registration()],
            env.sink,
            clock=env.clock,
            rng=random.Random(0),
            policy=policy,
        )
        supervisor.start()
        try:
            await self._drive(env, 12)
            health = supervisor.health()
            assert health[name].state is HealthState.DEGRADED, (
                f"{label}: expected degraded, got {health[name].state} ({health[name].detail})"
            )
            assert health[name].last_error_kind, f"{label}: degraded without an error_kind"
            assert health[name].restarts == 0, f"{label}: crashed instead of degrading"
            task = supervisor.task(name)
            assert task is not None
            assert not task.done(), f"{label}: the collector stopped"

            seen = len(env.sink.of_source(_NEIGHBOUR))
            await self._drive(env, 6)
            assert len(env.sink.of_source(_NEIGHBOUR)) > seen, f"{label}: the neighbour stalled"
            assert supervisor.health()[_NEIGHBOUR].state is HealthState.OK
            assert supervisor.health()[_NEIGHBOUR].restarts == 0
        finally:
            await supervisor.shutdown()
