"""Presence logic under ``FakeClock``: thresholds, transitions and the focus gate (WP5.1, A17)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from presence_support import FakeSignals, event_names, make_collector
from pydantic import ValidationError

from fylgja.collectors.presence import parse
from fylgja.collectors.presence.adapter import SignalError
from fylgja.collectors.presence.collector import PresenceCollector, PresenceSettings
from fylgja.models import (
    ContextItemEvent,
    EntityKind,
    HealthState,
    PresenceEvent,
    parse_record,
)
from fylgja.testing.fakes import EPOCH, FakeClock, settle

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
THRESHOLD = 300.0


def at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


# -- idle ---------------------------------------------------------------------------------------


class TestIdleTracker:
    def test_exact_boundary_below_is_active_and_at_threshold_is_idle(self) -> None:
        tracker = parse.IdleTracker(THRESHOLD)
        assert tracker.update(THRESHOLD - 0.001, NOW) is None
        assert not tracker.idle
        started = tracker.update(THRESHOLD, NOW)
        assert started == parse.Transition("idle.start", NOW)
        assert tracker.idle

    def test_start_is_stamped_with_the_moment_the_threshold_was_crossed(self) -> None:
        tracker = parse.IdleTracker(THRESHOLD)
        started = tracker.update(THRESHOLD + 12.5, NOW)
        assert started is not None
        assert started.at == NOW - timedelta(seconds=12.5)

    def test_staying_idle_emits_nothing_more(self) -> None:
        tracker = parse.IdleTracker(THRESHOLD)
        assert tracker.update(400, NOW) is not None
        assert tracker.update(415, at(15)) is None
        assert tracker.update(THRESHOLD, at(30)) is None

    def test_end_below_threshold_is_stamped_with_the_first_input(self) -> None:
        tracker = parse.IdleTracker(THRESHOLD)
        tracker.update(500, NOW)
        ended = tracker.update(THRESHOLD - 0.001, at(20))
        assert ended is not None
        assert ended.event == "idle.end"
        assert ended.at == at(20) - timedelta(seconds=THRESHOLD - 0.001)
        assert not tracker.idle
        assert tracker.update(3, at(35)) is None

    def test_a_second_idle_period_is_reported_again(self) -> None:
        tracker = parse.IdleTracker(THRESHOLD)
        events = [
            tracker.update(value, at(i * 15)) for i, value in enumerate([0, 310, 2, 0, 301, 0])
        ]
        assert [e.event for e in events if e] == [
            "idle.start",
            "idle.end",
            "idle.start",
            "idle.end",
        ]

    def test_a_negative_reading_counts_as_zero(self) -> None:
        tracker = parse.IdleTracker(THRESHOLD)
        assert tracker.update(-5, NOW) is None

    def test_a_threshold_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            parse.IdleTracker(0)


# -- lock ---------------------------------------------------------------------------------------


class TestLockTracker:
    def test_an_unlocked_first_reading_is_a_baseline_without_an_event(self) -> None:
        tracker = parse.LockTracker()
        assert tracker.update(False, NOW) is None
        assert not tracker.locked

    def test_lock_and_unlock(self) -> None:
        tracker = parse.LockTracker()
        assert tracker.update(True, NOW) == parse.Transition("lock.start", NOW)
        assert tracker.locked
        assert tracker.update(True, at(5)) is None
        assert tracker.update(False, at(10)) == parse.Transition("lock.end", at(10))
        assert tracker.update(False, at(15)) is None

    def test_starting_locked_is_an_event(self) -> None:
        assert parse.LockTracker().update(True, NOW) is not None


# -- call ---------------------------------------------------------------------------------------


class TestCallTracker:
    def test_microphone_starts_and_ends_a_call(self) -> None:
        tracker = parse.CallTracker()
        assert tracker.update(False, False, NOW) is None
        start = tracker.update(True, False, at(5))
        assert start == parse.Transition("call.start", at(5), microphone=True, camera=False)
        assert tracker.in_call
        assert tracker.update(True, False, at(10)) is None
        end = tracker.update(False, False, at(15))
        assert end == parse.Transition("call.end", at(15))
        assert not tracker.in_call

    def test_the_camera_alone_is_a_call(self) -> None:
        start = parse.CallTracker().update(False, True, NOW)
        assert start == parse.Transition("call.start", NOW, microphone=False, camera=True)

    def test_a_call_goes_on_while_either_signal_runs(self) -> None:
        tracker = parse.CallTracker()
        tracker.update(True, True, NOW)
        assert tracker.update(False, True, at(5)) is None
        assert tracker.update(True, False, at(10)) is None
        assert tracker.update(False, False, at(15)) is not None

    def test_an_unreadable_signal_never_ends_a_call(self) -> None:
        tracker = parse.CallTracker()
        tracker.update(True, False, NOW)
        assert tracker.update(None, False, at(5)) is None
        assert tracker.update(False, None, at(10)) is None
        assert tracker.in_call
        assert tracker.update(None, None, at(15)) is None
        assert tracker.in_call

    def test_a_readable_running_signal_starts_a_call_despite_an_unreadable_one(self) -> None:
        start = parse.CallTracker().update(None, True, NOW)
        assert start == parse.Transition("call.start", NOW, microphone=False, camera=True)

    def test_unknown_signals_do_not_start_a_call(self) -> None:
        assert parse.CallTracker().update(None, False, NOW) is None
        assert parse.CallTracker().update(None, None, NOW) is None


# -- records ------------------------------------------------------------------------------------


class TestRecords:
    @pytest.mark.parametrize(
        ("event", "entity_id", "state"),
        [
            ("idle.start", "idle", "idle"),
            ("idle.end", "idle", "active"),
            ("lock.start", "lock", "locked"),
            ("lock.end", "lock", "active"),
        ],
    )
    def test_idle_and_lock_are_presence_events(
        self, event: str, entity_id: str, state: str
    ) -> None:
        record = parse.to_record(parse.Transition(event, NOW))
        assert isinstance(record, PresenceEvent)
        assert record.source == "presence"
        assert record.entity_kind is EntityKind.PRESENCE
        assert record.entity_id == entity_id
        assert record.payload.state == state
        assert record.observed_at == NOW
        assert parse_record(record.model_dump(mode="json")) == record

    def test_a_call_is_a_context_item_with_flags(self) -> None:
        record = parse.to_record(parse.Transition("call.start", NOW, microphone=True))
        assert isinstance(record, ContextItemEvent)
        assert record.entity_kind is EntityKind.CONTEXT_ITEM
        assert record.entity_id == "call"
        assert record.payload.context_kind == "call"
        assert record.payload.data == {"event": "call.start", "microphone": True, "camera": False}
        assert parse_record(record.model_dump(mode="json")) == record

    def test_dedup_keys_differ_by_event_and_time(self) -> None:
        keys = {
            parse.to_record(parse.Transition(event, when)).dedup_key
            for event in ("idle.start", "idle.end", "lock.start", "lock.end", "call.end")
            for when in (NOW, at(1))
        }
        assert len(keys) == 10

    def test_an_unknown_event_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown presence event"):
            parse.to_record(parse.Transition("nap.start", NOW))


# -- the collector under FakeClock --------------------------------------------------------------


class TestSettings:
    def test_defaults(self) -> None:
        settings = PresenceSettings()
        assert settings.idle_threshold_seconds == 300
        assert settings.idle_poll_interval_seconds == 15
        assert settings.poll_interval_seconds == 5

    @pytest.mark.parametrize(
        "values",
        [
            {"idle_threshold_seconds": 0},
            {"idle_poll_interval_seconds": 0},
            {"poll_interval_seconds": 4.9},
            {"poll_interval_seconds": 10.1},
            {"unknown": 1},
        ],
    )
    def test_bad_values_are_refused(self, values: dict[str, float]) -> None:
        with pytest.raises(ValidationError):
            PresenceSettings(**values)


async def test_idle_is_polled_only_while_a_focus_span_is_open() -> None:
    signals = FakeSignals()
    signals.focus = False
    collector, clock, sink = make_collector(signals)
    task = _start(collector)
    await settle()
    await clock.advance(120)
    assert signals.idle_reads == 0, "no focused window, no idle poll"
    assert collector.idle_polls == 0
    assert signals.focus_reads >= 8  # the gate is checked every 15 s: 0, 15, ... 120

    signals.focus = True
    await clock.advance(15)
    assert signals.idle_reads == 1
    await clock.advance(30)
    assert signals.idle_reads == 3

    signals.focus = False
    await clock.advance(60)
    assert signals.idle_reads == 3, "polling stops when the focus span closes"
    assert sink.records == []
    await _stop(task)


async def test_idle_is_polled_every_fifteen_seconds() -> None:
    signals = FakeSignals()
    collector, clock, _ = make_collector(signals)
    task = _start(collector)
    await settle()
    assert signals.idle_reads == 1
    await clock.advance(14.9)
    assert signals.idle_reads == 1
    await clock.advance(0.1)
    assert signals.idle_reads == 2
    await clock.advance(45)
    assert signals.idle_reads == 5
    await _stop(task)


async def test_idle_start_and_end_follow_the_threshold_with_exact_boundaries() -> None:
    signals = FakeSignals()
    collector, clock, sink = make_collector(signals, idle_threshold_seconds=60)
    task = _start(collector)

    # The reading is the time since the last input; the last input was at EPOCH.
    signals.idle = lambda: clock.monotonic() if clock.monotonic() < 95 else 0.0
    await settle()
    await clock.advance(45)  # idle 45 s at the poll of t=45: below the threshold
    assert event_names(sink) == []
    await clock.advance(15)  # t=60 polls idle 60 s: exactly the threshold
    assert event_names(sink) == ["idle.start"]
    assert sink.records[0].observed_at == EPOCH + timedelta(seconds=60)
    await clock.advance(30)  # t=90: still idle
    assert event_names(sink) == ["idle.start"]
    await clock.advance(15)  # t=105: input at t=95 ended it
    assert event_names(sink) == ["idle.start", "idle.end"]
    assert sink.records[1].observed_at == EPOCH + timedelta(seconds=105)
    await _stop(task)


async def test_lock_transitions_are_emitted_within_one_poll() -> None:
    signals = FakeSignals()
    collector, clock, sink = make_collector(signals)
    task = _start(collector)
    await settle()
    assert event_names(sink) == []
    signals.locked = True
    await clock.advance(5)
    assert event_names(sink) == ["lock.start"]
    await clock.advance(10)
    assert event_names(sink) == ["lock.start"]
    signals.locked = False
    await clock.advance(5)
    assert event_names(sink) == ["lock.start", "lock.end"]
    assert [r.observed_at for r in sink.records] == [
        EPOCH + timedelta(seconds=5),
        EPOCH + timedelta(seconds=20),
    ]
    await _stop(task)


async def test_call_start_and_end_follow_the_microphone_and_the_camera() -> None:
    signals = FakeSignals()
    collector, clock, sink = make_collector(signals)
    task = _start(collector)
    await settle()
    signals.microphone = True
    await clock.advance(5)
    signals.camera = True
    await clock.advance(5)
    signals.microphone = False
    await clock.advance(5)
    assert event_names(sink) == ["call.start"]
    signals.camera = False
    await clock.advance(5)
    assert event_names(sink) == ["call.start", "call.end"]
    await _stop(task)


async def test_the_poll_interval_is_configurable_between_five_and_ten_seconds() -> None:
    signals = FakeSignals()
    collector, clock, _ = make_collector(signals, poll_interval_seconds=10)
    task = _start(collector)
    await settle()
    await clock.advance(25)
    assert collector.signal_polls == 3  # 0, 10, 20
    await _stop(task)


# -- failures -----------------------------------------------------------------------------------


async def test_a_failing_signal_degrades_and_the_others_keep_reporting() -> None:
    signals = FakeSignals()
    signals.microphone = SignalError("microphone", "framework_missing")
    collector, clock, sink = make_collector(signals)
    task = _start(collector)
    await settle()
    report = await collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.detail is not None
    assert "microphone: framework_missing" in report.detail
    assert report.error_kind == "microphone_framework_missing"

    signals.locked = True
    signals.camera = True
    await clock.advance(5)
    assert event_names(sink) == ["lock.start", "call.start"]
    assert (await collector.doctor()).state is HealthState.DEGRADED

    signals.microphone = False
    await clock.advance(5)
    assert (await collector.doctor()).state is HealthState.OK
    await _stop(task)


async def test_doctor_names_every_failing_signal_in_a_fixed_order() -> None:
    signals = FakeSignals()
    signals.camera = SignalError("camera", "os_status_error")
    signals.locked = SignalError("lock", "no_session_dictionary")
    signals.idle = SignalError("idle", "ioreg_missing")
    collector, _, _ = make_collector(signals)
    task = _start(collector)
    await settle()
    report = await collector.doctor()
    assert report.detail == (
        "failing signals: idle: ioreg_missing; lock: no_session_dictionary; camera: os_status_error"
    )
    assert report.error_kind == "idle_ioreg_missing"
    await _stop(task)


async def test_an_unreadable_signal_emits_no_guess() -> None:
    signals = FakeSignals()
    collector, clock, sink = make_collector(signals)
    task = _start(collector)
    signals.microphone = True
    await clock.advance(5)
    assert event_names(sink) == ["call.start"]
    signals.microphone = SignalError("microphone", "call_failed")
    await clock.advance(30)
    assert event_names(sink) == ["call.start"], "an unreadable microphone does not end the call"
    signals.microphone = False
    await clock.advance(5)
    assert event_names(sink) == ["call.start", "call.end"]
    await _stop(task)


async def test_an_unreadable_focus_state_stops_the_idle_poll_and_is_named() -> None:
    signals = FakeSignals()
    signals.focus = SignalError("focus", "store_OperationalError")
    collector, clock, _ = make_collector(signals)
    task = _start(collector)
    await settle()
    await clock.advance(30)
    assert signals.idle_reads == 0
    report = await collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.detail is not None
    assert "focus: store_OperationalError" in report.detail
    signals.focus = True
    await clock.advance(15)
    assert signals.idle_reads >= 1
    assert (await collector.doctor()).state is HealthState.OK
    await _stop(task)


async def test_an_unexpected_error_is_logged_once_and_the_loop_goes_on() -> None:
    signals = FakeSignals()

    def boom() -> bool:
        raise RuntimeError("boom")

    signals.locked = boom
    collector, clock, sink = make_collector(signals)
    task = _start(collector)
    await settle()
    await clock.advance(15)
    report = await collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.error_kind == "poll_failed"
    assert report.detail is not None
    assert "signals (RuntimeError)" in report.detail
    assert collector.signal_polls >= 3, "the loop kept polling"

    signals.locked = False
    await clock.advance(5)
    assert (await collector.doctor()).state is HealthState.OK
    assert sink.records == []
    await _stop(task)


async def test_an_unexpected_idle_error_does_not_stop_the_idle_loop() -> None:
    signals = FakeSignals()

    def boom() -> float:
        raise RuntimeError("boom")

    signals.idle = boom
    collector, clock, _ = make_collector(signals)
    task = _start(collector)
    await settle()
    await clock.advance(30)
    assert signals.idle_reads == 3
    report = await collector.doctor()
    assert report.detail is not None
    assert "idle (RuntimeError)" in report.detail
    signals.idle = 0.0
    await clock.advance(15)
    assert (await collector.doctor()).state is HealthState.OK
    await _stop(task)


async def test_doctor_before_the_first_poll_is_ok_and_says_so() -> None:
    collector, _, _ = make_collector(FakeSignals())
    report = await collector.doctor()
    assert report.state is HealthState.OK
    assert report.detail == "not polled yet"


async def test_cancellation_leaves_no_task_and_no_sleep() -> None:
    clock = FakeClock()
    collector, _, _ = make_collector(FakeSignals(), clock=clock)
    task = _start(collector)
    await settle()
    await _stop(task)
    assert clock.pending == 0


# -- helpers ------------------------------------------------------------------------------------


def _start(collector: PresenceCollector) -> asyncio.Task[None]:
    return asyncio.create_task(collector.run(), name="presence-test")


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.wait({task}, timeout=5)
    assert task.done()
