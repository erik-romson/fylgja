"""A fake ``$HOME`` of symlinks into a copy of ``tests/fixtures/dotfiles/`` (A22).

``build_home`` copies the fixture tree to ``<home>/dotfiles`` and links every ``dotfiles.conf``
entry into ``<home>`` the way Erik's install script does, so installers and doctor run against the
same layout as the real machine and never touch the real ``~/dotfiles``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from fylgja.cli.install import DOTFILES_CONF, Layout, parse_dotfiles_conf

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dotfiles"


def build_home(home: Path) -> Layout:
    """Copy the fixture into ``home/dotfiles`` and link each listed entry into ``home``."""
    dotfiles = home / "dotfiles"
    shutil.copytree(FIXTURE, dotfiles, symlinks=True)
    conf = (dotfiles / DOTFILES_CONF).read_text(encoding="utf-8")
    for entry in parse_dotfiles_conf(conf):
        source = dotfiles / entry.package / entry.path.rstrip("/")
        link = home / entry.path.rstrip("/")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(source)
    return Layout(home=home, dotfiles=dotfiles)


def tree_state(root: Path) -> dict[str, str]:
    """A comparable snapshot of every file, link and directory under ``root``."""
    state: dict[str, str] = {}
    for current, directories, files in os.walk(root):
        for name in sorted([*directories, *files]):
            path = Path(current) / name
            key = path.relative_to(root).as_posix()
            if path.is_symlink():
                state[key] = f"link:{os.readlink(path)}"
            elif path.is_dir():
                state[key] = "dir"
            else:
                state[key] = f"file:{path.read_bytes().hex()}"
    return state


# --------------------------------------------------------------------------------------------
# Hook and statusline installers (WP2.4)
# --------------------------------------------------------------------------------------------

import json  # noqa: E402
from typing import Any  # noqa: E402

from fylgja.testing.fakes import FakeProcessRunner  # noqa: E402

GIT_BIN = "/usr/bin/git"
CLAUDE_VERSION_OUTPUT = "2.1.289 (Claude Code)\n"


def settings_source(layout: Layout) -> Path:
    """``settings.json`` in the dotfiles tree (``~/.claude/settings.json`` links to it)."""
    return layout.dotfiles / "claude" / ".claude" / "settings.json"


def statusline_source(layout: Layout) -> Path:
    return layout.dotfiles / "claude" / ".claude" / "statusline.sh"


def load_settings(layout: Layout) -> dict[str, Any]:
    data = json.loads(settings_source(layout).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def write_settings(layout: Layout, settings: dict[str, Any]) -> None:
    """Write ``settings`` the way Claude Code does: two-space JSON and a final newline."""
    text = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    settings_source(layout).write_text(text, encoding="utf-8")


def without_lidskjalv(settings: dict[str, Any]) -> dict[str, Any]:
    """``settings`` with every Lidskjalv hook removed: the independence fixture."""
    kept = {k: v for k, v in settings.items() if k != "hooks"}
    kept["hooks"] = {
        "PostToolUse": [
            group
            for group in settings["hooks"]["PostToolUse"]
            if group["matcher"] == "(Write|Edit|MultiEdit)"
        ]
    }
    return kept


def hooks_runner(version_output: str = CLAUDE_VERSION_OUTPUT) -> FakeProcessRunner:
    """A runner that answers ``claude --version`` and ``git diff --stat``."""
    runner = FakeProcessRunner()
    runner.script("claude", "--version", stdout=version_output)
    runner.script(GIT_BIN, "-C", stdout=" claude/.claude/settings.json | 40 +++\n")
    return runner
