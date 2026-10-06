"""``fylgja install statusline`` against a copy of the fixture dotfiles tree (WP2.4, A4, A22)."""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dotfiles_support import GIT_BIN, hooks_runner, statusline_source, tree_state

from fylgja import paths
from fylgja.cli.install import (
    BLOCK_BEGIN,
    BLOCK_END,
    STATUSLINE_BLOCK,
    Layout,
    render_statusline,
    run_install_statusline,
)
from fylgja.testing.fakes import FakeProcessRunner

STAMP = datetime(2026, 10, 6, 9, 0, 0, tzinfo=UTC)
LABEL = "claude/.claude/statusline.sh"


def _run(
    layout: Layout, runner: FakeProcessRunner | None = None, *, apply: bool
) -> tuple[int, str]:
    lines: list[str] = []
    code = asyncio.run(
        run_install_statusline(
            apply=apply,
            runner=hooks_runner() if runner is None else runner,
            layout=layout,
            git=GIT_BIN,
            now=lambda: STAMP,
            echo=lines.append,
        )
    )
    return code, "\n".join(lines)


def _text(layout: Layout) -> str:
    return statusline_source(layout).read_text(encoding="utf-8")


def test_dry_run_prints_the_block_as_a_diff_and_writes_nothing(fake_home: Layout) -> None:
    before = tree_state(fake_home.home)
    runner = hooks_runner()
    code, output = _run(fake_home, runner, apply=False)
    assert code == 0
    assert f"--- a/{LABEL}" in output
    assert f"+{BLOCK_BEGIN}" in output
    assert f"+{BLOCK_END}" in output
    assert "--apply" in output
    assert tree_state(fake_home.home) == before
    assert runner.calls == []


def test_apply_inserts_the_block_once_after_the_input_line_and_keeps_the_mode(
    fake_home: Layout,
) -> None:
    original = _text(fake_home)
    mode = os.stat(statusline_source(fake_home)).st_mode & 0o777
    code, output = _run(fake_home, apply=True)
    assert code == 0, output
    text = _text(fake_home)
    assert text.count(BLOCK_BEGIN) == 1
    assert text.count(BLOCK_END) == 1
    assert text == original.replace("input=$(cat)\n", "input=$(cat)\n" + STATUSLINE_BLOCK)
    assert os.stat(statusline_source(fake_home)).st_mode & 0o777 == mode
    assert (fake_home.home / ".claude" / "statusline.sh").is_symlink()
    assert "wrote ~/dotfiles/claude/.claude/statusline.sh" in output


def test_the_block_is_inserted_exactly_once_across_reruns(fake_home: Layout) -> None:
    assert _run(fake_home, apply=True)[0] == 0
    state = tree_state(fake_home.home)
    for _ in range(3):
        code, output = _run(fake_home, apply=True)
        assert code == 0
        assert "nothing to do" in output
    assert tree_state(fake_home.home) == state
    assert _text(fake_home).count(BLOCK_BEGIN) == 1


def test_apply_makes_one_backup_of_the_original_and_reports_git(fake_home: Layout) -> None:
    original = statusline_source(fake_home).read_bytes()
    runner = hooks_runner()
    _, output = _run(fake_home, runner, apply=True)
    backups = sorted(paths.backups_dir().iterdir())
    assert [b.name for b in backups] == ["20261006T090000Z-claude_.claude_statusline.sh"]
    assert backups[0].read_bytes() == original
    assert runner.calls == [(GIT_BIN, "-C", str(fake_home.dotfiles), "diff", "--stat", "--", LABEL)]
    assert f'git -C ~/dotfiles commit -m "fylgja: install statusline" -- {LABEL}' in output
    assert "never commits" in output
    _run(fake_home, apply=True)
    assert len(list(paths.backups_dir().iterdir())) == 1


def test_a_stale_block_is_replaced_in_place_not_duplicated(fake_home: Layout) -> None:
    _run(fake_home, apply=True)
    stale = _text(fake_home).replace("hook StatusLine", "hook StatusLine --old")
    statusline_source(fake_home).write_text(stale, encoding="utf-8")
    assert _run(fake_home, apply=True)[0] == 0
    text = _text(fake_home)
    assert text.count(BLOCK_BEGIN) == 1
    assert "--old" not in text
    assert text.endswith("printf 'stub statusline\\n'\n")


