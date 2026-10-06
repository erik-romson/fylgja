"""Doctor and the AppleScript switches (WP5.3, §4.4).

Every osascript runs with a 3 s timeout. A timeout means a permission dialog is probably pending:
doctor reports the switch as degraded and records it as disabled until doctor is rerun; a rerun
that gets an answer enables it again. A switch that is off runs nothing.
"""

from __future__ import annotations

import asyncio
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from appcontext_support import T0, World, window

from fylgja.cli.doctor import Check, Status, check_applescript, run_doctor
from fylgja.cli.install import Layout
from fylgja.collectors.appcontext import switches
from fylgja.config import Config, parse_config
from fylgja.testing.fakes import FakeProcessRunner


def _config(**on: bool) -> Config:
    return parse_config({"actions": {"applescript": on}}, source="test")


def _run(
    config: Config, runner: FakeProcessRunner | None, state: Path, now: datetime = T0
) -> dict[str, Check]:
    checks = asyncio.run(check_applescript(config, runner=runner, now=lambda: now, state=state))
    return {c.name: c for c in checks}


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state" / "applescript-switches.json"


def test_switches_off_run_nothing_and_write_nothing(state: Path) -> None:
    runner = FakeProcessRunner()
    checks = _run(_config(), runner, state)
    assert set(checks) == {"applescript:chrome", "applescript:iterm2"}
    assert all(c.status is Status.OK and c.detail.startswith("off (") for c in checks.values())
    assert runner.calls == []
    assert not state.exists()


def test_a_probe_that_answers_enables_the_switch(state: Path) -> None:
    runner = FakeProcessRunner()
    runner.script(switches.OSASCRIPT, stdout=b"")
    checks = _run(_config(chrome=True), runner, state)
    assert checks["applescript:chrome"].status is Status.OK
    assert "within 3 s" in checks["applescript:chrome"].detail
    assert checks["applescript:iterm2"].detail.startswith("off (")
    assert runner.timeouts == [3.0]
    assert len(runner.calls) == 1  # only the switch that is on
    assert runner.calls[0] == tuple(switches.probe_argv("chrome"))
    assert switches.parse_records(state.read_text())["chrome"].ok is True
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o600


def test_a_timeout_is_degraded_and_disables_the_switch_until_rerun(state: Path) -> None:
    runner = FakeProcessRunner()
    runner.script(switches.OSASCRIPT, timeout=True)
    checks = _run(_config(chrome=True, iterm2=True), runner, state)

    for name in ("applescript:chrome", "applescript:iterm2"):
        check = checks[name]
        assert check.status is Status.WARN
        assert "degraded" in check.detail
        assert "did not answer within 3 s" in check.detail
        assert "permission dialog is likely pending" in check.detail
        assert "disabled until doctor is rerun" in check.detail
    assert runner.timeouts == [3.0, 3.0]
    records = switches.parse_records(state.read_text())
    assert {name: (r.ok, r.reason) for name, r in records.items()} == {
        "chrome": (False, "applescript_timeout"),
        "iterm2": (False, "applescript_timeout"),
    }

    # rerun after the dialog was answered: only the switch that now answers is enabled again
    later = T0 + timedelta(minutes=2)
    answering = FakeProcessRunner()
    answering.script(*switches.probe_argv("chrome"), stdout=b"")
    answering.script(*switches.probe_argv("iterm2"), timeout=True)
    rerun = _run(_config(chrome=True, iterm2=True), answering, state, later)
    assert rerun["applescript:chrome"].status is Status.OK
    assert rerun["applescript:iterm2"].status is Status.WARN
    records = switches.parse_records(state.read_text())
    assert records["chrome"].ok is True
    assert records["chrome"].at == later
    assert records["iterm2"].ok is False


def test_a_refused_permission_is_degraded_too(state: Path) -> None:
    runner = FakeProcessRunner()
    runner.script(
        switches.OSASCRIPT, returncode=1, stderr="execution error: Not authorized (-1743)"
    )
    check = _run(_config(chrome=True), runner, state)["applescript:chrome"]
    assert check.status is Status.WARN
    assert "permission was refused" in check.detail
    assert switches.parse_records(state.read_text())["chrome"].reason == "not_authorized"


