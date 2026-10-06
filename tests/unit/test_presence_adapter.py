"""The presence adapter: ``ioreg`` parsing from fixtures and ``ctypes`` failures per signal.

The frameworks are fakes (:class:`presence_support.FakeFrameworks`), so a failure is scripted: a
missing framework, a missing symbol, a non-zero ``OSStatus`` or an exception from the call itself.
Each one fails only its own signal.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from presence_support import FakeFrameworks, make_collector, store_with_focus

from fylgja.collectors.presence import adapter, parse
from fylgja.collectors.presence.adapter import SignalError, SystemSignals
from fylgja.models import HealthState
from fylgja.testing.fakes import FakeProcessRunner, settle

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "presence"
IOREG_ARGV = (adapter.IOREG, "-r", "-c", "IOHIDSystem", "-d", "1")


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def signals(
    frameworks: FakeFrameworks | None = None,
    runner: FakeProcessRunner | None = None,
    *,
    database: Path | None = None,
    platform: str = "darwin",
) -> SystemSignals:
    return SystemSignals(
        FakeProcessRunner() if runner is None else runner,
        Path("/nonexistent/fylgja.db") if database is None else database,
        loader=FakeFrameworks() if frameworks is None else frameworks,
        platform=platform,
    )


def fails(signal: str, kind: str, call: Any) -> None:
    with pytest.raises(SignalError) as caught:
        call()
    assert (caught.value.signal, caught.value.kind) == (signal, kind)


# -- ioreg --------------------------------------------------------------------------------------


class TestIoregParsing:
    @pytest.mark.parametrize(
        ("name", "seconds"),
        [
            ("ioreg-active.txt", 0.340898291),
            ("ioreg-idle-310s.txt", 310.0),
            ("ioreg-zero.txt", 0.0),
            ("ioreg-two-keys.txt", 5.0),
        ],
    )
    def test_the_idle_time_is_read_in_seconds(self, name: str, seconds: float) -> None:
        assert parse.parse_hid_idle_seconds(fixture(name)) == pytest.approx(seconds)

    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("ioreg-no-idle-key.txt", "idle_key_missing"),
            ("ioreg-garbled.txt", "idle_value_invalid"),
            ("ioreg-huge.txt", "idle_value_invalid"),
        ],
    )
    def test_unusable_output_is_refused_with_a_code(self, name: str, kind: str) -> None:
        with pytest.raises(parse.IdleParseError) as caught:
            parse.parse_hid_idle_seconds(fixture(name))
        assert caught.value.kind == kind

    def test_the_tree_form_with_a_prefix_is_read_too(self) -> None:
        assert parse.parse_hid_idle_seconds(b'    | | |   "HIDIdleTime" = 2000000000\n') == 2.0

    async def test_the_signal_runs_ioreg_with_a_timeout_and_parses_the_fixture(self) -> None:
        runner = FakeProcessRunner()
        runner.script(*IOREG_ARGV, stdout=fixture("ioreg-idle-310s.txt"))
        assert await signals(runner=runner).idle_seconds() == pytest.approx(310.0)
        assert runner.calls == [IOREG_ARGV]
        assert runner.timeouts == [adapter.IOREG_TIMEOUT]

    @pytest.mark.parametrize(
        ("script", "kind"),
        [
            ({"error": FileNotFoundError("ioreg")}, "ioreg_missing"),
            ({"error": PermissionError("ioreg")}, "ioreg_not_executable"),
            ({"timeout": True}, "ioreg_timeout"),
            ({"error": OSError("boom")}, "ioreg_os_error"),
            ({"returncode": 1, "stdout": b"x"}, "ioreg_failed"),
            ({"stdout": fixture("ioreg-no-idle-key.txt")}, "idle_key_missing"),
            ({"stdout": fixture("ioreg-garbled.txt")}, "idle_value_invalid"),
        ],
    )
    async def test_every_failure_is_the_idle_signal_failing(
        self, script: dict[str, Any], kind: str
    ) -> None:
        runner = FakeProcessRunner()
        runner.script(*IOREG_ARGV, **script)
        with pytest.raises(SignalError) as caught:
            await signals(runner=runner).idle_seconds()
        assert (caught.value.signal, caught.value.kind) == ("idle", kind)


# -- screen lock --------------------------------------------------------------------------------


class TestScreenLock:
    def test_a_locked_screen(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.locked = True
        assert signals(frameworks).screen_locked() is True
        assert frameworks.released == [200, 100], "the string and the dictionary are released"

    def test_an_absent_key_is_an_unlocked_screen(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.locked = None
        assert signals(frameworks).screen_locked() is False
        assert frameworks.released == [200, 100]

    def test_a_false_value_is_an_unlocked_screen(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.locked = False
        assert signals(frameworks).screen_locked() is False

    def test_no_session_dictionary_is_a_failure_not_an_unlocked_screen(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.session = False
        fails("lock", "no_session_dictionary", signals(frameworks).screen_locked)
        assert frameworks.released == [], "nothing was copied, nothing is released"

    def test_a_string_that_cannot_be_made_is_a_failure_and_the_dictionary_is_released(
        self,
    ) -> None:
        frameworks = FakeFrameworks()
        frameworks.string_ok = False
        fails("lock", "call_failed", signals(frameworks).screen_locked)
        assert frameworks.released == [100]

    @pytest.mark.parametrize("path", [adapter.COREGRAPHICS, adapter.COREFOUNDATION])
    def test_a_missing_framework_is_a_failure(self, path: str) -> None:
        frameworks = FakeFrameworks()
        frameworks.missing = {path}
        fails("lock", "framework_missing", signals(frameworks).screen_locked)

    @pytest.mark.parametrize(
        "symbol",
        [
            "CGSessionCopyCurrentDictionary",
            "CFDictionaryGetValue",
            "CFStringCreateWithCString",
            "CFBooleanGetValue",
            "CFRelease",
        ],
    )
    def test_a_missing_symbol_is_a_failure(self, symbol: str) -> None:
        frameworks = FakeFrameworks()
        frameworks.missing_symbols = {symbol}
        fails("lock", "symbol_missing", signals(frameworks).screen_locked)

    def test_a_call_that_raises_is_a_failure_and_releases_what_it_holds(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.locked = True
        frameworks.raising = {"CFDictionaryGetValue": OSError("boom")}
        fails("lock", "call_failed", signals(frameworks).screen_locked)
        assert frameworks.released == [200, 100]

    def test_a_non_macos_platform_is_a_failure_and_loads_nothing(self) -> None:
        frameworks = FakeFrameworks()
        fails("lock", "unsupported_platform", signals(frameworks, platform="linux").screen_locked)
        assert frameworks.loads == []


# -- microphone ---------------------------------------------------------------------------------


class TestMicrophone:
    def test_idle_and_running(self) -> None:
        frameworks = FakeFrameworks()
        assert signals(frameworks).microphone_in_use() is False
        frameworks.input_running = True
        assert signals(frameworks).microphone_in_use() is True

    def test_no_input_device_is_not_in_use(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.default_input = 0
        frameworks.input_running = True
        assert signals(frameworks).microphone_in_use() is False

    def test_a_status_error_on_the_default_device_is_a_failure(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.audio_status = {(1, adapter.AUDIO_DEFAULT_INPUT): -1}
        fails("microphone", "os_status_error", signals(frameworks).microphone_in_use)

    def test_a_status_error_on_the_running_flag_is_a_failure(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.audio_status = {(7, adapter.RUNNING_SOMEWHERE): -1}
        fails("microphone", "os_status_error", signals(frameworks).microphone_in_use)

    def test_a_missing_framework_is_a_failure(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.missing = {adapter.COREAUDIO}
        fails("microphone", "framework_missing", signals(frameworks).microphone_in_use)

    def test_a_missing_symbol_is_a_failure(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.missing_symbols = {"AudioObjectGetPropertyData"}
        fails("microphone", "symbol_missing", signals(frameworks).microphone_in_use)

    def test_a_call_that_raises_is_a_failure(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.raising = {"AudioObjectGetPropertyData": OSError("boom")}
        fails("microphone", "call_failed", signals(frameworks).microphone_in_use)

    def test_a_non_macos_platform_is_a_failure(self) -> None:
        fails(
            "microphone",
            "unsupported_platform",
            signals(platform="linux").microphone_in_use,
        )


# -- camera -------------------------------------------------------------------------------------


class TestCamera:
    def test_idle_and_running(self) -> None:
        frameworks = FakeFrameworks()
        assert signals(frameworks).camera_in_use() is False
        frameworks.camera_running = {11: True}
        assert signals(frameworks).camera_in_use() is True

    def test_no_video_device_is_not_in_use(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.cameras = []
        assert signals(frameworks).camera_in_use() is False

    def test_any_running_device_counts(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.cameras = [11, 12, 13]
        frameworks.camera_running = {13: True}
        assert signals(frameworks).camera_in_use() is True

    def test_a_device_without_the_property_is_skipped(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.cameras = [11, 12]
        frameworks.camera_status = {11: adapter.UNKNOWN_PROPERTY}
        assert signals(frameworks).camera_in_use() is False
        frameworks.camera_running = {12: True}
        assert signals(frameworks).camera_in_use() is True

    def test_a_device_that_errors_while_none_runs_is_a_failure(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.cameras = [11, 12]
        frameworks.camera_status = {11: -1}
        fails("camera", "os_status_error", signals(frameworks).camera_in_use)

    def test_a_running_device_settles_it_despite_another_that_errors(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.cameras = [11, 12]
        frameworks.camera_status = {11: -1}
        frameworks.camera_running = {12: True}
        assert signals(frameworks).camera_in_use() is True

    def test_a_failing_device_list_is_a_failure(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.list_size_status = -1
        fails("camera", "os_status_error", signals(frameworks).camera_in_use)
        frameworks.list_size_status = 0
        frameworks.list_data_status = -1
        fails("camera", "os_status_error", signals(frameworks).camera_in_use)

    def test_a_missing_framework_is_a_failure(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.missing = {adapter.COREMEDIAIO}
        fails("camera", "framework_missing", signals(frameworks).camera_in_use)

    @pytest.mark.parametrize(
        "symbol", ["CMIOObjectGetPropertyDataSize", "CMIOObjectGetPropertyData"]
    )
    def test_a_missing_symbol_is_a_failure(self, symbol: str) -> None:
        frameworks = FakeFrameworks()
        frameworks.missing_symbols = {symbol}
        fails("camera", "symbol_missing", signals(frameworks).camera_in_use)

    def test_a_call_that_raises_is_a_failure(self) -> None:
        frameworks = FakeFrameworks()
        frameworks.raising = {"CMIOObjectGetPropertyDataSize": OSError("boom")}
        fails("camera", "call_failed", signals(frameworks).camera_in_use)

    def test_a_non_macos_platform_is_a_failure(self) -> None:
        fails("camera", "unsupported_platform", signals(platform="linux").camera_in_use)


# -- focus (the gate of the idle poll) ----------------------------------------------------------


class TestFocus:
    def test_a_store_that_does_not_exist_yet_has_no_focus(self, tmp_path: Path) -> None:
        assert signals(database=tmp_path / "none.db").focus_open() is False

    def test_a_focused_window_is_an_open_span(self, tmp_path: Path) -> None:
        database = store_with_focus(tmp_path / "a" / "fylgja.db", window_id="42")
        assert signals(database=database).focus_open() is True

    def test_a_focus_event_without_a_window_is_no_span(self, tmp_path: Path) -> None:
        database = store_with_focus(tmp_path / "b" / "fylgja.db", window_id=None)
        assert signals(database=database).focus_open() is False

    def test_an_unreadable_store_is_a_failure(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.db"
        broken.write_bytes(b"this is not a database" * 64)
        with pytest.raises(SignalError) as caught:
            signals(database=broken).focus_open()
        assert caught.value.signal == "focus"
        assert caught.value.kind.startswith("store_")


# -- a failure degrades its own signal only -----------------------------------------------------


async def test_a_ctypes_failure_degrades_that_signal_and_the_rest_keep_reporting() -> None:
    frameworks = FakeFrameworks()
    frameworks.missing = {adapter.COREAUDIO}  # the microphone is gone
    frameworks.locked = True
    frameworks.camera_running = {11: True}
    runner = FakeProcessRunner()
    runner.script(*IOREG_ARGV, stdout=fixture("ioreg-active.txt"))
    collector, clock, sink = make_collector(signals(frameworks, runner))
    task = asyncio.create_task(collector.run())
    await settle()
    await clock.advance(5)

    report = await collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.detail == "failing signals: microphone: framework_missing"
    assert [r.entity_id for r in sink.records] == ["lock", "call"]
    task.cancel()
    await asyncio.wait({task}, timeout=5)


async def test_every_framework_missing_degrades_all_three_ctypes_signals() -> None:
    frameworks = FakeFrameworks()
    frameworks.missing = {
        adapter.COREAUDIO,
        adapter.COREMEDIAIO,
        adapter.COREGRAPHICS,
        adapter.COREFOUNDATION,
    }
    collector, clock, sink = make_collector(signals(frameworks))
    task = asyncio.create_task(collector.run())
    await settle()
    report = await collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.detail == (
        "failing signals: lock: framework_missing; microphone: framework_missing; "
        "camera: framework_missing"
    )
    assert sink.records == []
    task.cancel()
    await asyncio.wait({task}, timeout=5)


async def test_a_platform_other_than_macos_degrades_every_signal_and_never_guesses() -> None:
    runner = FakeProcessRunner()  # no ioreg here: the command is missing
    collector, clock, sink = make_collector(signals(runner=runner, platform="linux"))
    task = asyncio.create_task(collector.run())
    await settle()
    report = await collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.detail is not None
    for name in ("lock", "microphone", "camera"):
        assert f"{name}: unsupported_platform" in report.detail
    assert sink.records == []
    task.cancel()
    await asyncio.wait({task}, timeout=5)


def test_the_default_signals_are_the_system_signals(tmp_path: Path) -> None:
    built = adapter.default_signals(tmp_path / "fylgja.db")
    assert isinstance(built, SystemSignals)
    assert isinstance(built, adapter.PresenceSignals)


@pytest.mark.skipif(sys.platform != "darwin", reason="CoreAudio and friends are macOS only")
def test_the_real_frameworks_load_and_expose_the_symbols_the_adapter_uses() -> None:
    """Loads the real libraries (no device is read) so a renamed symbol fails here, not live."""
    import ctypes

    wanted = {
        adapter.COREGRAPHICS: ["CGSessionCopyCurrentDictionary"],
        adapter.COREFOUNDATION: [
            "CFDictionaryGetValue",
            "CFStringCreateWithCString",
            "CFBooleanGetValue",
            "CFRelease",
        ],
        adapter.COREAUDIO: ["AudioObjectGetPropertyData"],
        adapter.COREMEDIAIO: ["CMIOObjectGetPropertyDataSize", "CMIOObjectGetPropertyData"],
    }
    for path, names in wanted.items():
        library = ctypes.CDLL(path)
        for name in names:
            assert getattr(library, name) is not None
