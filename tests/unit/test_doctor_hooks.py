"""The WP2.4 doctor checks in their ok, warn and fail forms, and the guarded hook command.

Everything runs against a fake ``$HOME`` and fake processes. The Lidskjalv check is
informational (A21): health is the same with and without Lidskjalv's entries.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from dotfiles_support import (
    CLAUDE_VERSION_OUTPUT,
    GIT_BIN,
    hooks_runner,
    load_settings,
    settings_source,
    statusline_source,
    without_lidskjalv,
    write_settings,
)

from fylgja import paths
from fylgja.cli import client
from fylgja.cli.doctor import (
    Check,
    Status,
    check_claude_version,
    check_hook_async,
    check_hook_executable,
    check_hooks_committed,
    check_inbox,
    check_lidskjalv_hooks,
    exit_code,
    fylgja_registrations,
    read_settings,
    render_checks,
    render_json,
    run_doctor,
)
from fylgja.cli.install import (
    Layout,
    fylgja_hook_command,
    run_install_hooks,
    run_install_statusline,
)
from fylgja.testing.fakes import FakeProcessRunner

NOW = 1_800_000_000.0


def _registrations(layout: Layout) -> list[tuple[str, dict[str, Any]]]:
    return fylgja_registrations(read_settings(layout))


def _install(layout: Layout) -> None:
    async def both() -> None:
        lines: list[str] = []
        runner = hooks_runner()
        await run_install_hooks(apply=True, runner=runner, layout=layout, echo=lines.append)
        await run_install_statusline(apply=True, runner=runner, layout=layout, echo=lines.append)

    asyncio.run(both())


def _stub_executable(layout: Layout) -> Path:
    target = layout.home / ".local" / "bin" / "fylgja"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    return target


def _inbox() -> Path:
    path = paths.inbox_dir("claude")
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


# --------------------------------------------------------------------------------------------
# The guarded command
# --------------------------------------------------------------------------------------------


def _sh(command: str, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        timeout=20,
        check=False,
    )


def test_the_guarded_command_exits_zero_with_no_output_when_fylgja_is_missing(
    isolated_env: Path,
) -> None:
    home = isolated_env / "home"
    assert not (home / ".local" / "bin" / "fylgja").exists()
    result = _sh(fylgja_hook_command("Stop"), home)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_the_guarded_command_exits_zero_even_for_a_dangling_link(isolated_env: Path) -> None:
    home = isolated_env / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "fylgja").symlink_to(home / "nowhere")
    result = _sh(fylgja_hook_command("SessionEnd"), home)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_the_guarded_command_runs_fylgja_with_hook_and_the_event_when_present(
    fake_home: Layout, tmp_path: Path
) -> None:
    log = tmp_path / "args"
    stub = _stub_executable(fake_home)
    stub.write_text(f'#!/bin/sh\necho "$@" > {log}\nexit 7\n', encoding="utf-8")
    result = _sh(fylgja_hook_command("PostToolUse"), fake_home.home)
    assert result.returncode == 0  # whatever fylgja returns, the command never fails Claude
    assert log.read_text(encoding="utf-8") == "hook PostToolUse\n"


def test_every_installed_command_is_the_guarded_form(fake_home: Layout) -> None:
    _install(fake_home)
    registrations = _registrations(fake_home)
    assert registrations
    for event, hook in registrations:
        assert hook["command"] == fylgja_hook_command(event)
        assert result_ok(hook["command"], fake_home.home)


def result_ok(command: str, home: Path) -> bool:
    outcome = _sh(command, home)
    return (outcome.returncode, outcome.stdout, outcome.stderr) == (0, "", "")


# --------------------------------------------------------------------------------------------
# Settings and registrations
# --------------------------------------------------------------------------------------------


def test_read_settings_returns_none_for_missing_malformed_and_non_objects(
    fake_home: Layout,
) -> None:
    assert isinstance(read_settings(fake_home), dict)
    settings_source(fake_home).write_text("{ nope", encoding="utf-8")
    assert read_settings(fake_home) is None
    settings_source(fake_home).write_text("[1]", encoding="utf-8")
    assert read_settings(fake_home) is None
    settings_source(fake_home).unlink()
    assert read_settings(fake_home) is None
    assert fylgja_registrations(None) == []


def test_registrations_are_the_fylgja_commands_only(fake_home: Layout) -> None:
    assert _registrations(fake_home) == []  # the fixture has only Lidskjalv's and the formatter
    _install(fake_home)
    assert len(_registrations(fake_home)) == 19


# --------------------------------------------------------------------------------------------
# hooks:executable
# --------------------------------------------------------------------------------------------


def test_executable_is_ok_when_nothing_is_registered(fake_home: Layout) -> None:
    check = check_hook_executable(False, fake_home)
    assert check.status is Status.OK
    assert "nothing runs it" in check.detail


def test_executable_ok_when_present_and_executable(fake_home: Layout) -> None:
    _stub_executable(fake_home)
    check = check_hook_executable(True, fake_home)
    assert (check.name, check.status) == ("hooks:executable", Status.OK)
    assert "~/.local/bin/fylgja is executable" in check.detail


def test_executable_warns_when_it_is_elsewhere_on_path(fake_home: Layout) -> None:
    check = check_hook_executable(True, fake_home, which=lambda name: "/opt/bin/fylgja")
    assert check.status is Status.WARN
    assert "/opt/bin/fylgja" in check.detail
    assert "ln -s /opt/bin/fylgja" in check.detail


def test_executable_fails_when_missing_everywhere(fake_home: Layout) -> None:
    check = check_hook_executable(True, fake_home, which=lambda name: None)
    assert check.status is Status.FAIL
    assert "missing" in check.detail


def test_executable_fails_when_not_executable_or_dangling(fake_home: Layout) -> None:
    target = _stub_executable(fake_home)
    target.chmod(0o644)
    assert check_hook_executable(True, fake_home).status is Status.FAIL
    target.unlink()
    target.symlink_to(fake_home.home / "gone")
    dangling = check_hook_executable(True, fake_home)
    assert dangling.status is Status.FAIL
    assert "dangling" in dangling.detail


# --------------------------------------------------------------------------------------------
# hooks:async
# --------------------------------------------------------------------------------------------


def test_async_check_forms(fake_home: Layout) -> None:
    none = check_hook_async([])
    assert none.status is Status.OK
    assert "no Fylgja hooks registered" in none.detail
    _install(fake_home)
    ok = check_hook_async(_registrations(fake_home))
    assert ok.status is Status.OK
    assert "19 registrations over 19 events, all async" in ok.detail

    settings = load_settings(fake_home)
    for event, hook in fylgja_registrations(settings):
        if event in ("Stop", "SessionEnd"):
            hook["async"] = False
        if event == "PreCompact":
            del hook["async"]
    write_settings(fake_home, settings)
    bad = check_hook_async(_registrations(fake_home))
    assert bad.status is Status.FAIL
    assert "PreCompact" in bad.detail
    assert "SessionEnd" in bad.detail
    assert "Stop" in bad.detail
    assert "block Claude Code" in bad.detail


# --------------------------------------------------------------------------------------------
# claude-version
# --------------------------------------------------------------------------------------------


def test_claude_version_forms(fake_home: Layout) -> None:
    def run(registrations: Any, runner: FakeProcessRunner | None) -> Check:
        return asyncio.run(check_claude_version(registrations, runner))

    assert run([], hooks_runner()).status is Status.OK
    _install(fake_home)
    registrations = _registrations(fake_home)
    assert "no process runner" in run(registrations, None).detail

    ok = run(registrations, hooks_runner())
    assert ok.status is Status.OK
    assert "Claude Code 2.1.289 supports all 19 events" in ok.detail

    old = run(registrations, hooks_runner("2.1.200 (Claude Code)\n"))
    assert old.status is Status.WARN
    assert "PostModelSwitch" in old.detail

    missing = FakeProcessRunner()  # no claude on PATH
    gone = run(registrations, missing)
    assert gone.status is Status.WARN
    assert "cannot run `claude --version`" in gone.detail
    assert CLAUDE_VERSION_OUTPUT.startswith("2.1.289")


# --------------------------------------------------------------------------------------------
# The inbox: owner and mode, backlog age, dropped.count
# --------------------------------------------------------------------------------------------


def _by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def test_a_missing_inbox_is_ok_everywhere(fake_home: Layout) -> None:
    checks = _by_name(check_inbox(fake_home, now=lambda: NOW))
    assert set(checks) == {"inbox:access", "inbox:backlog", "inbox:dropped"}
    assert all(c.status is Status.OK for c in checks.values())
    assert "no inbox yet" in checks["inbox:access"].detail


def test_access_ok_for_a_private_directory_and_fails_for_every_refusal(
    fake_home: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = _inbox()
    ok = _by_name(check_inbox(fake_home, now=lambda: NOW))["inbox:access"]
    assert ok.status is Status.OK
    assert "mode 0700" in ok.detail

    inbox.chmod(0o755)
    mode = _by_name(check_inbox(fake_home, now=lambda: NOW))["inbox:access"]
    assert mode.status is Status.FAIL
    assert "mode 0755" in mode.detail
    assert "chmod 700" in mode.detail
    inbox.chmod(0o700)

    with monkeypatch.context() as patch:
        patch.setattr(os, "getuid", lambda: os.stat(inbox).st_uid + 1)
        owner = _by_name(check_inbox(fake_home, now=lambda: NOW))["inbox:access"]
    assert owner.status is Status.FAIL
    assert "owned by uid" in owner.detail

    inbox.rmdir()
    target = fake_home.home / "elsewhere"
    target.mkdir(mode=0o700)
    inbox.symlink_to(target)
    link = _by_name(check_inbox(fake_home, now=lambda: NOW))["inbox:access"]
    assert link.status is Status.FAIL
    assert "not a plain directory" in link.detail


def test_access_fails_for_a_statusline_directory_the_hook_would_refuse(fake_home: Layout) -> None:
    _inbox()
    snapshots = paths.statusline_dir()
    snapshots.mkdir(parents=True, mode=0o755)
    snapshots.chmod(0o755)
    check = _by_name(check_inbox(fake_home, now=lambda: NOW))["inbox:access"]
    assert check.status is Status.FAIL
    assert "statusline" in check.detail


def _envelope(inbox: Path, name: str, age: float) -> None:
    path = inbox / name
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (NOW - age, NOW - age))


def test_backlog_age_ok_warn_and_fail(fake_home: Layout) -> None:
    inbox = _inbox()

    def backlog() -> Check:
        return _by_name(check_inbox(fake_home, now=lambda: NOW))["inbox:backlog"]

    assert backlog().status is Status.OK
    assert "empty" in backlog().detail
    _envelope(inbox, "1-a.json", 20)
    _envelope(inbox, ".tmp-ignored", 99999)
    fresh = backlog()
    assert fresh.status is Status.OK
    assert "1 events waiting, the oldest 20 s old" in fresh.detail
    _envelope(inbox, "2-b.json", 600)
    stale = backlog()
    assert stale.status is Status.WARN
    assert "2 events waiting, the oldest 600 s old" in stale.detail
    _envelope(inbox, "3-c.json", 7200)
    dead = backlog()
    assert dead.status is Status.FAIL
    assert "not draining" in dead.detail


def test_a_full_backlog_stamp_fails_even_when_young(fake_home: Layout) -> None:
    inbox = _inbox()
    _envelope(inbox, "1-a.json", 1)
    (inbox / ".backlog-stamp").write_bytes(b"full\n")
    check = _by_name(check_inbox(fake_home, now=lambda: NOW))["inbox:backlog"]
    assert check.status is Status.FAIL
    assert "drops new ones" in check.detail


def test_an_unreadable_inbox_warns(fake_home: Layout) -> None:
    inbox = _inbox()
    inbox.chmod(0o000)
    try:
        check = _by_name(check_inbox(fake_home, now=lambda: NOW))["inbox:backlog"]
    finally:
        inbox.chmod(0o700)
    assert check.status is Status.WARN
    assert "cannot read the inbox" in check.detail


def test_dropped_count_forms(fake_home: Layout) -> None:
    inbox = _inbox()

    def dropped() -> Check:
        return _by_name(check_inbox(fake_home, now=lambda: NOW))["inbox:dropped"]

    assert dropped().status is Status.OK
    (inbox / "dropped.count").write_text("0\n", encoding="utf-8")
    assert dropped().status is Status.OK
    (inbox / "dropped.count").write_text("12\n", encoding="utf-8")
    some = dropped()
    assert some.status is Status.WARN
    assert "12 events were dropped" in some.detail
    (inbox / "dropped.count").write_text("5000\n", encoding="utf-8")
    assert dropped().status is Status.FAIL
    (inbox / "dropped.count").write_text("lots\n", encoding="utf-8")
    garbage = dropped()
    assert garbage.status is Status.WARN
    assert "not a number" in garbage.detail
    (inbox / "dropped.count").write_text("\n", encoding="utf-8")
    assert dropped().status is Status.WARN
    (inbox / "dropped.count").unlink()
    (inbox / "dropped.count").mkdir()  # unreadable as a file
    assert dropped().status is Status.WARN


# --------------------------------------------------------------------------------------------
# dotfiles:hooks-committed (Q10: a warning only)
# --------------------------------------------------------------------------------------------


def _committed(layout: Layout, runner: FakeProcessRunner | None) -> Check | None:
    return asyncio.run(check_hooks_committed(layout, runner, GIT_BIN))


def test_committed_check_is_silent_until_fylgja_has_edited_something(fake_home: Layout) -> None:
    runner = FakeProcessRunner()
    assert _committed(fake_home, runner) is None
    assert _committed(fake_home, None) is None
    assert runner.calls == []


def test_committed_check_ok_and_warn(fake_home: Layout) -> None:
    _install(fake_home)
    (fake_home.dotfiles / ".git").mkdir()
    clean = FakeProcessRunner()
    clean.script(GIT_BIN, "-C", stdout="")
    ok = _committed(fake_home, clean)
    assert ok is not None
    assert (ok.name, ok.status) == ("dotfiles:hooks-committed", Status.OK)
    assert clean.calls == [
        (
            GIT_BIN,
            "-C",
            str(fake_home.dotfiles),
            "status",
            "--porcelain",
            "--",
            "claude/.claude/settings.json",
            "claude/.claude/statusline.sh",
        )
    ]

    dirty = FakeProcessRunner()
    dirty.script(GIT_BIN, "-C", stdout=" M claude/.claude/settings.json\n")
    warn = _committed(fake_home, dirty)
    assert warn is not None
    assert warn.status is Status.WARN
    assert "claude/.claude/settings.json" in warn.detail
    assert "never commits" in warn.detail
    assert all("commit" not in call for call in dirty.calls)


def test_committed_check_reads_each_file_separately(fake_home: Layout) -> None:
    asyncio.run(
        run_install_statusline(
            apply=True, runner=hooks_runner(), layout=fake_home, echo=lambda _: None
        )
    )
    (fake_home.dotfiles / ".git").mkdir()
    runner = FakeProcessRunner()
    runner.script(GIT_BIN, "-C", stdout="")
    assert _committed(fake_home, runner) is not None
    assert runner.calls[0][-1] == "claude/.claude/statusline.sh"


def test_committed_check_tolerates_broken_files_and_a_missing_git(fake_home: Layout) -> None:
    settings_source(fake_home).write_text("[1]", encoding="utf-8")
    statusline_source(fake_home).write_bytes(b"\xff\xfe")
    assert _committed(fake_home, FakeProcessRunner()) is None
    statusline_source(fake_home).write_text("# >>> fylgja >>>\n", encoding="utf-8")
    ok = _committed(fake_home, FakeProcessRunner())  # no .git: nothing to ask
    assert ok is not None
    assert ok.status is Status.OK


# --------------------------------------------------------------------------------------------
# lidskjalv-hooks: informational (A21)
# --------------------------------------------------------------------------------------------


def test_the_lidskjalv_check_is_absent_without_lidskjalv_entries(fake_home: Layout) -> None:
    assert check_lidskjalv_hooks(None, fake_home) is None
    assert check_lidskjalv_hooks(without_lidskjalv(load_settings(fake_home)), fake_home) is None


def test_the_lidskjalv_check_ok_form(fake_home: Layout) -> None:
    check = check_lidskjalv_hooks(read_settings(fake_home), fake_home)
    assert check is not None
    assert check.informational is True
    assert check.status is Status.OK
    assert "untouched, target exists" in check.detail
    assert "14 entries over 12 events" in check.detail


def test_the_lidskjalv_check_stays_untouched_after_fylgja_installs(fake_home: Layout) -> None:
    _install(fake_home)
    check = check_lidskjalv_hooks(read_settings(fake_home), fake_home)
    assert check is not None
    assert check.status is Status.OK
    assert "14 entries over 12 events" in check.detail


def test_the_lidskjalv_check_warns_for_a_missing_target_and_a_changed_entry(
    fake_home: Layout,
) -> None:
    (fake_home.dotfiles / "claude" / ".claude" / "lidskjalv.sh").unlink()
    settings = read_settings(fake_home)
    assert settings is not None
    settings["hooks"]["Stop"][0]["hooks"][0]["async"] = True
    check = check_lidskjalv_hooks(settings, fake_home)
    assert check is not None
    assert check.status is Status.WARN
    assert check.informational is True
    assert "changed from the plain registration: Stop" in check.detail
    assert "target problem" in check.detail
    assert "informational" in check.detail


def test_health_is_the_same_with_and_without_lidskjalv(fake_home: Layout) -> None:
    informational = Check("lidskjalv-hooks", Status.FAIL, "broken", informational=True)
    healthy = [Check("config", Status.OK, "fine")]
    assert exit_code(healthy) == exit_code([*healthy, informational]) == 0
    json_with = json.loads(render_json([*healthy, informational]))
    json_without = json.loads(render_json(healthy))
    assert json_with["summary"] == json_without["summary"]
    assert json_with["checks"][1] == {
        "name": "lidskjalv-hooks",
        "status": "fail",
        "detail": "broken",
        "informational": True,
    }
    table = render_checks([*healthy, informational], 100)
    assert any(line.startswith("INFO") and "lidskjalv-hooks" in line for line in table.splitlines())
    assert table.splitlines()[-1] == render_checks(healthy, 100).splitlines()[-1]
    assert exit_code([Check("x", Status.FAIL, "")]) == 1


# --------------------------------------------------------------------------------------------
# run_doctor: after the installers, with and without Lidskjalv
# --------------------------------------------------------------------------------------------


def _doctor(layout: Layout, runner: FakeProcessRunner) -> list[Check]:
    def down(path: str, *, url: str | None = None, timeout: float | None = None) -> Any:
        raise client.DaemonDown("u", "refused")

    return asyncio.run(
        run_doctor(
            runner=runner,
            layout=layout,
            fetch=down,
            probe=lambda port: False,
            source=lambda: [],
            git=GIT_BIN,
            now=lambda: NOW,
            which=lambda name: None,
        )
    )


def test_doctor_is_healthy_after_install_and_names_every_hook_check(fake_home: Layout) -> None:
    _install(fake_home)
    _stub_executable(fake_home)
    (fake_home.dotfiles / ".git").mkdir()
    runner = hooks_runner()
    runner.script(GIT_BIN, "-C", stdout=" M claude/.claude/settings.json\n")
    checks = _doctor(fake_home, runner)
    by_name = {c.name: c for c in checks}
    for name in (
        "hooks:executable",
        "hooks:async",
        "claude-version",
        "inbox:access",
        "inbox:backlog",
        "inbox:dropped",
        "dotfiles:hooks-committed",
        "lidskjalv-hooks",
    ):
        assert name in by_name, name
    assert by_name["hooks:executable"].status is Status.OK
    assert by_name["hooks:async"].status is Status.OK
    assert by_name["claude-version"].status is Status.OK
    assert by_name["dotfiles:hooks-committed"].status is Status.WARN  # uncommitted: a warning
    assert by_name["lidskjalv-hooks"].informational is True
    assert exit_code(checks) == 0
    assert not any(c.status is Status.FAIL for c in checks if not c.informational)


def test_doctor_passes_the_same_against_settings_with_no_lidskjalv_entries(
    fake_home: Layout,
) -> None:
    write_settings(fake_home, without_lidskjalv(load_settings(fake_home)))
    _install(fake_home)
    _stub_executable(fake_home)
    checks = _doctor(fake_home, hooks_runner())
    names = [c.name for c in checks]
    assert "lidskjalv-hooks" not in names
    assert "hooks:async" in names
    assert exit_code(checks) == 0
    assert {c.name: c.status for c in checks if c.name.startswith(("hooks:", "claude"))} == {
        "hooks:executable": Status.OK,
        "hooks:async": Status.OK,
        "claude-version": Status.OK,
    }


def test_doctor_health_does_not_depend_on_lidskjalv(fake_home: Layout) -> None:
    _install(fake_home)
    _stub_executable(fake_home)
    with_lidskjalv = _doctor(fake_home, hooks_runner())
    (fake_home.dotfiles / "claude" / ".claude" / "lidskjalv.sh").unlink()  # break its target
    broken = _doctor(fake_home, hooks_runner())
    lidskjalv = next(c for c in broken if c.name == "lidskjalv-hooks")
    assert lidskjalv.status is Status.WARN
    own = [c for c in broken if not c.informational and c.name != "hook-commands"]
    assert [c for c in own if c.status is not Status.OK] == [
        c
        for c in with_lidskjalv
        if not c.informational and c.status is not Status.OK and c.name != "hook-commands"
    ]


def test_doctor_fails_when_a_registered_hook_would_block_or_have_no_executable(
    fake_home: Layout,
) -> None:
    _install(fake_home)
    settings = load_settings(fake_home)
    next(h for e, h in fylgja_registrations(settings) if e == "Stop")["async"] = False
    write_settings(fake_home, settings)
    checks = {c.name: c for c in _doctor(fake_home, hooks_runner())}
    assert checks["hooks:async"].status is Status.FAIL
    assert checks["hooks:executable"].status is Status.FAIL
    assert exit_code(list(checks.values())) == 1