def test_the_edit_leaves_the_rest_of_the_script_byte_for_byte(fake_home: Layout) -> None:
    original = _text(fake_home)
    _run(fake_home, apply=True)
    text = _text(fake_home)
    start, end = text.index(BLOCK_BEGIN), text.index(BLOCK_END) + len(BLOCK_END) + 1
    assert text[:start] + text[end:] == original


def test_a_regular_file_instead_of_the_link_is_refused(fake_home: Layout) -> None:
    link = fake_home.home / ".claude" / "statusline.sh"
    content = statusline_source(fake_home).read_bytes()
    link.unlink()
    link.write_bytes(content)
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "is a regular file, but dotfiles expects a link" in output
    assert tree_state(fake_home.home) == before


def test_a_script_without_the_input_line_is_refused(fake_home: Layout) -> None:
    statusline_source(fake_home).write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "no `input=$(cat)` line" in output
    assert "Add the block by hand" in output
    assert tree_state(fake_home.home) == before


@pytest.mark.parametrize(
    "text",
    [
        f"input=$(cat)\n{BLOCK_BEGIN}\n",
        f"input=$(cat)\n{BLOCK_END}\n",
        f"input=$(cat)\n{BLOCK_BEGIN}\n{BLOCK_BEGIN}\n{BLOCK_END}\n{BLOCK_END}\n",
        f"input=$(cat)\n{BLOCK_END}\n{BLOCK_BEGIN}\n",
        f"input=$(cat)\nx {BLOCK_BEGIN}\n{BLOCK_END}\n",
    ],
)
def test_broken_markers_are_refused_untouched(fake_home: Layout, text: str) -> None:
    statusline_source(fake_home).write_text(text, encoding="utf-8")
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "Nothing was changed" in output
    assert tree_state(fake_home.home) == before


def test_render_statusline_handles_a_block_at_the_start_and_a_last_line_without_newline() -> None:
    assert render_statusline("input=$(cat)").startswith("input=$(cat)\n" + BLOCK_BEGIN)
    only_block = BLOCK_BEGIN + "\nold\n" + BLOCK_END
    assert render_statusline(only_block) == STATUSLINE_BLOCK
    assert render_statusline(render_statusline("input=$(cat)\nrest\n")) == (
        "input=$(cat)\n" + STATUSLINE_BLOCK + "rest\n"
    )


def test_a_missing_dotfiles_script_is_refused_as_dangling(fake_home: Layout) -> None:
    statusline_source(fake_home).unlink()
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "dangling" in output


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a POSIX sh")
def test_the_installed_script_stays_valid_and_hands_the_json_to_fylgja(
    fake_home: Layout, tmp_path: Path
) -> None:
    """Run the edited script under bash with a stub ``fylgja``: it gets ``hook StatusLine`` and
    the JSON on stdin, the script's own output is unchanged and nothing leaks to stderr."""
    _run(fake_home, apply=True)
    bin_dir = fake_home.home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    log = tmp_path / "fylgja.log"
    stub = bin_dir / "fylgja"
    stub.write_text(f'#!/bin/sh\necho "$@" > {log}\ncat >> {log}\n', encoding="utf-8")
    stub.chmod(0o755)
    script = fake_home.home / ".claude" / "statusline.sh"
    result = subprocess.run(
        ["/bin/bash", str(script)],
        input='{"session_id":"s1"}',
        capture_output=True,
        text=True,
        env={"HOME": str(fake_home.home), "PATH": "/usr/bin:/bin"},
        timeout=20,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "stub statusline\n"
    assert result.stderr == ""
    for _ in range(100):  # the hook runs in the background
        if log.exists() and '{"session_id":"s1"}' in log.read_text(encoding="utf-8"):
            break
        subprocess.run(["/bin/sleep", "0.05"], check=True)
    assert log.read_text(encoding="utf-8").splitlines() == [
        "hook StatusLine",
        '{"session_id":"s1"}',
    ]


def test_the_script_without_fylgja_installed_is_silent(fake_home: Layout) -> None:
    _run(fake_home, apply=True)
    script = fake_home.home / ".claude" / "statusline.sh"
    result = subprocess.run(
        ["/bin/bash", str(script)],
        input="{}",
        capture_output=True,
        text=True,
        env={"HOME": str(fake_home.home), "PATH": "/usr/bin:/bin"},
        timeout=20,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "stub statusline\n"
    assert result.stderr == ""
