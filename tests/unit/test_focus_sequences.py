"""The exact command sequences of the focus actions, per host, on a fake process runner."""

from __future__ import annotations

from typing import Any

import pytest

from fylgja.config import Config, parse_config
from fylgja.core.focus import (
    APPLESCRIPT_TIMEOUT,
    CMUX_CLI,
    CMUX_SCRIPT,
    ITERM2_SCRIPT,
    OSASCRIPT,
    FocusActions,
    FocusFailed,
)
from fylgja.testing.fakes import FakeProcessRunner

AERO = "/opt/homebrew/bin/aerospace"
SURFACE = "7A1B2C3D-0000-4000-8000-00000000AAAA"
CMUX_WS = "11111111-2222-4333-8444-555555555555"
ITERM_UUID = "ABCDEF01-2345-4678-89AB-CDEF01234567"
KEY = "claude:abc"


def _config(user: dict[str, Any] | None = None) -> Config:
    return parse_config(user or {}, source="test.toml")


def _ok_runner() -> FakeProcessRunner:
    runner = FakeProcessRunner()
    runner.script(AERO)
    runner.script(OSASCRIPT)
    runner.script(CMUX_CLI)
    return runner


async def _session(actions: FocusActions, host: str | None, **extra: str | None) -> tuple[str, str]:
    result = await actions.focus_session(
        key=KEY, window_id="42", workspace="2", host_kind=host, **extra
    )
    return result.focused, result.via


PREFIX = [(AERO, "workspace", "2"), (AERO, "focus", "--window-id", "42")]


async def test_window_focus_is_one_aerospace_call() -> None:
    runner = _ok_runner()
    result = await FocusActions(runner, _config()).focus_window("42")
    assert runner.calls == [(AERO, "focus", "--window-id", "42")]
    assert result.as_json() == {"focused": "window", "via": "aerospace"}


@pytest.mark.parametrize("window_id", ["", "4x", "-1", "42;ls", "1" * 13])
async def test_window_focus_refuses_a_malformed_id(window_id: str) -> None:
    runner = _ok_runner()
    with pytest.raises(FocusFailed, match="invalid window id"):
        await FocusActions(runner, _config()).focus_window(window_id)
    assert runner.calls == []


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ({"returncode": 1}, "exit 1"),
        ({"timeout": True}, "timeout"),
        ({"error": FileNotFoundError()}, "not installed"),
        ({"error": PermissionError()}, "could not run"),
    ],
)
async def test_window_focus_failure_is_an_error(script: dict[str, Any], reason: str) -> None:
    runner = FakeProcessRunner()
    runner.script(AERO, **script)
    with pytest.raises(FocusFailed, match=reason):
        await FocusActions(runner, _config()).focus_window("42")


async def test_jetbrains_is_window_level_only() -> None:
    runner = _ok_runner()
    assert await _session(FocusActions(runner, _config()), "jetbrains") == ("window", "aerospace")
    assert runner.calls == PREFIX


async def test_unknown_and_missing_hosts_are_window_level() -> None:
    for host in ("other", "weird", None):
        runner = _ok_runner()
        assert await _session(FocusActions(runner, _config()), host) == ("window", "aerospace")
        assert runner.calls == PREFIX


async def test_workspace_failure_is_not_fatal_but_a_bad_name_is_skipped() -> None:
    runner = FakeProcessRunner()
    runner.script(AERO, "workspace", returncode=1)
    runner.script(AERO, "focus")
    actions = FocusActions(runner, _config())
    assert await _session(actions, "other") == ("window", "aerospace")
    assert len(runner.calls) == 2
    runner = _ok_runner()
    result = await FocusActions(runner, _config()).focus_session(
        key=KEY, window_id="42", workspace="--help", host_kind=None
    )
    assert result.focused == "window"
    assert runner.calls == [PREFIX[1]]
    runner = _ok_runner()
    await FocusActions(runner, _config()).focus_session(
        key=KEY, window_id="42", workspace=None, host_kind=None
    )
    assert runner.calls == [PREFIX[1]]


async def test_session_window_failure_raises() -> None:
    runner = FakeProcessRunner()
    runner.script(AERO, "workspace")
    runner.script(AERO, "focus", returncode=1)
    with pytest.raises(FocusFailed):
        await _session(FocusActions(runner, _config()), "cmux")
    assert len(runner.calls) == 2


async def test_cmux_applescript_sequence() -> None:
    runner = _ok_runner()
    result = await _session(
        FocusActions(runner, _config()), "cmux", cmux_surface=SURFACE, cmux_workspace=CMUX_WS
    )
    assert result == ("tab", "cmux-applescript")
    assert runner.calls == [*PREFIX, (OSASCRIPT, "-e", CMUX_SCRIPT, SURFACE)]
    assert runner.timeouts[-1] == APPLESCRIPT_TIMEOUT
    assert SURFACE not in CMUX_SCRIPT  # the id is an argument, never part of the script


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ({"returncode": 1}, "exit 1"),
        ({"timeout": True}, "timeout"),
        ({"error": FileNotFoundError()}, "not installed"),
    ],
)
async def test_cmux_applescript_failure_falls_back_to_window(
    script: dict[str, Any], reason: str
) -> None:
    runner = FakeProcessRunner()
    runner.script(AERO)
    runner.script(OSASCRIPT, **script)
    result = await _session(FocusActions(runner, _config()), "cmux", cmux_surface=SURFACE)
    assert result == ("window", f"aerospace (cmux: {reason})")


async def test_cmux_without_a_usable_surface_is_window_level() -> None:
    for surface in (None, "bad id;", ""):
        runner = _ok_runner()
        result = await _session(FocusActions(runner, _config()), "cmux", cmux_surface=surface)
        assert result == ("window", "aerospace (cmux: no surface id)")
        assert runner.calls == PREFIX