def test_another_failure_is_a_warning_and_leaves_the_record_alone(state: Path) -> None:
    runner = FakeProcessRunner()
    runner.script(switches.OSASCRIPT, returncode=1, stderr="syntax error")
    check = _run(_config(chrome=True), runner, state)["applescript:chrome"]
    assert check.status is Status.WARN
    assert "exited 1" in check.detail
    assert not state.exists()


def test_a_missing_osascript_and_an_os_error_are_warnings(state: Path) -> None:
    missing = _run(_config(chrome=True), FakeProcessRunner(), state)["applescript:chrome"]
    assert missing.status is Status.WARN
    assert "missing" in missing.detail
    broken = FakeProcessRunner()
    broken.script(switches.OSASCRIPT, error=PermissionError(13, "Permission denied"))
    check = _run(_config(chrome=True), broken, state)["applescript:chrome"]
    assert check.status is Status.WARN
    assert "could not run" in check.detail
    assert not state.exists()


def test_without_a_runner_a_switch_that_is_on_is_not_checked(state: Path) -> None:
    check = _run(_config(chrome=True), None, state)["applescript:chrome"]
    assert check.status is Status.WARN
    assert "no process runner" in check.detail


def test_an_unwritable_record_is_reported_in_the_check(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    runner = FakeProcessRunner()
    runner.script(switches.OSASCRIPT, timeout=True)
    check = _run(_config(chrome=True), runner, blocker / "applescript-switches.json")
    assert "could not be written" in check["applescript:chrome"].detail


def test_the_default_state_file_is_the_one_the_collector_reads(tmp_path: Path) -> None:
    runner = FakeProcessRunner()
    runner.script(switches.OSASCRIPT, timeout=True)
    asyncio.run(check_applescript(_config(chrome=True), runner=runner, now=lambda: T0))
    assert switches.state_file().is_file()
    assert switches.parse_records(switches.state_file().read_text())["chrome"].ok is False


async def test_the_collector_follows_what_doctor_wrote(tmp_path: Path) -> None:
    """End to end: a doctor timeout disables the switch in the collector; a rerun enables it."""
    runner = FakeProcessRunner()
    runner.script(switches.OSASCRIPT, timeout=True)
    await check_applescript(_config(chrome=True), runner=runner, now=lambda: T0)

    world = World(chrome=True)
    world.fs.add_file(switches.state_file(), switches.state_file().read_text())
    world.runner.script(switches.OSASCRIPT, stdout=b"https://example.org/\n")
    world.source.windows = [window("1", "com.google.Chrome", "Page - Google Chrome")]
    world.source.focus_on("1")
    await world.collector.poll()
    assert world.osascript_calls == []

    answering = FakeProcessRunner()
    answering.script(switches.OSASCRIPT, stdout=b"")
    await check_applescript(
        _config(chrome=True), runner=answering, now=lambda: T0 + timedelta(minutes=1)
    )
    world.fs.add_file(switches.state_file(), switches.state_file().read_text())
    world.source.focus_on("1")
    await world.collector.poll()
    assert len(world.osascript_calls) == 1


def test_run_doctor_ends_with_the_switch_lines(tmp_path: Path) -> None:
    layout = Layout(home=tmp_path / "home", dotfiles=tmp_path / "home" / "dotfiles")
    layout.home.mkdir(parents=True, exist_ok=True)

    def down(path: str, **_: Any) -> Any:
        from fylgja.cli import client

        raise client.DaemonDown("u", "refused")

    runner = FakeProcessRunner()
    checks = asyncio.run(
        run_doctor(
            layout=layout, fetch=down, probe=lambda port: False, source=lambda: [], runner=runner
        )
    )
    assert [c.name for c in checks[-2:]] == ["applescript:chrome", "applescript:iterm2"]
    assert all(c.status is Status.OK for c in checks[-2:])
    assert not any(call and call[0] == switches.OSASCRIPT for call in runner.calls)
