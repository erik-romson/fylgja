"""The ingest gate: each rejection reason, the degraded threshold under a fake clock, privacy
drops and health merging (WP1.4, §3.3). The writer is a recording stub; no database is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from fylgja.collectors.base import Capability, CollectorPlugin, RecordSink
from fylgja.collectors.supervisor import CollectorHealth
from fylgja.core.ingest import (
    DEGRADED_ERROR_KIND,
    IngestGate,
    PluginSink,
    PluginStats,
    RecordRejected,
    Rejection,
)
from fylgja.core.writer import WriterClosed
from fylgja.models import (
    EntityKind,
    FocusEvent,
    FocusPayload,
    HealthState,
    Record,
    SessionActivity,
    SessionActivityEvent,
    SnapshotItem,
    WindowEvent,
    WindowPayload,
    WindowSnapshot,
)
from fylgja.privacy import PrivacyFilter
from fylgja.store.rows import HealthRow

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.wall = T0
        self.mono = 1000.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - unused here
        raise AssertionError("the gate never sleeps")

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)


class StubWriter:
    def __init__(self) -> None:
        self.submitted: list[Record] = []
        self.committed: list[Record] = []
        self.health: list[HealthRow] = []
        self.received: list[datetime | None] = []
        self.closed = False

    def submit(self, record: Record, *, received_at: datetime | None = None) -> None:
        if self.closed:
            raise WriterClosed("stopped")
        self.submitted.append(record)
        self.received.append(received_at)

    async def submit_committed(
        self, record: Record, *, received_at: datetime | None = None
    ) -> object:
        self.committed.append(record)
        return "stored"

    def save_health(self, row: HealthRow) -> None:
        if self.closed:
            raise WriterClosed("stopped")
        self.health.append(row)


class EmptyConfig(BaseModel):
    pass


def plugin(name: str, *caps: Capability) -> CollectorPlugin:
    return CollectorPlugin(
        name=name,
        api_major=1,
        capabilities=frozenset(caps),
        config_model=EmptyConfig,
        factory=lambda settings, context: None,  # type: ignore[arg-type,return-value]
    )


def window(source: str = "aero", observed_at: datetime = T0, **payload: Any) -> WindowEvent:
    return WindowEvent(
        source=source, entity_id="w1", observed_at=observed_at, payload=WindowPayload(**payload)
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def writer() -> StubWriter:
    return StubWriter()


@pytest.fixture
def gate(writer: StubWriter, clock: FakeClock) -> IngestGate:
    return IngestGate(
        writer,
        [plugin("aero", Capability.WINDOWS), plugin("claude", Capability.SESSIONS)],
        clock=clock,
        privacy=PrivacyFilter(
            redact_patterns=[r"sk-[A-Za-z0-9]{8,}"], ignore_bundle_ids=["com.example.vault"]
        ),
    )


# --------------------------------------------------------------------------------------------
# Acceptance and each rejection reason
# --------------------------------------------------------------------------------------------


def test_a_valid_record_is_queued_with_its_receive_time(
    gate: IngestGate, writer: StubWriter
) -> None:
    record = window(title="ok")
    gate.sink("aero").emit(record)
    assert writer.submitted == [record]
    assert writer.received == [T0]
    assert gate.stats("aero").accepted == 1
    assert gate.stats("aero").rejected_total == 0


def test_source_must_equal_the_plugin_name(gate: IngestGate, writer: StubWriter) -> None:
    with pytest.raises(RecordRejected) as rejected:
        gate.check("aero", window(source="claude"))
    assert rejected.value.reason is Rejection.SOURCE
    assert rejected.value.plugin == "aero"
    assert gate.stats("aero").rejected[Rejection.SOURCE] == 1
    assert writer.submitted == []


def test_entity_kind_must_be_allowed_by_the_capabilities(gate: IngestGate) -> None:
    activity = SessionActivityEvent(
        source="aero", entity_id="c:1", observed_at=T0, payload=SessionActivity()
    )
    with pytest.raises(RecordRejected) as rejected:
        gate.check("aero", activity)
    assert rejected.value.reason is Rejection.ENTITY_KIND
    with pytest.raises(RecordRejected):
        gate.check("claude", window(source="claude"))
    assert gate.stats("claude").rejected[Rejection.ENTITY_KIND] == 1


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(
            WindowEvent.model_construct(
                source="aero", entity_id="", observed_at=T0, payload=WindowPayload()
            ),
            id="constructed-without-validation",
        ),
        pytest.param(
            WindowEvent.model_construct(
                source="aero",
                entity_id="w1",
                observed_at=T0,
                payload={"title": 3},
                record_type="event",
            ),
            id="payload-of-the-wrong-type",
        ),
        pytest.param(
            {"record_type": "event", "entity_kind": "window", "source": "aero"}, id="bad-dict"
        ),
        pytest.param(object(), id="not-a-record"),
        pytest.param(WindowPayload(title="x"), id="a-payload-not-a-record"),
    ],
)
def test_a_record_that_fails_its_schema_is_rejected(gate: IngestGate, bad: object) -> None:
    with pytest.raises(RecordRejected) as rejected:
        gate.check("aero", bad)
    assert rejected.value.reason is Rejection.SCHEMA


def test_a_mutated_record_is_revalidated(gate: IngestGate) -> None:
    record = window()
    object.__setattr__(record, "entity_id", "")
    with pytest.raises(RecordRejected) as rejected:
        gate.check("aero", record)
    assert rejected.value.reason is Rejection.SCHEMA


def test_a_valid_dict_is_accepted(gate: IngestGate) -> None:
    data = window(title="from a dict").model_dump(mode="json")
    accepted = gate.check("aero", data)
    assert isinstance(accepted, WindowEvent)
    assert accepted.dedup_key == data["dedup_key"]  # the plugin's dedup key is kept


@pytest.mark.parametrize(
    ("offset", "accepted"),
    [
        pytest.param(-timedelta(days=30), True, id="exactly-30-days-old"),
        pytest.param(-timedelta(days=30, microseconds=1), False, id="older-than-30-days"),
        pytest.param(timedelta(minutes=5), True, id="exactly-5-minutes-ahead"),
        pytest.param(timedelta(minutes=5, microseconds=1), False, id="more-than-5-minutes-ahead"),
        pytest.param(timedelta(0), True, id="now"),
    ],
)
def test_timestamp_window(gate: IngestGate, offset: timedelta, accepted: bool) -> None:
    record = window(observed_at=T0 + offset)
    if accepted:
        assert gate.check("aero", record) == record
    else:
        with pytest.raises(RecordRejected) as rejected:
            gate.check("aero", record)
        assert rejected.value.reason is Rejection.TIMESTAMP


def test_emit_never_raises_and_emit_committed_does(gate: IngestGate, writer: StubWriter) -> None:
    sink = gate.sink("aero")
    sink.emit(window(source="claude"))
    assert gate.stats("aero").rejected[Rejection.SOURCE] == 1
    assert writer.submitted == []


async def test_emit_committed_raises_for_rejections(gate: IngestGate, writer: StubWriter) -> None:
    sink = gate.sink("aero")
    with pytest.raises(RecordRejected):
        await sink.emit_committed(window(observed_at=T0 + timedelta(hours=1)))
    record = window(title="kept")
    await sink.emit_committed(record)
    assert writer.committed == [record]


def test_unknown_plugins(gate: IngestGate) -> None:
    with pytest.raises(ValueError, match="unknown plugin"):
        gate.sink("nobody")
    with pytest.raises(ValueError, match="unknown plugin"):
        gate.check("nobody", window())
    with pytest.raises(ValueError, match="already registered"):
        gate.add_plugin(plugin("aero", Capability.WINDOWS))


def test_the_sink_is_a_record_sink(gate: IngestGate) -> None:
    sink = gate.sink("claude")
    assert isinstance(sink, PluginSink)
    assert isinstance(sink, RecordSink)
    assert sink.name == "claude"


# --------------------------------------------------------------------------------------------
# Privacy in the gate
# --------------------------------------------------------------------------------------------


def test_privacy_filter_runs_before_the_writer(gate: IngestGate, writer: StubWriter) -> None:
    gate.emit("aero", window(title="token sk-ABCDEFGHIJKL here"))
    assert writer.submitted[0].payload.title == "token [redacted] here"  # type: ignore[union-attr]
    gate.emit("aero", window(app_bundle_id="com.example.vault", title="vault"))
    assert len(writer.submitted) == 1
    stats = gate.stats("aero")
    assert (stats.accepted, stats.dropped, stats.rejected_total) == (1, 1, 0)


async def test_emit_committed_of_a_dropped_record_returns_at_once(
    gate: IngestGate, writer: StubWriter
) -> None:
    await gate.sink("aero").emit_committed(window(app_bundle_id="com.example.vault"))
    assert writer.committed == []


def test_snapshot_items_are_filtered_one_by_one(gate: IngestGate, writer: StubWriter) -> None:
    snapshot = WindowSnapshot(
        source="aero",
        entity_id="all",
        observed_at=T0,
        complete=True,
        items=(
            SnapshotItem(entity_id="w1", payload=WindowPayload(title="editor")),
            SnapshotItem(entity_id="w2", payload=WindowPayload(app_bundle_id="com.example.vault")),
        ),
    )
    gate.emit("aero", snapshot)
    (stored,) = writer.submitted
    assert isinstance(stored, WindowSnapshot)
    assert [item.entity_id for item in stored.items] == ["w1"]


def test_a_stopped_writer_counts_lost_records(gate: IngestGate, writer: StubWriter) -> None:
    writer.closed = True
    gate.emit("aero", window())
    assert gate.stats("aero").lost == 1
    gate.observe_health(CollectorHealth("aero", HealthState.OK, since=T0))  # logged, not raised


# --------------------------------------------------------------------------------------------
# Counts per entity kind and record type (docs/decisions/0048)
# --------------------------------------------------------------------------------------------


def _window_snapshot(observed_at: datetime = T0) -> WindowSnapshot:
    return WindowSnapshot(
        source="aero",
        entity_id="all",
        observed_at=observed_at,
        complete=True,
        items=(SnapshotItem(entity_id="w1", payload=WindowPayload(title="editor")),),
    )


def _focus(observed_at: datetime = T0) -> FocusEvent:
    return FocusEvent(
        source="aero",
        entity_id="focus",
        observed_at=observed_at,
        payload=FocusPayload(window_id="w1", workspace="1"),
    )


def test_accepted_records_are_counted_per_kind_and_record_type(
    gate: IngestGate, clock: FakeClock
) -> None:
    gate.emit("aero", _window_snapshot())
    clock.advance(5)
    gate.emit("aero", _focus(T0 + timedelta(seconds=5)))
    gate.emit("aero", window(observed_at=T0 + timedelta(seconds=5)))
    clock.advance(5)
    gate.emit("aero", _window_snapshot(T0 + timedelta(seconds=10)))
    stats = gate.stats("aero")
    assert stats.accepted == 4
    assert stats.kinds == {
        (EntityKind.WINDOW, "snapshot"): 2,
        (EntityKind.FOCUS, "event"): 1,
        (EntityKind.WINDOW, "event"): 1,
    }
    assert stats.last_at == {
        (EntityKind.WINDOW, "snapshot"): T0 + timedelta(seconds=10),
        (EntityKind.FOCUS, "event"): T0 + timedelta(seconds=5),
        (EntityKind.WINDOW, "event"): T0 + timedelta(seconds=5),
    }
    assert gate.stats("claude").kinds == {}


def test_dropped_and_rejected_records_do_not_count_as_kinds(
    gate: IngestGate, writer: StubWriter
) -> None:
    gate.emit("aero", window(app_bundle_id="com.example.vault"))  # dropped by the privacy filter
    gate.emit("aero", window(source="claude"))  # rejected: source
    gate.emit("aero", window(observed_at=T0 + timedelta(hours=1)))  # rejected: timestamp
    stats = gate.stats("aero")
    assert (stats.accepted, stats.dropped, stats.rejected_total) == (0, 1, 2)
    assert stats.kinds == {}
    assert stats.last_at == {}
    writer.closed = True
    gate.emit("aero", window())  # accepted and counted, then lost by the stopped writer
    assert stats.lost == 1
    assert stats.kinds == {(EntityKind.WINDOW, "event"): 1}


def test_all_stats_is_a_read_only_view_of_every_plugin(gate: IngestGate) -> None:
    gate.emit("aero", window())
    every = gate.all_stats()
    assert set(every) == {"aero", "claude"}
    assert every["aero"] is gate.stats("aero")
    assert every["aero"].accepted == 1
    with pytest.raises(TypeError):
        every["other"] = PluginStats()  # type: ignore[index]


# --------------------------------------------------------------------------------------------
# Degraded threshold
# --------------------------------------------------------------------------------------------


def reject(gate: IngestGate, times: int) -> None:
    for _ in range(times):
        gate.emit("aero", window(source="claude"))


def test_more_than_20_rejections_in_a_minute_degrade_the_plugin(
    gate: IngestGate, writer: StubWriter, clock: FakeClock
) -> None:
    reject(gate, 20)
    assert not gate.is_degraded("aero")
    assert writer.health == []
    clock.advance(30)
    reject(gate, 1)
    assert gate.is_degraded("aero")
    (row,) = writer.health
    assert (row.name, row.state, row.last_error_kind) == ("aero", "degraded", DEGRADED_ERROR_KIND)
    assert row.since == clock.now()
    assert not gate.is_degraded("claude")  # counted per plugin


def test_rejections_spread_over_more_than_a_minute_do_not_degrade(
    gate: IngestGate, clock: FakeClock
) -> None:
    reject(gate, 20)
    clock.advance(60)  # the first 20 fall out of the window exactly now
    reject(gate, 20)
    assert not gate.is_degraded("aero")
    assert gate.stats("aero").rejected_total == 40


def test_a_degraded_plugin_recovers_after_a_quiet_minute(
    gate: IngestGate, writer: StubWriter, clock: FakeClock
) -> None:
    reject(gate, 21)
    assert gate.is_degraded("aero")
    clock.advance(59)
    gate.refresh()
    assert gate.is_degraded("aero")
    clock.advance(1)
    gate.refresh()
    assert not gate.is_degraded("aero")
    assert [row.state for row in writer.health] == ["degraded", "ok"]


def test_a_valid_record_also_re_evaluates_the_window(
    gate: IngestGate, writer: StubWriter, clock: FakeClock
) -> None:
    reject(gate, 21)
    clock.advance(61)
    gate.emit("aero", window(observed_at=clock.now()))
    assert not gate.is_degraded("aero")


def test_degraded_merges_with_supervisor_health(
    gate: IngestGate, writer: StubWriter, clock: FakeClock
) -> None:
    healthy = CollectorHealth("aero", HealthState.OK, since=T0, last_ok_at=T0, restarts=2)
    gate.observe_health(healthy)
    assert writer.health[-1].state == "ok"
    clock.advance(5)
    reject(gate, 21)
    merged = gate.merged_health("aero")
    assert merged is not None
    assert merged.state is HealthState.DEGRADED
    assert merged.restarts == 2
    assert merged.last_ok_at == T0
    assert writer.health[-1].state == "degraded"
    failed = CollectorHealth("aero", HealthState.FAILED, since=clock.now(), last_error_kind="boom")
    gate.observe_health(failed)
    assert writer.health[-1].state == "failed"  # a worse supervisor state wins
    assert writer.health[-1].last_error_kind == "boom"
    clock.advance(61)
    gate.refresh()
    assert writer.health[-1].state == "failed"
    gate.observe_health(healthy)
    assert writer.health[-1].state == "ok"


def test_merged_health_of_an_unknown_name_is_none(gate: IngestGate) -> None:
    assert gate.merged_health("nobody") is None
    assert gate.merged_health("aero") is not None
