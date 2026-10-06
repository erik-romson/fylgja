"""``fylgja install skill`` against a copy of the fixture dotfiles tree (WP4.4, A18, A22, Q10)."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotfiles_support import GIT_BIN, conf_path, hooks_runner, skill_dir, tree_state

from fylgja import paths
from fylgja.cli.install import (
    SKILL_ENTRY,
    ConfEntry,
    Layout,
    parse_dotfiles_conf,
    run_install_skill,
    skill_template,
)
from fylgja.testing.fakes import FakeProcessRunner

STAMP = datetime(2026, 10, 6, 9, 0, 0, tzinfo=UTC)
SKILL_LABEL = "claude/.claude/skills/fylgja-todo/SKILL.md"
ENTRY_TEXT = "claude:.claude/skills/fylgja-todo/"


def _run(
    layout: Layout, runner: FakeProcessRunner | None = None, *, apply: bool
) -> tuple[int, str]:
    lines: list[str] = []
    code = asyncio.run(
        run_install_skill(
            apply=apply,
            runner=hooks_runner() if runner is None else runner,
            layout=layout,
            git=GIT_BIN,
            now=lambda: STAMP,
            echo=lines.append,
        )
    )
    return code, "\n".join(lines)


def _link(layout: Layout) -> Path:
    return layout.home / ".claude" / "skills" / "fylgja-todo"


def test_the_template_follows_the_documented_mechanism() -> None:
    text = skill_template()
    front = text.split("---")[1]
    assert text.startswith("---\n")
    assert "name: fylgja-todo" in front
    assert "disable-model-invocation: true" in front
    assert "allowed-tools: Bash(~/.local/bin/fylgja todo add *)" in front
    assert "--session ${CLAUDE_SESSION_ID}" in text
    assert text.count("$ARGUMENTS") == 1
    # A18: no hook, no injection into model context
    assert "hook" not in text.lower()
    assert re.search(
        r"^~/\.local/bin/fylgja todo add --session \$\{CLAUDE_SESSION_ID\} 'TODO_TEXT'$", text, re.M
    )


def test_dry_run_prints_the_diff_and_writes_nothing(fake_home: Layout) -> None:
    before = tree_state(fake_home.home)
    runner = hooks_runner()
    code, output = _run(fake_home, runner, apply=False)
    assert code == 0
    assert f"+++ b/{SKILL_LABEL}" in output
    assert f'+    "{ENTRY_TEXT}"' in output
    assert (
        "link: ~/.claude/skills/fylgja-todo -> ~/dotfiles/claude/.claude/skills/fylgja-todo"
        in output
    )
    assert "--apply" in output
    assert tree_state(fake_home.home) == before
    assert runner.calls == []


def test_apply_installs_the_file_the_conf_line_and_the_link(fake_home: Layout) -> None:
    code, output = _run(fake_home, apply=True)
    assert code == 0, output
    assert (skill_dir(fake_home) / "SKILL.md").read_text(encoding="utf-8") == skill_template()
    conf = conf_path(fake_home).read_text(encoding="utf-8")
    assert conf.count(ENTRY_TEXT) == 1
    assert ConfEntry("claude", ".claude/skills/fylgja-todo/") in parse_dotfiles_conf(conf)
    assert SKILL_ENTRY in parse_dotfiles_conf(conf)
    # the existing entries keep their order and the new one sits last, before the closing paren
    assert conf.index('"claude:.claude/skills/example-skill/"') < conf.index(ENTRY_TEXT)
    assert conf.rstrip().endswith(f'"{ENTRY_TEXT}"\n)')
    link = _link(fake_home)
    assert link.is_symlink()
    assert os.readlink(link) == str(skill_dir(fake_home))
    assert (link / "SKILL.md").read_text(encoding="utf-8") == skill_template()
    assert "wrote ~/dotfiles/claude/.claude/skills/fylgja-todo/SKILL.md" in output
    assert "linked ~/.claude/skills/fylgja-todo" in output


def test_a_rerun_changes_nothing_and_makes_no_backup(fake_home: Layout) -> None:
    assert _run(fake_home, apply=True)[0] == 0
    backups = sorted(p.name for p in paths.backups_dir().iterdir())
    state = tree_state(fake_home.home)
    for _ in range(3):
        code, output = _run(fake_home, apply=True)
        assert code == 0
        assert "nothing to do" in output
    assert tree_state(fake_home.home) == state
    assert sorted(p.name for p in paths.backups_dir().iterdir()) == backups
    assert conf_path(fake_home).read_text(encoding="utf-8").count(ENTRY_TEXT) == 1


def test_apply_backs_up_dotfiles_conf_reports_git_and_never_commits(fake_home: Layout) -> None:
    conf_before = conf_path(fake_home).read_bytes()
    runner = hooks_runner()
    _, output = _run(fake_home, runner, apply=True)
    backups = sorted(paths.backups_dir().iterdir())
    assert [b.name for b in backups] == ["20261006T090000Z-dotfiles.conf"]
    assert backups[0].read_bytes() == conf_before
    assert runner.calls == [
        (
            GIT_BIN,
            "-C",
            str(fake_home.dotfiles),
            "diff",
            "--stat",
            "--",
            SKILL_LABEL,
            "dotfiles.conf",
        )
    ]
    assert f'-m "fylgja: install skill" -- {SKILL_LABEL} dotfiles.conf' in output
    assert "never commits" in output


def test_a_changed_skill_file_is_backed_up_and_restored(fake_home: Layout) -> None:
    _run(fake_home, apply=True)
    file = skill_dir(fake_home) / "SKILL.md"
    file.write_text("edited by hand\n", encoding="utf-8")
    code, _ = _run(fake_home, apply=True)
    assert code == 0
    assert file.read_text(encoding="utf-8") == skill_template()
    names = sorted(p.name for p in paths.backups_dir().iterdir())
    assert names == [
        "20261006T090000Z-claude_.claude_skills_fylgja-todo_SKILL.md",
        "20261006T090000Z-dotfiles.conf",
    ]
    backup = paths.backups_dir() / names[0]
    assert backup.read_text(encoding="utf-8") == "edited by hand\n"


def test_an_existing_conf_line_is_not_added_twice(fake_home: Layout) -> None:
    conf = conf_path(fake_home)
    conf.write_text(
        conf.read_text(encoding="utf-8").replace(")\n", f'    "{ENTRY_TEXT}"\n)\n'),
        encoding="utf-8",
    )
    assert _run(fake_home, apply=True)[0] == 0
    assert conf.read_text(encoding="utf-8").count(ENTRY_TEXT) == 1


def test_the_other_skills_and_files_are_untouched(fake_home: Layout) -> None:
    before = tree_state(fake_home.home)
    _run(fake_home, apply=True)
    after = tree_state(fake_home.home)
    changed = {k for k in after if before.get(k) != after[k]}
    assert changed == {
        "dotfiles/dotfiles.conf",
        "dotfiles/claude/.claude/skills/fylgja-todo",
        "dotfiles/claude/.claude/skills/fylgja-todo/SKILL.md",
        ".claude/skills/fylgja-todo",
        ".local",
        ".local/state",
        ".local/state/fylgja",
        ".local/state/fylgja/backups",
        ".local/state/fylgja/backups/20261006T090000Z-dotfiles.conf",
    }


def test_a_real_directory_instead_of_the_link_is_refused(fake_home: Layout) -> None:
    link = _link(fake_home)
    link.mkdir(parents=True)
    (link / "SKILL.md").write_text("mine\n", encoding="utf-8")
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "is a directory, but dotfiles expects a link" in output
    assert tree_state(fake_home.home) == before


def test_a_link_to_somewhere_else_is_refused(fake_home: Layout) -> None:
    link = _link(fake_home)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(fake_home.home / "elsewhere")
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "not to ~/dotfiles/claude/.claude/skills/fylgja-todo" in output
    assert tree_state(fake_home.home) == before


def test_a_linked_skills_directory_is_refused(fake_home: Layout) -> None:
    skills = fake_home.home / ".claude" / "skills"
    shutil.rmtree(skills)  # holds only the example-skill link
    skills.symlink_to(fake_home.dotfiles / "claude" / ".claude" / "skills")
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "is a link to a directory" in output
    assert tree_state(fake_home.home) == before


def test_a_linked_dotfiles_skill_directory_is_refused(fake_home: Layout) -> None:
    target = skill_dir(fake_home)
    target.symlink_to(fake_home.home)
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "is a link; it must be a real path" in output
    assert tree_state(fake_home.home) == before


def test_a_missing_dotfiles_conf_is_refused(fake_home: Layout) -> None:
    conf_path(fake_home).unlink()
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "dotfiles.conf not found" in output


def test_a_conf_without_a_closing_paren_is_refused_untouched(fake_home: Layout) -> None:
    conf_path(fake_home).write_text("DOTFILES=(\n", encoding="utf-8")
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "no closing ')'" in output
    assert tree_state(fake_home.home) == before


def test_every_installer_coexists_and_stays_idempotent(fake_home: Layout) -> None:
    """Config, hooks, statusline, keybinding, skill, launchd and secret edit different files (and
    ``dotfiles.conf`` only for the entries that need one): run together, each keeps its own content
    and a second pass changes nothing."""
    from dotfiles_support import aerospace_source, settings_source, statusline_source

    from fylgja.cli.install import (
        BLOCK_BEGIN,
        run_install_config,
        run_install_hooks,
        run_install_keybinding,
        run_install_launchd,
        run_install_secret,
        run_install_statusline,
    )

    runner = hooks_runner()
    runner.script("launchctl", "bootstrap")
    runner.script("launchctl", "print", returncode=0, stdout="loaded")
    lines: list[str] = []

    def everything() -> list[int]:
        common: dict[str, Any] = {"apply": True, "runner": runner, "layout": fake_home}
        common |= {"now": lambda: STAMP, "echo": lines.append}
        git = {"git": GIT_BIN}
        return [
            asyncio.run(run_install_config(**common, **git)),
            asyncio.run(run_install_hooks(**common, **git)),
            asyncio.run(run_install_statusline(**common, **git)),
            asyncio.run(run_install_keybinding(**common, **git)),
            asyncio.run(run_install_skill(**common, **git)),
            asyncio.run(run_install_launchd(**common, uid=501)),
            run_install_secret(layout=fake_home, echo=lines.append),
        ]

    assert everything() == [0] * 7, lines
    state = tree_state(fake_home.home)
    assert everything() == [0] * 7, lines
    assert tree_state(fake_home.home) == state
    assert statusline_source(fake_home).read_text(encoding="utf-8").count(BLOCK_BEGIN) == 1
    assert aerospace_source(fake_home).read_text(encoding="utf-8").count(BLOCK_BEGIN) == 1
    assert "hook SessionStart" in settings_source(fake_home).read_text(encoding="utf-8")
    conf = conf_path(fake_home).read_text(encoding="utf-8")
    assert conf.count(ENTRY_TEXT) == 1
    assert conf.count("fylgja:.config/fylgja/config.toml") == 1
    assert (paths.api_secret_file().stat().st_mode & 0o777) == 0o600
    assert not any("osascript" in call[0] for call in runner.calls)
