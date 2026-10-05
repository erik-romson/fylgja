"""The LaunchAgent plist and ``fylgja install launchd`` (WP1.10)."""

from __future__ import annotations

import asyncio
import plistlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dotfiles_support import tree_state

from fylgja import paths
from fylgja.cli.install import (
    LAUNCHD_LABEL,
    Layout,
    launchd_plist_path,
    launchd_plist_text,
    run_install_launchd,
)
from fylgja.testing.fakes import FakeProcessRunner

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "launchd" / f"{LAUNCHD_LABEL}.plist"
UID = 501
STAMP = datetime(2026, 10, 5, 12, 30, 45, tzinfo=UTC)


@pytest.fixture
def layout(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> Layout:
    home = isolated_env / "home"
    monkeypatch.setenv("FYLGJA_STATE_DIR", str(home / ".local" / "state" / "fylgja"))
    return Layout(home, home / "dotfiles")


def _run(layout: Layout, runner: FakeProcessRunner, *, apply: bool) -> tuple[int, str]:
    lines: list[str] = []
    code = asyncio.run(
        run_install_launchd(
            apply=apply, runner=runner, layout=layout, uid=UID, now=lambda: STAMP, echo=lines.append
        )
    )
    return code, "\n".join(lines)


def _bootstrap(layout: Layout) -> tuple[str, ...]:
    return ("launchctl", "bootstrap", f"gui/{UID}", str(launchd_plist_path(layout)))


def test_the_plist_matches_the_golden_file(layout: Layout) -> None:
    expected = GOLDEN.read_text(encoding="utf-8").replace("{{HOME}}", str(layout.home))
    assert launchd_plist_text(layout) == expected


def test_the_plist_content(layout: Layout) -> None:
    document = plistlib.loads(launchd_plist_text(layout).encode("utf-8"))
    home = str(layout.home)
    assert document["Label"] == "io.github.erik-romson.fylgja"
    assert document["ProgramArguments"] == [f"{home}/.local/bin/fylgja", "serve"]
    assert document["KeepAlive"] is True
    assert document["RunAtLoad"] is True
    assert document["ThrottleInterval"] == 10
    state = str(paths.state_dir())
    assert document["StandardOutPath"].startswith(state)
    assert document["StandardErrorPath"].startswith(state)
    path = document["EnvironmentVariables"]["PATH"].split(":")
    assert path[0] == f"{home}/.local/bin"
    assert {"/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"} <= set(path)
    assert not any(part.startswith("~") for part in path)  # launchd does not expand ~


def test_the_plist_lives_in_launch_agents_and_never_in_dotfiles(layout: Layout) -> None:
    assert launchd_plist_path(layout) == (
        layout.home / "Library" / "LaunchAgents" / "io.github.erik-romson.fylgja.plist"
    )
    assert not GOLDEN.read_text(encoding="utf-8").count("dotfiles")


def test_dry_run_prints_the_plist_and_the_command_and_runs_nothing(layout: Layout) -> None:
    before = tree_state(layout.home)
    runner = FakeProcessRunner()
    code, output = _run(layout, runner, apply=False)
    assert code == 0
    assert "--- /dev/null" in output
    assert "+\t<key>Label</key>" in output
    assert "dry run" in output
    assert " ".join(_bootstrap(layout)) in output
    assert runner.calls == []
    assert tree_state(layout.home) == before


def test_apply_writes_the_plist_and_issues_exactly_one_bootstrap(layout: Layout) -> None:
    runner = FakeProcessRunner()
    runner.script("launchctl", "bootstrap")
    code, output = _run(layout, runner, apply=True)
    assert code == 0, output
    assert runner.calls == [_bootstrap(layout)]
    plist = launchd_plist_path(layout)
    assert plist.read_text(encoding="utf-8") == launchd_plist_text(layout)
    assert paths.state_dir().is_dir()  # the log directory launchd writes into exists
    assert "bootstrapped io.github.erik-romson.fylgja in gui/501" in output


def test_a_changed_plist_is_backed_up_and_bootstrapped_again(layout: Layout) -> None:
    plist = launchd_plist_path(layout)
    plist.parent.mkdir(parents=True)
    plist.write_text("<old/>", encoding="utf-8")
    runner = FakeProcessRunner()
    runner.script("launchctl", "bootstrap")
    code, output = _run(layout, runner, apply=True)
    assert code == 0
    assert runner.calls == [_bootstrap(layout)]
    backups = list(paths.backups_dir().iterdir())
    assert [b.name for b in backups] == [
        "20261005T123045Z-Library_LaunchAgents_io.github.erik-romson.fylgja.plist"
    ]
    assert backups[0].read_text(encoding="utf-8") == "<old/>"
    assert "backup:" in output


def test_a_rerun_with_the_service_loaded_changes_nothing(layout: Layout) -> None:
    runner = FakeProcessRunner()
    runner.script("launchctl", "bootstrap")
    runner.script("launchctl", "print", returncode=0, stdout="loaded")
    _run(layout, runner, apply=True)
    after_first = tree_state(layout.home)
    runner.calls.clear()
    code, output = _run(layout, runner, apply=True)
    assert code == 0
    assert "already loaded" in output
    assert runner.calls == [("launchctl", "print", f"gui/{UID}/{LAUNCHD_LABEL}")]
    assert tree_state(layout.home) == after_first


def test_a_rerun_with_the_service_not_loaded_bootstraps(layout: Layout) -> None:
    runner = FakeProcessRunner()
    runner.script("launchctl", "bootstrap")
    _run(layout, runner, apply=True)
    runner.script("launchctl", "print", returncode=113, stderr="Could not find service")
    runner.calls.clear()
    code, _ = _run(layout, runner, apply=True)
    assert code == 0
    assert runner.calls == [
        ("launchctl", "print", f"gui/{UID}/{LAUNCHD_LABEL}"),
        _bootstrap(layout),
    ]


def test_a_failing_bootstrap_is_reported_with_the_way_out(layout: Layout) -> None:
    runner = FakeProcessRunner()
    runner.script("launchctl", "bootstrap", returncode=5, stderr="Bootstrap failed: 5")
    code, output = _run(layout, runner, apply=True)
    assert code == 1
    assert "exited 5" in output
    assert "Bootstrap failed: 5" in output
    assert f"launchctl bootout gui/{UID}/{LAUNCHD_LABEL}" in output


@pytest.mark.parametrize("error", [FileNotFoundError(), TimeoutError()])
def test_a_missing_or_hanging_launchctl_is_reported(layout: Layout, error: BaseException) -> None:
    runner = FakeProcessRunner()
    runner.script("launchctl", error=error)
    code, output = _run(layout, runner, apply=True)
    assert code == 1
    assert "launchctl bootstrap failed" in output


def test_a_missing_launchctl_while_checking_a_loaded_service_is_reported(layout: Layout) -> None:
    runner = FakeProcessRunner()
    runner.script("launchctl", "bootstrap")
    _run(layout, runner, apply=True)
    quiet = FakeProcessRunner()  # nothing scripted: launchctl is "missing"
    code, output = _run(layout, quiet, apply=True)
    assert code == 1
    assert "launchctl is not available" in output


def test_a_linked_plist_is_refused(layout: Layout, isolated_env: Path) -> None:
    plist = launchd_plist_path(layout)
    plist.parent.mkdir(parents=True)
    target = isolated_env / "elsewhere.plist"
    target.write_text("x", encoding="utf-8")
    plist.symlink_to(target)
    runner = FakeProcessRunner()
    code, output = _run(layout, runner, apply=True)
    assert code == 1
    assert "is a link" in output
    assert runner.calls == []
