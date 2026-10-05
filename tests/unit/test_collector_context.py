"""The per-plugin collector context: logger fields, paths and the inbox boundary."""

from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime

import pytest

from fylgja import paths
from fylgja.collectors.base import Clock, CollectorContext, ReadOnlyPaths, RecordSink
from fylgja.collectors.context import (
    LOGGER_NAME,
    PluginPaths,
    SystemClock,
    build_context,
    plugin_paths,
)
from fylgja.models import Record

TIMEOUT = pytest.mark.timeout(10)


class NullSink:
    def emit(self, record: Record) -> None:
        return None

    async def emit_committed(self, record: Record) -> None:
        return None


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)

    def monotonic(self) -> float:
        return 5.0

    async def sleep(self, seconds: float) -> None:
        return None


@TIMEOUT
def test_context_carries_the_injected_sink_and_clock() -> None:
    sink = NullSink()
    clock = FixedClock()
    context = build_context("alpha", sink, clock=clock)
    assert isinstance(context, CollectorContext)
    assert context.sink is sink
    assert context.clock is clock
    assert isinstance(context.sink, RecordSink)
    assert isinstance(context.clock, Clock)


@TIMEOUT
def test_default_clock_is_the_system_clock() -> None:
    context = build_context("alpha", NullSink())
    assert isinstance(context.clock, SystemClock)
    assert context.clock.now().tzinfo is not None
    first = context.clock.monotonic()
    assert context.clock.monotonic() >= first


@TIMEOUT
async def test_system_clock_sleep_returns() -> None:
    await SystemClock().sleep(0)


@TIMEOUT
def test_logger_stamps_the_collector_name(caplog: pytest.LogCaptureFixture) -> None:
    context = build_context("alpha", NullSink())
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        context.logger.info("hello")
        build_context("beta", NullSink()).logger.info("hi")
    names = [r.collector for r in caplog.records if r.name == LOGGER_NAME]  # type: ignore[attr-defined]
    assert names == ["alpha", "beta"]


@TIMEOUT
def test_logger_extra_survives_a_call_site_extra(caplog: pytest.LogCaptureFixture) -> None:
    context = build_context("alpha", NullSink())
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        context.logger.info("hello", extra={"other": 1})
    record = caplog.records[-1]
    assert record.collector == "alpha"  # type: ignore[attr-defined]
    assert record.other == 1  # type: ignore[attr-defined]


@TIMEOUT
def test_custom_base_logger_is_used(caplog: pytest.LogCaptureFixture) -> None:
    base = logging.getLogger("fylgja.test-base")
    context = build_context("alpha", NullSink(), logger=base)
    with caplog.at_level(logging.INFO, logger="fylgja.test-base"):
        context.logger.info("x")
    assert caplog.records[-1].name == "fylgja.test-base"


@TIMEOUT
def test_paths_come_from_fylgja_paths() -> None:
    context = build_context("alpha", NullSink())
    assert context.paths.data_dir == paths.data_dir()
    assert context.paths.state_dir == paths.state_dir()
    assert context.paths.inbox_dir == paths.inbox_dir("alpha")
    assert isinstance(context.paths, ReadOnlyPaths)


@TIMEOUT
def test_paths_follow_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    other = tmp_path_factory.mktemp("elsewhere")
    monkeypatch.setenv("FYLGJA_DATA_DIR", str(other))
    assert plugin_paths("alpha").inbox_dir == other / "hook-inbox" / "alpha"


@TIMEOUT
def test_inbox_source_overrides_the_plugin_name() -> None:
    assert plugin_paths("alpha", inbox_source="claude").inbox_dir == paths.inbox_dir("claude")


@TIMEOUT
def test_each_plugin_gets_only_its_own_inbox() -> None:
    alpha = build_context("alpha", NullSink()).paths
    beta = build_context("beta", NullSink()).paths
    assert alpha.inbox_dir != beta.inbox_dir
    assert alpha.inbox_dir.parent == beta.inbox_dir.parent
    assert alpha.inbox_dir.name == "alpha"


@TIMEOUT
def test_paths_object_is_immutable_and_holds_no_other_inbox() -> None:
    alpha = plugin_paths("alpha")
    assert isinstance(alpha, PluginPaths)
    with pytest.raises(dataclasses.FrozenInstanceError):
        alpha.inbox_dir = paths.inbox_dir("beta")  # type: ignore[misc]
    with pytest.raises(AttributeError):
        alpha.extra = 1  # type: ignore[attr-defined]
    held = {f.name: getattr(alpha, f.name) for f in dataclasses.fields(alpha)}
    inboxes = [v for v in held.values() if v == paths.inbox_dir("beta")]
    assert inboxes == []
    assert set(held) == {"plugin", "data_dir", "state_dir", "inbox_dir"}


@TIMEOUT
@pytest.mark.parametrize("name", ["../beta", "a/b", "", "UPPER", "a b"])
def test_invalid_plugin_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="invalid plugin name"):
        plugin_paths(name)


@TIMEOUT
def test_nothing_is_created_on_disk() -> None:
    context = build_context("alpha", NullSink())
    assert not context.paths.inbox_dir.exists()