async def test_cmux_cli_only_sequence() -> None:
    runner = _ok_runner()
    config = _config({"collectors": {"cmux": {"applescript_focus": False, "cli_focus": True}}})
    result = await _session(
        FocusActions(runner, config), "cmux", cmux_surface=SURFACE, cmux_workspace=CMUX_WS
    )
    assert result == ("tab", "cmux-cli")
    assert runner.calls == [
        *PREFIX,
        (CMUX_CLI, "select-workspace", "--workspace", CMUX_WS),
        (CMUX_CLI, "focus-panel", "--panel", SURFACE, "--workspace", CMUX_WS),
    ]


async def test_cmux_cli_without_a_workspace_id() -> None:
    runner = _ok_runner()
    config = _config({"collectors": {"cmux": {"applescript_focus": False, "cli_focus": True}}})
    result = await _session(FocusActions(runner, config), "cmux", cmux_surface=SURFACE)
    assert result.count("tab") == 1
    assert runner.calls[-1] == (CMUX_CLI, "focus-panel", "--panel", SURFACE)


async def test_cmux_cli_refusal_after_applescript_failure_names_both() -> None:
    runner = FakeProcessRunner()
    runner.script(AERO)
    runner.script(OSASCRIPT, returncode=1)
    runner.script(CMUX_CLI, "select-workspace", returncode=1)
    config = _config({"collectors": {"cmux": {"cli_focus": True}}})
    result = await _session(
        FocusActions(runner, config), "cmux", cmux_surface=SURFACE, cmux_workspace=CMUX_WS
    )
    assert result == ("window", "aerospace (cmux: exit 1; cli exit 1)")
    assert not any("focus-panel" in call for call in runner.calls)


async def test_cmux_cli_alone_failing_names_its_reason() -> None:
    runner = FakeProcessRunner()
    runner.script(AERO)
    runner.script(CMUX_CLI, "focus-panel", returncode=1)
    config = _config({"collectors": {"cmux": {"applescript_focus": False, "cli_focus": True}}})
    result = await _session(FocusActions(runner, config), "cmux", cmux_surface=SURFACE)
    assert result == ("window", "aerospace (cmux: exit 1)")


async def test_cmux_cli_succeeds_after_applescript_failure() -> None:
    config = _config({"collectors": {"cmux": {"cli_focus": True}}})
    runner = FakeProcessRunner()
    runner.script(AERO)
    runner.script(OSASCRIPT, timeout=True)
    runner.script(CMUX_CLI)
    result = await _session(FocusActions(runner, config), "cmux", cmux_surface=SURFACE)
    assert result == ("tab", "cmux-cli")


async def test_cmux_both_routes_off_is_window_level() -> None:
    runner = _ok_runner()
    config = _config({"collectors": {"cmux": {"applescript_focus": False}}})
    result = await _session(FocusActions(runner, config), "cmux", cmux_surface=SURFACE)
    assert result == ("window", "aerospace (cmux: tab focus disabled)")
    assert runner.calls == PREFIX


def _iterm(runner: FakeProcessRunner, enabled: bool, raw: str | None) -> FocusActions:
    config = _config({"actions": {"applescript": {"iterm2": enabled}}})
    return FocusActions(
        runner,
        config,
        terminal_id=lambda key, name: raw if (key, name) == (KEY, "ITERM_SESSION_ID") else None,
    )


async def test_iterm2_off_never_calls_osascript() -> None:
    runner = _ok_runner()
    result = await _session(_iterm(runner, False, f"w0t0p0:{ITERM_UUID}"), "iterm2")
    assert result == ("window", "aerospace (iterm2: applescript disabled)")
    assert runner.calls == PREFIX


async def test_iterm2_on_selects_by_uuid() -> None:
    runner = FakeProcessRunner()
    runner.script(AERO)
    runner.script(OSASCRIPT, stdout="ok\n")
    result = await _session(_iterm(runner, True, f"w0t0p0:{ITERM_UUID}"), "iterm2")
    assert result == ("tab", "iterm2-applescript")
    assert runner.calls == [*PREFIX, (OSASCRIPT, "-e", ITERM2_SCRIPT, ITERM_UUID)]
    assert runner.timeouts[-1] == APPLESCRIPT_TIMEOUT


async def test_iterm2_accepts_a_bare_uuid() -> None:
    runner = FakeProcessRunner()
    runner.script(AERO)
    runner.script(OSASCRIPT, stdout="ok")
    assert (await _session(_iterm(runner, True, ITERM_UUID), "iterm2"))[0] == "tab"


@pytest.mark.parametrize("raw", [None, "-", "w0t0p0:not-a-uuid", f'w0t0p0:{ITERM_UUID}"; x'])
async def test_iterm2_without_a_valid_id_is_window_level(raw: str | None) -> None:
    runner = _ok_runner()
    result = await _session(_iterm(runner, True, raw), "iterm2")
    assert result == ("window", "aerospace (iterm2: no session id)")
    assert runner.calls == PREFIX


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ({"timeout": True}, "timeout"),
        ({"returncode": 1}, "exit 1"),
        ({"stdout": ""}, "session not found"),
        ({"error": FileNotFoundError()}, "not installed"),
    ],
)
async def test_iterm2_failure_falls_back_to_window(script: dict[str, Any], reason: str) -> None:
    runner = FakeProcessRunner()
    runner.script(AERO)
    runner.script(OSASCRIPT, **script)
    result = await _session(_iterm(runner, True, ITERM_UUID), "iterm2")
    assert result == ("window", f"aerospace (iterm2: {reason})")
