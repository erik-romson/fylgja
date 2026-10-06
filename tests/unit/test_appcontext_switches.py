"""The AppleScript switches of the ``appcontext`` collector (WP5.3, §4.4).

With a switch off, no ``osascript`` command line is ever produced. A timeout is read as "a
permission dialog is probably pending": it disables only its own switch, marks it degraded and
keeps it disabled until doctor is rerun, while every other context source keeps reporting.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import timedelta
from pathlib import Path

from appcontext_support import (
    CHROME,
    CMUX,
    CMUX_STORE,
    ITERM2,
    OBSIDIAN,
    OBSIDIAN_JSON,
    PYCHARM,
    T0,
    World,
    cmux_store,
    obsidian_config,
    window,
)

from fylgja.collectors.appcontext import adapter, switches
from fylgja.collectors.appcontext.switches import SwitchRecord
from fylgja.models import HealthState
from fylgja.testing.fakes import FakeFS


def _all_apps(world: World) -> None:
    """One window of each app that has a second tier; focus moves through them all."""
    world.fs.add_file(CMUX_STORE, cmux_store({"ws": ("surface", "/work/alpha", 1.0)}))
    world.fs.add_file(OBSIDIAN_JSON, obsidian_config({"v": ("/vaults/Notes", 1)}))
    world.source.windows = [
        window("1", CHROME, "Page - Google Chrome - Person 1"),
        window("2", ITERM2, "shell"),
        window("3", CMUX, "alpha"),
        window("4", PYCHARM, "proj [~/proj] – main.py"),
        window("5", OBSIDIAN, "Daily - Notes - Obsidian v1.13.7"),
    ]


async def _visit_every_window(world: World) -> None:
    for window_id in ("1", "2", "3", "4", "5"):
        world.source.focus_on(window_id)
        await world.collector.poll()


def _write_state(world: World, **records: SwitchRecord) -> None:
    body = {
        name: {"ok": record.ok, "at": record.at.isoformat(), "reason": record.reason}
        for name, record in records.items()
    }
    world.fs.add_file(switches.state_file(), json.dumps(body))


# --- switches off: no osascript, ever -------------------------------------------------------------


async def test_with_both_switches_off_no_osascript_argv_is_produced() -> None:
    world = World()
    _all_apps(world)
    await _visit_every_window(world)
    assert world.collector.tier2_runs == 5
    assert world.osascript_calls == []
    assert world.runner.calls == []  # no process at all: the other sources are file reads
    assert world.items("chrome") == []
    assert world.items("iterm2") == []
    assert world.items("cmux")  # the sources that need no switch keep reporting
    assert world.items("jetbrains")
    assert world.items("obsidian")


async def test_one_switch_on_calls_only_its_own_script() -> None:
    world = World(chrome=True)
    world.runner.script(switches.OSASCRIPT, stdout=b"https://example.org/\n")
    _all_apps(world)
    await _visit_every_window(world)
    assert len(world.osascript_calls) == 1
    assert 'application "Google Chrome"' in world.osascript_calls[0][2]
    assert world.items("iterm2") == []


def test_osascript_argv_is_made_in_one_place_with_a_fixed_binary() -> None:
    for switch in switches.SWITCHES:
        argv = switches.osascript_argv(switch)
        assert argv[:2] == [switches.OSASCRIPT, "-e"]
        assert len(argv) == 3
        assert switches.probe_argv(switch) == argv
        assert " is running" in argv[2]  # the script never launches the app
    assert switches.APPLESCRIPT_TIMEOUT == 3.0


def test_the_switch_config_defaults_are_off() -> None:
    off = adapter.AppleScriptSwitches()
    assert (off.chrome, off.iterm2, off.enabled("other")) == (False, False, False)
    assert adapter.configured_switches() == off  # the packaged defaults


# --- a timeout disables only its own switch -------------------------------------------------------


async def test_a_chrome_timeout_disables_only_the_chrome_switch() -> None:
    world = World(chrome=True, iterm2=True)
    world.runner.script(switches.OSASCRIPT, timeout=True, times=1)  # Chrome hangs first
    world.runner.script(switches.OSASCRIPT, stdout=b"/Users/erik/code\nvim\n")
    _all_apps(world)
    await _visit_every_window(world)

    report = await world.collector.doctor()
    assert report.state is HealthState.DEGRADED
    assert report.error_kind == "applescript_chrome_applescript_timeout"
    assert "applescript:chrome" in (report.detail or "")
    assert "rerun `fylgja doctor`" in (report.detail or "")
    assert "applescript:iterm2" not in (report.detail or "")

    # every other context source reported
    assert world.items("chrome") == []
    assert world.fields("iterm2") == {"path": "/Users/erik/code", "job": "vim"}
    for key in ("cmux", "jetbrains", "obsidian"):
        assert world.items(key), key
    assert len(world.items("title")) == 4  # tier 1 is untouched (iTerm2 has no title parser)

    # and chrome is not called again while it is disabled
    calls = len(world.osascript_calls)
    world.source.focus_on("1")
    await world.collector.poll()
    world.source.focus_on("1")
    await world.collector.poll()
    assert len(world.osascript_calls) == calls


async def test_an_iterm2_timeout_leaves_chrome_working() -> None:
    world = World(chrome=True, iterm2=True)
    world.runner.script(switches.OSASCRIPT, stdout=b"https://example.org/\n", times=1)
    world.runner.script(switches.OSASCRIPT, timeout=True)
    _all_apps(world)
    await _visit_every_window(world)
    assert world.fields("chrome")["url"] == "https://example.org/"
    report = await world.collector.doctor()
    assert report.error_kind == "applescript_iterm2_applescript_timeout"
    assert world.runner.timeouts == [3.0, 3.0]  # both scripts ran with the 3 s timeout


async def test_a_failing_script_is_a_failure_that_clears_with_the_next_good_run() -> None:
    world = World(chrome=True)
    world.runner.script(switches.OSASCRIPT, returncode=1, stderr="boom", times=1)
    world.runner.script(switches.OSASCRIPT, stdout=b"https://example.org/\n")
    world.source.windows = [window("1", CHROME, "Page - Google Chrome")]
    world.source.focus_on("1")
    await world.collector.poll()
    assert (await world.collector.doctor()).error_kind == "applescript_chrome_applescript_failed"
    world.source.focus_on("1")
    await world.collector.poll()  # a failure other than a timeout does not disable the switch
    assert (await world.collector.doctor()).state is HealthState.OK


async def test_a_missing_osascript_is_reported_without_a_crash() -> None:
    world = World(chrome=True)  # nothing scripted: the fake runner raises FileNotFoundError
    world.source.windows = [window("1", CHROME, "Page - Google Chrome")]
    world.source.focus_on("1")
    await world.collector.poll()
    assert (await world.collector.doctor()).error_kind == "applescript_chrome_applescript_missing"


async def test_an_os_error_from_the_runner_is_reported() -> None:
    world = World(chrome=True)
    world.runner.script(switches.OSASCRIPT, error=PermissionError("no"))
    world.source.windows = [window("1", CHROME, "Page - Google Chrome")]
    world.source.focus_on("1")
    await world.collector.poll()
    assert (await world.collector.doctor()).error_kind == "applescript_chrome_applescript_os_error"


# --- disabled until doctor is rerun ---------------------------------------------------------------


async def test_a_disabled_switch_stays_off_until_a_later_doctor_ok() -> None:
    world = World(chrome=True)
    world.runner.script(switches.OSASCRIPT, timeout=True, times=1)
    world.runner.script(switches.OSASCRIPT, stdout=b"https://example.org/\n")
    world.source.windows = [window("1", CHROME, "Page - Google Chrome")]
    world.source.focus_on("1")
    await world.collector.poll()
    assert len(world.osascript_calls) == 1
    disabled_at = world.clock.now()

    await world.clock.advance(10.0)
    # an old doctor result (from before the timeout) does not enable it
    _write_state(world, chrome=SwitchRecord(True, disabled_at - timedelta(seconds=5)))
    world.source.focus_on("1")
    await world.collector.poll()
    assert len(world.osascript_calls) == 1
    assert (await world.collector.doctor()).state is HealthState.DEGRADED

    # doctor was rerun after the timeout and the probe answered
    _write_state(world, chrome=SwitchRecord(True, world.clock.now()))
    world.source.focus_on("1")
    await world.collector.poll()
    assert len(world.osascript_calls) == 2
    assert world.items("chrome")
    assert (await world.collector.doctor()).state is HealthState.OK


async def test_a_doctor_timeout_disables_the_switch_from_the_start() -> None:
    world = World(chrome=True, iterm2=True)
    _write_state(world, chrome=SwitchRecord(False, T0, "applescript_timeout"))
    world.runner.script(switches.OSASCRIPT, stdout=b"/a\nvim\n")
    world.source.windows = [window("1", CHROME, "Page - Google Chrome"), window("2", ITERM2, "x")]
    world.source.focus_on("1")
    await world.collector.poll()
    world.source.focus_on("2")
    await world.collector.poll()
    assert len(world.osascript_calls) == 1  # iterm2 only
    report = await world.collector.doctor()
    assert report.error_kind == "applescript_chrome_doctor_applescript_timeout"

    await world.clock.advance(1.0)
    _write_state(world, chrome=SwitchRecord(True, world.clock.now()))
    world.source.focus_on("1")
    await world.collector.poll()
    assert len(world.osascript_calls) == 2


async def test_a_doctor_record_without_a_reason_still_disables() -> None:
    world = World(iterm2=True)
    _write_state(world, iterm2=SwitchRecord(False, T0))
    world.source.windows = [window("2", ITERM2, "x")]
    world.source.focus_on("2")
    await world.collector.poll()
    assert world.osascript_calls == []
    assert (await world.collector.doctor()).error_kind == "applescript_iterm2_doctor_failed"


async def test_an_unreadable_state_file_changes_nothing() -> None:
    world = World(chrome=True)
    world.fs.fail(switches.state_file(), PermissionError("no"))
    world.runner.script(switches.OSASCRIPT, stdout=b"https://example.org/\n")
    world.source.windows = [window("1", CHROME, "Page - Google Chrome")]
    world.source.focus_on("1")
    await world.collector.poll()
    assert len(world.osascript_calls) == 1


# --- the state file -------------------------------------------------------------------------------


def test_state_file_parsing_ignores_what_it_does_not_know() -> None:
    good = {"chrome": {"ok": False, "at": "2026-10-06T09:00:00+00:00", "reason": "r"}}
    parsed = switches.parse_records(json.dumps(good))
    assert parsed == {"chrome": SwitchRecord(False, T0, "r")}
    naive = switches.parse_records('{"iterm2": {"ok": true, "at": "2026-10-06T09:00:00"}}')
    assert naive["iterm2"].at == T0
    for bad in (
        "not json",
        "[]",
        '{"chrome": 1}',
        '{"chrome": {"ok": "yes", "at": "2026-10-06T09:00:00"}}',
        '{"chrome": {"ok": true, "at": "yesterday"}}',
        '{"chrome": {"ok": true}}',
        '{"other": {"ok": true, "at": "2026-10-06T09:00:00"}}',
    ):
        assert switches.parse_records(bad) == {}


def test_write_record_is_atomic_private_and_keeps_the_other_switch(tmp_path: Path) -> None:
    target = tmp_path / "state" / "applescript-switches.json"
    switches.write_record("chrome", ok=False, at=T0, reason="applescript_timeout", path=target)
    switches.write_record("iterm2", ok=True, at=T0, path=target)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert not target.with_name(target.name + ".tmp").exists()
    parsed = switches.parse_records(target.read_text())
    assert parsed["chrome"] == SwitchRecord(False, T0, "applescript_timeout")
    assert parsed["iterm2"] == SwitchRecord(True, T0)
    switches.write_record("chrome", ok=True, at=T0 + timedelta(seconds=1), path=target)
    assert switches.parse_records(target.read_text())["chrome"].ok is True
    assert set(json.loads(target.read_text())) == {"chrome", "iterm2"}


def test_write_record_survives_a_garbled_file(tmp_path: Path) -> None:
    target = tmp_path / "s.json"
    target.write_text("garbage")
    switches.write_record("chrome", ok=True, at=T0, path=target)
    assert switches.parse_records(target.read_text())["chrome"].ok is True


def test_the_state_file_lives_in_the_state_directory() -> None:
    assert switches.state_file().name == "applescript-switches.json"
    assert switches.state_file().parent == Path(os.environ["FYLGJA_STATE_DIR"])


def test_the_collector_reads_the_state_file_through_the_fs_seam() -> None:
    fs = FakeFS({switches.state_file(): "{}"})
    assert adapter.read_input(fs, switches.state_file(), Path("/home")) == "{}"
