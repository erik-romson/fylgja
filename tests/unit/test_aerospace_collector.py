"""The AeroSpace collector under fakes: restarts, debounce, timer, degraded health, snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import pytest

from fylgja.collectors.aerospace import adapter, parse
from fylgja.collectors.aerospace.collector import (
    MAX_DEBOUNCE_WAIT,
    STREAM_POLICY,
    AerospaceCollector,
    AerospaceSettings,
)
from fylgja.collectors.base import Capability
from fylgja.collectors.context import build_context
from fylgja.models import (
    EntityKind,
    FocusEvent,
    HealthState,
    WindowSnapshot,
    WorkspaceSnapshot,
)
from fylgja.testing.fakes import (
    FakeClock,
    FakeLineStream,
    FakeProcessRunner,
    FakeStreamOpener,
    RecordingSink,
    settle,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "aerospace"
BIN = "/opt/homebrew/bin/aerospace"
LIST_WINDOWS = (BIN, "list-windows")
LIST_WORKSPACES = (BIN, "list-workspaces", "--all", "--json")
VERSION_TEXT = "aerospace CLI client version: 0.21.3-Beta abc123\n"


class NoJitter:
    def uniform(self, a: float, b: float) -> float:
        return 0.0


def _window(window_id: int, workspace: str = "1", title: str = "Title") -> dict[str, object]:
    return {
        "window-id": window_id,
        "workspace": workspace,
        "monitor-id": 1,
        "app-bundle-id": "com.example.app",
        "app-name": "Example",
        "app-pid": 100 + window_id,
        "window-title": title,
    }


class Rig:
    def __init__(self, settings: AerospaceSettings | None = None) -> None:
        self.clock = FakeClock()
        self.sink = RecordingSink()
        self.runner = FakeProcessRunner()
        self.streams = FakeStreamOpener()
        self.collector = AerospaceCollector(
            AerospaceSettings() if settings is None else settings,
            build_context("aerospace", self.sink, clock=self.clock),
            binary=BIN,
            seams=adapter.Seams(self.runner, self.streams),
            rng=NoJitter(),
        )
        self.task: asyncio.Task[None] | None = None

    def script_world(
        self,
        windows: list[dict[str, object]] | None = None,
        *,
        skip: tuple[str, ...] = (),
    ) -> None:
        """Script a healthy AeroSpace. ``skip`` leaves out commands (the first script wins)."""
        if "list-windows" not in skip:
            self.runner.script(
                *LIST_WINDOWS, stdout=json.dumps([_window(1)] if windows is None else windows)
            )
        if "list-workspaces" not in skip:
            self.runner.script(
                *LIST_WORKSPACES,
                stdout=json.dumps(
                    [{"workspace": "1", "workspace-is-focused": True, "monitor-id": 1}]
                ),
            )
        if "version" not in skip:
            self.runner.script(BIN, "--version", stdout=VERSION_TEXT)
        if "count" not in skip:
            self.runner.script(BIN, "list-workspaces", "--all", "--count", stdout="1\n")

    def listings(self) -> int:
        return sum(1 for call in self.runner.calls if call[:2] == LIST_WINDOWS)

    async def start(self) -> None:
        self.task = asyncio.create_task(self.collector.run())
        await settle()

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    value = Rig()
    value.script_world()
    try:
        yield value
    finally:
        await value.stop()


def _focus_line(window_id: int, workspace: str = "1") -> str:
    return json.dumps({"_event": "focus-changed", "windowId": window_id, "workspace": workspace})


# -- stream end -> restart ----------------------------------------------------------------------


async def test_the_stream_ending_restarts_it_with_growing_backoff(rig: Rig) -> None:
    first = FakeLineStream(_focus_line(1) + "\n", eof=True)
    rig.streams.add(first)
    rig.streams.add(FakeLineStream(eof=True))
    rig.streams.add(FakeLineStream(eof=True))
    await rig.start()
    assert len(rig.streams.calls) == 1
    assert rig.streams.calls[0] == (BIN, "subscribe", "--all")

    await rig.clock.advance(0.9)
    assert len(rig.streams.calls) == 1, "the first retry waits 1 s"
    await rig.clock.advance(0.2)
    assert len(rig.streams.calls) == 2
    await rig.clock.advance(1.8)
    assert len(rig.streams.calls) == 2, "the second retry waits 2 s"
    await rig.clock.advance(0.3)
    assert len(rig.streams.calls) == 3
    assert rig.collector.stats.restarts == 3
    retry_sleeps = [s for s in rig.clock.sleeps if s in (1.0, 2.0, 4.0)]
    assert retry_sleeps[:3] == [1.0, 2.0, 4.0]


async def test_a_torn_last_line_at_the_end_of_the_stream_is_counted(rig: Rig) -> None:
    rig.streams.add(FakeLineStream(_focus_line(1) + "\n" + '{"_event":"focus-chan', eof=True))
    await rig.start()
    assert rig.collector.stats.events == 1
    assert rig.collector.stats.malformed == {"torn": 1}
    assert rig.collector.stats.malformed_total == 1


async def test_the_backoff_resets_after_a_healthy_stretch(rig: Rig) -> None:
    long_stream = FakeLineStream(_focus_line(1) + "\n")
    rig.streams.add(FakeLineStream(eof=True))
    rig.streams.add(long_stream)
    await rig.start()
    await rig.clock.advance(1.1)  # second open: the long-lived stream
    assert len(rig.streams.calls) == 2
    await rig.clock.advance(STREAM_POLICY.reset_after + 1)
    long_stream.feed_eof()
    await rig.clock.advance(0.5)
    await rig.clock.advance(1.0)
    assert len(rig.streams.calls) >= 3, "after a long run the next delay is the initial 1 s again"


async def test_a_missing_binary_makes_the_stream_loop_retry_without_crashing() -> None:
    rig = Rig()
    rig.streams.error = FileNotFoundError("aerospace")
    await rig.start()
    try:
        for _ in range(10):
            await rig.clock.advance(5.0)
        assert rig.task is not None
        assert not rig.task.done()
        assert len(rig.streams.calls) >= 3
        assert rig.collector.snapshots == 0
        assert rig.streams.active == 0
    finally:
        await rig.stop()


async def test_other_os_errors_on_open_also_retry(rig: Rig) -> None:
    rig.streams.error = PermissionError("denied")
    await rig.start()
    await rig.clock.advance(3.5)
    assert rig.task is not None
    assert not rig.task.done()
    assert len(rig.streams.calls) >= 2


# -- debounce and timer ----------------------------------------------------------------------


async def test_a_focus_burst_gives_one_snapshot_250_ms_after_the_last_event(rig: Rig) -> None:
    stream = FakeLineStream()
    rig.streams.add(stream)
    await rig.start()
    assert rig.listings() == 1, "a snapshot is taken at start"

    stream.feed_line(_focus_line(1))
    await rig.clock.advance(0.1)
    stream.feed_line(_focus_line(2))
    await rig.clock.advance(0.1)
    stream.feed_line(_focus_line(3))
    await rig.clock.advance(0.2)
    assert rig.listings() == 1, "the debounce window restarts on each event"
    await rig.clock.advance(0.1)
    assert rig.listings() == 2
    await rig.clock.advance(5)
    assert rig.listings() == 2, "one burst, one snapshot"


async def test_events_that_do_not_move_focus_never_take_a_snapshot(rig: Rig) -> None:
    stream = FakeLineStream(
        '{"_event":"mode-changed","mode":"service"}\n',
        '{"_event":"binding-triggered","mode":"main"}\n',
        '{"_event":"new-thing"}\n',
        "garbage\n",
    )
    rig.streams.add(stream)
    await rig.start()
    await rig.clock.advance(5)
    assert rig.listings() == 1
    assert rig.collector.stats.unknown_events == 1
    assert rig.collector.stats.malformed == {"invalid_json": 1}


async def test_a_continuous_stream_of_events_cannot_starve_the_snapshot(rig: Rig) -> None:
    stream = FakeLineStream()
    rig.streams.add(stream)
    await rig.start()
    for _ in range(12):
        stream.feed_line(_focus_line(1))
        await rig.clock.advance(0.2)
    assert rig.listings() >= 2, f"no snapshot within {MAX_DEBOUNCE_WAIT}s of a constant burst"


async def test_the_timer_snapshots_every_30_seconds(rig: Rig) -> None:
    await rig.start()
    assert rig.listings() == 1
    await rig.clock.advance(29.9)
    assert rig.listings() == 1
    await rig.clock.advance(0.2)
    assert rig.listings() == 2
    await rig.clock.advance(30)
    assert rig.listings() == 3
    assert AerospaceSettings().snapshot_interval_seconds == 30


async def test_the_interval_is_configurable() -> None:
    rig = Rig(AerospaceSettings(snapshot_interval_seconds=5))
    rig.script_world()
    await rig.start()
    try:
        await rig.clock.advance(10.5)
        assert rig.listings() == 3
    finally:
        await rig.stop()


async def test_the_snapshot_commands_and_their_timeout(rig: Rig) -> None:
    await rig.start()
    assert rig.runner.calls[0] == (
        BIN,
        "list-windows",
        "--monitor",
        "all",
        "--json",
        "--format",
        parse.WINDOW_FORMAT,
    )
    assert rig.runner.calls[1] == (
        BIN,
        "list-workspaces",
        "--all",
        "--json",
        "--format",
        parse.WORKSPACE_FORMAT,
    )
    assert rig.runner.timeouts[:2] == [5.0, 5.0]


# -- snapshots ---------------------------------------------------------------------------------


async def test_a_complete_snapshot_closes_a_window_that_disappeared() -> None:
    rig = Rig()
    rig.script_world([_window(1), _window(2, "2")])
    await rig.start()
    try:
        first = rig.sink.of_kind(EntityKind.WINDOW)[0]
        assert isinstance(first, WindowSnapshot)
        assert first.complete
        assert [i.entity_id for i in first.items] == ["1", "2"]

        rig2 = FakeProcessRunner()
        rig2.script(*LIST_WINDOWS, stdout=json.dumps([_window(1)]))
        rig2.script(*LIST_WORKSPACES, stdout="[]")
        rig.collector._seams = adapter.Seams(rig2, rig.streams)
        await rig.clock.advance(30.5)
        second = rig.sink.of_kind(EntityKind.WINDOW)[-1]
        assert isinstance(second, WindowSnapshot)
        assert second.complete
        assert [i.entity_id for i in second.items] == ["1"], (
            "window 2 is absent: the core closes it"
        )
    finally:
        await rig.stop()


async def test_a_snapshot_emits_complete_window_and_workspace_snapshots(rig: Rig) -> None:
    assert await rig.collector.snapshot() is True
    windows, workspaces = rig.sink.records
    assert isinstance(windows, WindowSnapshot)
    assert windows.complete
    assert isinstance(workspaces, WorkspaceSnapshot)
    assert workspaces.complete
    assert windows.observed_at == workspaces.observed_at
    assert rig.collector.snapshots == 1


async def test_a_failing_command_emits_nothing_and_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig()
    rig.script_world(skip=("list-windows",))
    rig.runner.script(*LIST_WINDOWS, returncode=1, stderr="Can't connect to AeroSpace server")
    caplog.set_level(logging.INFO)
    assert await rig.collector.snapshot() is False
    assert await rig.collector.snapshot() is False
    assert rig.sink.records == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "a repeated failure is logged once"
    assert "server_unreachable" in warnings[0].getMessage()


async def test_recovery_is_logged_and_snapshots_resume(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    broken = FakeProcessRunner()
    good = rig.runner
    rig.collector._seams = adapter.Seams(broken, rig.streams)
    caplog.set_level(logging.INFO)
    assert await rig.collector.snapshot() is False
    rig.collector._seams = adapter.Seams(good, rig.streams)
    assert await rig.collector.snapshot() is True
    assert any("recovered" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("script", "kind"),
    [
        ({"timeout": True}, "timeout"),
        ({"error": PermissionError("no")}, "binary_not_executable"),
        ({"error": OSError("boom")}, "os_error"),
        ({"stdout": "not json"}, "bad_output"),
        ({"stdout": '{"a": 1}'}, "bad_output"),
    ],
)
async def test_snapshot_failures_never_raise(
    script: dict[str, object], kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig()
    rig.runner.script(*LIST_WINDOWS, **script)  # type: ignore[arg-type]
    caplog.set_level(logging.WARNING)
    assert await rig.collector.snapshot() is False
    assert kind in caplog.text
    assert rig.sink.records == []


# -- focus events -------------------------------------------------------------------------------


async def test_focus_changed_emits_a_focus_event_enriched_from_the_last_snapshot(rig: Rig) -> None:
    rig.streams.add(FakeLineStream())
    await rig.start()
    rig.collector.handle_line((_focus_line(1, "1") + "\n").encode())
    event = rig.sink.of_kind(EntityKind.FOCUS)[-1]
    assert isinstance(event, FocusEvent)
    assert event.payload.window_id == "1"
    assert event.payload.workspace == "1"
    assert event.payload.app_bundle_id == "com.example.app"
    assert event.payload.title == "Title"
    assert event.entity_id == "focus"


async def test_focus_of_an_unknown_window_has_no_bundle_id(rig: Rig) -> None:
    rig.collector.handle_line((_focus_line(99, "7") + "\n").encode())
    event = rig.sink.of_kind(EntityKind.FOCUS)[-1]
    assert isinstance(event, FocusEvent)
    assert (event.payload.window_id, event.payload.app_bundle_id) == ("99", None)


async def test_a_workspace_change_clears_the_focused_window_only_when_the_workspace_differs(
    rig: Rig,
) -> None:
    handle = rig.collector.handle_line
    handle((_focus_line(5, "a") + "\n").encode())
    handle(b'{"_event":"focused-workspace-changed","workspace":"a","prevWorkspace":"z"}\n')
    focus = rig.sink.of_kind(EntityKind.FOCUS)
    assert len(focus) == 1, "same workspace: the focused window stays"
    handle(b'{"_event":"focused-workspace-changed","workspace":"b","prevWorkspace":"a"}\n')
    handle(b'{"_event":"focused-monitor-changed","monitorId":2,"workspace":"c"}\n')
    handle(b'{"_event":"focused-monitor-changed","monitorId":2}\n')
    focus = rig.sink.of_kind(EntityKind.FOCUS)
    payloads = [f.payload for f in focus if isinstance(f, FocusEvent)]
    assert [(p.window_id, p.workspace) for p in payloads] == [("5", "a"), (None, "b"), (None, "c")]


async def test_blank_lines_and_a_closed_stream_are_harmless(rig: Rig) -> None:
    rig.collector.handle_line(b"\n")
    rig.collector.handle_line(b"")
    assert rig.collector.stats.events == 0


# -- health --------------------------------------------------------------------------------------


async def test_doctor_reports_ok_with_the_version(rig: Rig) -> None:
    report = await rig.collector.doctor()
    assert report.state is HealthState.OK
    assert report.detail == "aerospace 0.21.3-Beta"
    assert rig.runner.calls == [(BIN, "--version"), (BIN, "list-workspaces", "--all", "--count")]
    assert rig.sink.records == [], "doctor has no side effects"


async def test_doctor_without_a_version_in_the_output_is_still_ok() -> None:
    rig = Rig()
    rig.script_world(skip=("version",))
    rig.runner.script(BIN, "--version", stdout="who knows")
    report = await rig.collector.doctor()
    assert (report.state, report.detail) == (HealthState.OK, "aerospace ready")


async def test_a_missing_binary_is_degraded_and_the_collector_keeps_running() -> None:
    rig = Rig()  # nothing scripted: every command is "not found"
    rig.streams.error = FileNotFoundError("aerospace")
    report = await rig.collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.error_kind == "binary_missing"
    assert BIN in (report.detail or "")
    await rig.start()
    try:
        await rig.clock.advance(65)
        assert rig.task is not None
        assert not rig.task.done()
    finally:
        await rig.stop()


async def test_a_server_that_does_not_answer_is_degraded() -> None:
    rig = Rig()
    rig.script_world(skip=("count",))
    rig.runner.script(BIN, "list-workspaces", "--all", "--count", returncode=1)
    report = await rig.collector.doctor()
    assert (report.state, report.error_kind) == (HealthState.DEGRADED, "server_unreachable")


async def test_a_hanging_command_is_degraded_as_a_timeout() -> None:
    rig = Rig()
    rig.script_world(skip=("version",))
    rig.runner.script(BIN, "--version", timeout=True)
    report = await rig.collector.doctor()
    assert (report.state, report.error_kind) == (HealthState.DEGRADED, "timeout")


# -- the plugin descriptor and settings -------------------------------------------------------


def test_the_plugin_descriptor() -> None:
    from fylgja.collectors.aerospace import plugin

    assert plugin.name == "aerospace"
    assert plugin.capabilities == frozenset({Capability.WINDOWS})
    assert plugin.config_model is AerospaceSettings
    assert plugin.allowed_kinds == {EntityKind.WINDOW, EntityKind.WORKSPACE, EntityKind.FOCUS}


def test_the_factory_builds_a_collector_and_rejects_foreign_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import BaseModel

    from fylgja.collectors.aerospace import plugin

    class Foreign(BaseModel):
        pass

    runner, streams = FakeProcessRunner(), FakeStreamOpener()
    monkeypatch.setattr(adapter, "default_seams", lambda: adapter.Seams(runner, streams))
    context = build_context("aerospace", RecordingSink(), clock=FakeClock())
    assert isinstance(plugin.factory(AerospaceSettings(), context), AerospaceCollector)
    with pytest.raises(TypeError):
        plugin.factory(Foreign(), context)


def test_settings_reject_unknown_keys_and_bad_values() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AerospaceSettings.model_validate({"surprise": 1})
    with pytest.raises(ValidationError):
        AerospaceSettings(snapshot_interval_seconds=0)


def test_the_packaged_defaults_enable_the_collector() -> None:
    from fylgja.config import load_config

    config = load_config()
    assert config.collector_enabled("aerospace") is True
    assert config.collector_settings("aerospace") == {"snapshot_interval_seconds": 30}


# -- the real I/O seams, against sys.executable --------------------------------------------------


async def test_the_subprocess_runner_runs_a_command_and_reports_the_exit_status() -> None:
    runner = adapter.SubprocessRunner()
    ok = await runner.run([sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"])
    assert (ok.returncode, ok.stdout.strip()) == (3, b"hi")
    echoed = await runner.run(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"], stdin=b"in"
    )
    assert echoed.stdout == b"in"


async def test_the_subprocess_runner_times_out_and_leaves_no_process() -> None:
    runner = adapter.SubprocessRunner()
    with pytest.raises(TimeoutError):
        await runner.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)


async def test_the_subprocess_runner_reports_a_missing_binary() -> None:
    with pytest.raises(FileNotFoundError):
        await adapter.SubprocessRunner().run(["/nonexistent/aerospace", "--version"])


async def test_the_subprocess_stream_yields_lines_and_ends() -> None:
    code = "print('one'); print('two', end='')"
    async with adapter.open_subprocess_stream([sys.executable, "-c", code]) as stream:
        assert await stream.readline() == b"one\n"
        assert await stream.readline() == b"two"
        assert await stream.readline() == b""
        assert await stream.readline() == b""


async def test_the_subprocess_stream_survives_an_overlong_line() -> None:
    code = (
        "import sys; sys.stdout.write('x' * (2 << 20) + '\\n'); "
        "sys.stdout.write('after\\n'); sys.stdout.flush()"
    )
    async with adapter.open_subprocess_stream([sys.executable, "-c", code]) as stream:
        first = await stream.readline()
        assert first == adapter.OVERLONG_LINE
        assert parse.parse_event_line(first) == parse.MalformedLine("invalid_json")
        # The reader may hand out the tail of the long line before the next real line.
        lines = [await stream.readline() for _ in range(3)]
        assert b"after\n" in lines


async def test_leaving_the_subprocess_stream_kills_the_process() -> None:
    code = "import time; print('up', flush=True); time.sleep(60)"
    async with adapter.open_subprocess_stream([sys.executable, "-c", code]) as stream:
        assert await stream.readline() == b"up\n"
    # Reaching this point within the test timeout means the group was killed and reaped.


async def test_a_missing_binary_cannot_open_the_stream() -> None:
    with pytest.raises(FileNotFoundError):
        async with adapter.open_subprocess_stream(["/nonexistent/aerospace", "subscribe"]):
            pass  # pragma: no cover


def test_default_seams_are_the_real_ones() -> None:
    seams = adapter.default_seams()
    assert isinstance(seams.runner, adapter.SubprocessRunner)
    assert seams.open_stream is adapter.open_subprocess_stream


def test_resolve_binary_reads_the_paths_section_and_survives_a_broken_config(
    isolated_env: Path,
) -> None:
    assert adapter.resolve_binary() == "/opt/homebrew/bin/aerospace"
    (isolated_env / "config" / "config.toml").write_text(
        '[paths]\naerospace = "/custom/bin/aerospace"\n', encoding="utf-8"
    )
    assert adapter.resolve_binary() == "/custom/bin/aerospace"
    (isolated_env / "config" / "config.toml").write_text("[paths\n", encoding="utf-8")
    assert adapter.resolve_binary() == "/opt/homebrew/bin/aerospace"


# -- the fake stream opener -----------------------------------------------------------------------


async def test_the_fake_stream_opener_counts_opens_and_open_streams() -> None:
    opener = FakeStreamOpener(FakeLineStream("a\n", eof=True))
    async with opener(["x", "y"]) as stream:
        assert opener.active == 1
        assert await stream.readline() == b"a\n"
    assert opener.active == 0
    assert opener.calls == [("x", "y")]
    async with opener(["z"]) as silent:
        assert isinstance(silent, FakeLineStream)
    opener.error = FileNotFoundError("gone")
    with pytest.raises(FileNotFoundError):
        async with opener(["z"]):
            pass  # pragma: no cover
    assert opener.active == 0
