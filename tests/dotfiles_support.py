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
