"""``fylgja install keybinding`` against a copy of the fixture dotfiles tree (WP4.4, A22, Q10)."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from dotfiles_support import GIT_BIN, aerospace_source, hooks_runner, tree_state

from fylgja import paths
from fylgja.cli.install import (
    BLOCK_BEGIN,
    BLOCK_END,
    KEYBINDING_KEY,
    Layout,
    keybinding_block,
    keybinding_command,
    render_keybinding,
    run_install_keybinding,
)
from fylgja.testing.fakes import FakeProcessRunner

STAMP = datetime(2026, 10, 6, 9, 0, 0, tzinfo=UTC)
LABEL = "aerospace/.aerospace.toml"


def _run(
    layout: Layout,
    runner: FakeProcessRunner | None = None,
    *,
    apply: bool,
    key: str = KEYBINDING_KEY,
) -> tuple[int, str]:
    lines: list[str] = []
    code = asyncio.run(
        run_install_keybinding(
            apply=apply,
            runner=hooks_runner() if runner is None else runner,
            layout=layout,
            key=key,
            git=GIT_BIN,
            now=lambda: STAMP,
            echo=lines.append,
        )
    )
    return code, "\n".join(lines)


def _text(layout: Layout) -> str:
    return aerospace_source(layout).read_text(encoding="utf-8")


def _binding(text: str, key: str = KEYBINDING_KEY) -> str:
    data: dict[str, Any] = tomllib.loads(text)
    value = data["mode"]["main"]["binding"][key]
    assert isinstance(value, str)
    return value


def test_dry_run_prints_the_binding_and_writes_nothing(fake_home: Layout) -> None:
    before = tree_state(fake_home.home)
    runner = hooks_runner()
    code, output = _run(fake_home, runner, apply=False)
    assert code == 0
    assert f"--- a/{LABEL}" in output
    assert f"+{BLOCK_BEGIN}" in output
    assert f"+{BLOCK_END}" in output
    assert f"+{KEYBINDING_KEY} = '''exec-and-forget " in output
    assert "osascript -e 'text returned of (display dialog" in output
    assert '$HOME/.local/bin/fylgja" todo add -- "$answer"' in output
    assert '[ -x "$HOME/.local/bin/fylgja" ]' in output
    assert "--apply" in output
    assert "aerospace reload-config" in output
    assert tree_state(fake_home.home) == before
    assert runner.calls == []


def test_apply_inserts_one_block_at_the_end_of_the_binding_table(fake_home: Layout) -> None:
    original = _text(fake_home)
    mode = os.stat(aerospace_source(fake_home)).st_mode & 0o777
    code, output = _run(fake_home, apply=True)
    assert code == 0, output
    text = _text(fake_home)
    assert text.count(BLOCK_BEGIN) == 1
    assert text.count(BLOCK_END) == 1
    assert text == original + "\n" + keybinding_block()
    assert _binding(text) == f"exec-and-forget {keybinding_command()}"
    data = tomllib.loads(text)
    assert data["mode"]["main"]["binding"]["alt-h"] == "focus left"
    assert os.stat(aerospace_source(fake_home)).st_mode & 0o777 == mode
    assert "wrote ~/dotfiles/aerospace/.aerospace.toml" in output


def test_the_block_goes_before_the_next_table(fake_home: Layout) -> None:
    after = "\n[[on-window-detected]]\nif.app-id = 'x'\nrun = 'layout floating'\n"
    original = _text(fake_home).rstrip("\n") + "\n\n\n" + after.lstrip("\n")
    aerospace_source(fake_home).write_text(original, encoding="utf-8")
    assert _run(fake_home, apply=True)[0] == 0
    text = _text(fake_home)
    assert text.index(BLOCK_END) < text.index("[[on-window-detected]]")
    data = tomllib.loads(text)
    assert KEYBINDING_KEY in data["mode"]["main"]["binding"]
    assert data["on-window-detected"][0]["run"] == "layout floating"
    assert text.replace(keybinding_block(), "").count("[[on-window-detected]]") == 1


def test_the_block_is_inserted_exactly_once_across_reruns(fake_home: Layout) -> None:
    assert _run(fake_home, apply=True)[0] == 0
    state = tree_state(fake_home.home)
    for _ in range(3):
        code, output = _run(fake_home, apply=True)
        assert code == 0
        assert "nothing to do" in output
    assert tree_state(fake_home.home) == state
    assert _text(fake_home).count(BLOCK_BEGIN) == 1


def test_apply_makes_one_backup_reports_git_and_never_commits(fake_home: Layout) -> None:
    original = aerospace_source(fake_home).read_bytes()
    runner = hooks_runner()
    _, output = _run(fake_home, runner, apply=True)
    backups = sorted(paths.backups_dir().iterdir())
    assert [b.name for b in backups] == ["20261006T090000Z-aerospace_.aerospace.toml"]
    assert backups[0].read_bytes() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    # the only git call is the read-only diff; nothing stages or commits
    assert runner.calls == [(GIT_BIN, "-C", str(fake_home.dotfiles), "diff", "--stat", "--", LABEL)]
    assert f'git -C ~/dotfiles commit -m "fylgja: install keybinding" -- {LABEL}' in output
    assert "never commits" in output
    _run(fake_home, apply=True)
    assert len(list(paths.backups_dir().iterdir())) == 1


def test_the_edit_leaves_the_rest_of_the_file_byte_for_byte(fake_home: Layout) -> None:
    original = _text(fake_home)
    _run(fake_home, apply=True)
    text = _text(fake_home)
    start, end = text.index(BLOCK_BEGIN), text.index(BLOCK_END) + len(BLOCK_END) + 1
    assert (text[:start] + text[end:]).rstrip("\n") == original.rstrip("\n")


def test_a_stale_block_is_replaced_in_place(fake_home: Layout) -> None:
    _run(fake_home, apply=True)
    stale = _text(fake_home).replace("Fylgja todo", "Old title")
    aerospace_source(fake_home).write_text(stale, encoding="utf-8")
    assert _run(fake_home, apply=True)[0] == 0
    text = _text(fake_home)
    assert text.count(BLOCK_BEGIN) == 1
    assert "Old title" not in text


def test_a_regular_file_instead_of_the_link_is_refused(fake_home: Layout) -> None:
    link = fake_home.home / ".aerospace.toml"
    content = aerospace_source(fake_home).read_bytes()
    link.unlink()
    link.write_bytes(content)
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "is a regular file, but dotfiles expects a link" in output
    assert tree_state(fake_home.home) == before


def test_a_missing_link_or_dangling_link_is_refused(fake_home: Layout) -> None:
    link = fake_home.home / ".aerospace.toml"
    link.unlink()
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "does not exist" in output
    link.symlink_to(fake_home.dotfiles / "aerospace" / "missing.toml")
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "dangling" in output


def test_a_key_that_is_already_bound_is_refused_and_another_key_works(fake_home: Layout) -> None:
    aerospace_source(fake_home).write_text(
        "[mode.main.binding]\nalt-shift-t = 'layout tiles'\n", encoding="utf-8"
    )
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "alt-shift-t is already bound" in output
    assert "--key" in output
    assert tree_state(fake_home.home) == before
    code, output = _run(fake_home, apply=True, key="alt-shift-y")
    assert code == 0, output
    binding = tomllib.loads(_text(fake_home))["mode"]["main"]["binding"]
    assert binding["alt-shift-t"] == "layout tiles"
    assert binding["alt-shift-y"].startswith("exec-and-forget ")


def test_a_file_without_the_binding_table_is_refused_with_the_block_to_add(
    fake_home: Layout,
) -> None:
    aerospace_source(fake_home).write_text("start-at-login = true\n", encoding="utf-8")
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "no [mode.main.binding] table" in output
    assert BLOCK_BEGIN in output
    assert tree_state(fake_home.home) == before


def test_invalid_toml_is_refused_untouched(fake_home: Layout) -> None:
    aerospace_source(fake_home).write_text("[mode.main.binding\nbroken\n", encoding="utf-8")
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "not valid TOML" in output
    assert tree_state(fake_home.home) == before


@pytest.mark.parametrize("key", ["", "Alt-T", "alt shift t", "alt--t", "a'b"])
def test_an_invalid_key_is_refused(fake_home: Layout, key: str) -> None:
    code, output = _run(fake_home, apply=True, key=key)
    assert code == 1
    assert "is not an AeroSpace key" in output


@pytest.mark.parametrize(
    "text",
    [
        f"[mode.main.binding]\n{BLOCK_BEGIN}\n",
        f"[mode.main.binding]\n{BLOCK_END}\n",
        f"[mode.main.binding]\n{BLOCK_END}\n{BLOCK_BEGIN}\n",
        f"[mode.main.binding]\nx {BLOCK_BEGIN}\n{BLOCK_END}\n",
    ],
)
def test_broken_markers_are_refused_untouched(fake_home: Layout, text: str) -> None:
    aerospace_source(fake_home).write_text(text, encoding="utf-8")
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "Nothing was changed" in output
    assert tree_state(fake_home.home) == before


def test_render_keybinding_handles_a_table_on_the_last_line_without_newline() -> None:
    text = render_keybinding("[mode.main.binding]")
    assert text == "[mode.main.binding]\n" + keybinding_block()
    assert render_keybinding(text) == text
    assert render_keybinding("[mode.main.binding]\nalt-h = 'x'") == (
        "[mode.main.binding]\nalt-h = 'x'\n\n" + keybinding_block()
    )


# --- the command itself, run under sh with stubs ----------------------------------------------


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _run_binding(
    home: Path,
    tmp: Path,
    *,
    answer: str | None,
    fylgja_exit: int = 0,
    with_fylgja: bool = True,
) -> list[str]:
    """Run the binding's command with a stub ``osascript`` and a stub ``fylgja``; returns the
    log lines: ``fylgja:`` for each argument it got, ``notify`` for a notification."""
    tmp.mkdir(parents=True, exist_ok=True)
    log = tmp / "calls.log"
    answer_file = tmp / "answer"
    if answer is not None:
        answer_file.write_text(answer, encoding="utf-8")
    bin_dir = tmp / "bin"
    _stub(
        bin_dir / "osascript",
        f'case "$2" in\n*notification*) echo notify >> "{log}";;\n'
        f'*) [ -f "{answer_file}" ] && cat "{answer_file}" || exit 1;;\nesac\n',
    )
    command = _binding(
        render_keybinding("[mode.main.binding]\n"),
    ).removeprefix("exec-and-forget ")
    if with_fylgja:
        _stub(
            home / ".local" / "bin" / "fylgja",
            f'for a in "$@"; do echo "fylgja:$a" >> "{log}"; done\nexit {fylgja_exit}\n',
        )
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"},
        timeout=20,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_the_command_passes_the_answer_as_one_argument(fake_home: Layout, tmp_path: Path) -> None:
    answer = 'buy milk\'s "cream" $HOME `id` ; rm -rf x'
    calls = _run_binding(fake_home.home, tmp_path, answer=answer)
    assert calls == ["fylgja:todo", "fylgja:add", "fylgja:--", f"fylgja:{answer}"]


def test_a_cancelled_dialog_or_an_empty_answer_adds_nothing(
    fake_home: Layout, tmp_path: Path
) -> None:
    assert _run_binding(fake_home.home, tmp_path / "a", answer=None) == []
    assert _run_binding(fake_home.home, tmp_path / "b", answer="") == []


def test_a_failed_add_posts_a_notification(fake_home: Layout, tmp_path: Path) -> None:
    calls = _run_binding(fake_home.home, tmp_path, answer="x", fylgja_exit=3)
    assert calls[-1] == "notify"
    assert "fylgja:x" in calls


def test_without_fylgja_the_binding_does_nothing_and_never_asks(
    fake_home: Layout, tmp_path: Path
) -> None:
    assert _run_binding(fake_home.home, tmp_path, answer="x", with_fylgja=False) == []


def test_the_installer_never_runs_osascript(fake_home: Layout) -> None:
    runner = hooks_runner()
    _run(fake_home, runner, apply=True)
    assert runner.calls, "expected the git diff call"
    assert [call[0] for call in runner.calls] == [GIT_BIN]


def test_the_cli_commands_are_registered() -> None:
    from typer.testing import CliRunner

    from fylgja.cli.app import app

    result = CliRunner().invoke(app, ["install", "--help"])
    assert result.exit_code == 0
    assert "keybinding" in result.output
    assert "skill" in result.output
    for command in ("keybinding", "skill"):
        assert CliRunner().invoke(app, ["install", command, "--help"]).exit_code == 0
