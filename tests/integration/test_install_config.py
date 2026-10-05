"""``fylgja install config`` against a copy of the fixture dotfiles tree (WP1.10, A22, Q10)."""

from __future__ import annotations

import asyncio
import os
import stat
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dotfiles_support import tree_state

from fylgja import paths
from fylgja.cli import install
from fylgja.cli.install import (
    CONFIG_ENTRY,
    DOTFILES_CONF,
    ConfEntry,
    InstallError,
    Layout,
    add_conf_entry,
    config_template,
    parse_dotfiles_conf,
    run_install_config,
)
from fylgja.config import load_config, load_defaults, parse_config
from fylgja.testing.fakes import FakeProcessRunner

STAMP = datetime(2026, 10, 5, 12, 30, 45, tzinfo=UTC)
GIT = "/usr/bin/git"


def _run(layout: Layout, runner: FakeProcessRunner, *, apply: bool) -> tuple[int, str]:
    lines: list[str] = []
    code = asyncio.run(
        run_install_config(
            apply=apply,
            runner=runner,
            layout=layout,
            git=GIT,
            now=lambda: STAMP,
            echo=lines.append,
        )
    )
    return code, "\n".join(lines)


def _runner() -> FakeProcessRunner:
    runner = FakeProcessRunner()
    runner.script(
        GIT, "-C", stdout=" dotfiles.conf | 1 +\n fylgja/.config/fylgja/config.toml | 40 +++\n"
    )
    return runner


def _source(layout: Layout) -> Path:
    return layout.dotfiles / "fylgja" / ".config" / "fylgja" / "config.toml"


def test_dry_run_prints_a_diff_and_writes_nothing(fake_home: Layout) -> None:
    before = tree_state(fake_home.home)
    runner = _runner()
    code, output = _run(fake_home, runner, apply=False)
    assert code == 0
    assert "--- /dev/null" in output
    assert "+++ b/fylgja/.config/fylgja/config.toml" in output
    assert "--- a/dotfiles.conf" in output
    assert '+    "fylgja:.config/fylgja/config.toml"' in output
    assert "link: ~/.config/fylgja/config.toml -> ~/dotfiles/fylgja/.config/fylgja/config.toml" in (
        output
    )
    assert "--apply" in output
    assert tree_state(fake_home.home) == before
    assert runner.calls == []


def test_apply_creates_the_file_the_conf_line_and_the_file_link(fake_home: Layout) -> None:
    runner = _runner()
    code, output = _run(fake_home, runner, apply=True)
    assert code == 0, output
    source = _source(fake_home)
    assert source.read_text(encoding="utf-8") == config_template()
    entries = parse_dotfiles_conf((fake_home.dotfiles / DOTFILES_CONF).read_text(encoding="utf-8"))
    assert entries.count(CONFIG_ENTRY) == 1
    link = paths.config_file()
    assert link == fake_home.home / ".config" / "fylgja" / "config.toml"
    assert link.is_symlink()
    assert os.path.realpath(link) == os.path.realpath(source)
    assert not link.parent.is_symlink()  # a file link, never a directory link
    assert load_config() == load_config(source)  # the daemon reads it through the link
    assert "wrote ~/dotfiles/fylgja/.config/fylgja/config.toml" in output
    assert "linked ~/.config/fylgja/config.toml" in output


def test_apply_prints_the_git_stat_and_a_commit_suggestion_limited_to_the_files(
    fake_home: Layout,
) -> None:
    runner = _runner()
    _, output = _run(fake_home, runner, apply=True)
    assert runner.calls == [
        (
            GIT,
            "-C",
            str(fake_home.dotfiles),
            "diff",
            "--stat",
            "--",
            "fylgja/.config/fylgja/config.toml",
            "dotfiles.conf",
        )
    ]
    assert "fylgja/.config/fylgja/config.toml | 40" in output
    assert "git -C ~/dotfiles commit" in output
    assert (
        'git -C ~/dotfiles commit -m "fylgja: install config" -- '
        "fylgja/.config/fylgja/config.toml dotfiles.conf"
    ) in output
    assert "never commits" in output


def test_git_is_never_invoked_to_commit_or_stage(fake_home: Layout) -> None:
    runner = _runner()
    _run(fake_home, runner, apply=True)
    _run(fake_home, runner, apply=True)
    for call in runner.calls:
        assert call[0] == GIT
        assert not {"commit", "add", "reset", "checkout", "stash", "push", "restore"} & set(call)


def test_a_second_run_changes_nothing(fake_home: Layout) -> None:
    _run(fake_home, _runner(), apply=True)
    after_first = tree_state(fake_home.home)
    state = fake_home.home / ".local" / "state" / "fylgja"
    backups_before = sorted(p.name for p in state.rglob("*") if p.is_file())
    runner = _runner()
    code, output = _run(fake_home, runner, apply=True)
    assert code == 0
    assert "nothing to do" in output
    assert tree_state(fake_home.home) == after_first
    assert sorted(p.name for p in state.rglob("*") if p.is_file()) == backups_before
    assert runner.calls == []
    code, output = _run(fake_home, runner, apply=False)
    assert (code, "nothing to do" in output, "--apply" in output) == (0, True, False)


def test_a_regular_file_where_the_link_is_expected_is_refused(fake_home: Layout) -> None:
    link = paths.config_file()
    link.parent.mkdir(parents=True)
    link.write_text("# my own settings\n", encoding="utf-8")
    before = tree_state(fake_home.home)
    runner = _runner()
    for apply in (False, True):
        code, output = _run(fake_home, runner, apply=apply)
        assert code == 1
        assert "regular file" in output
        assert "ln -s" in output  # how to restore the link
        assert "mv " in output
    assert tree_state(fake_home.home) == before  # nothing was written, not even the dotfiles side
    assert link.read_text(encoding="utf-8") == "# my own settings\n"
    assert runner.calls == []


def test_a_directory_link_for_the_config_directory_is_refused(
    fake_home: Layout, isolated_env: Path
) -> None:
    elsewhere = isolated_env / "elsewhere"
    elsewhere.mkdir()
    config_dir = paths.config_dir()
    config_dir.parent.mkdir(parents=True, exist_ok=True)
    config_dir.symlink_to(elsewhere)
    code, output = _run(fake_home, _runner(), apply=True)
    assert code == 1
    assert "link to a directory" in output
    assert "API secret" in output
    assert not (elsewhere / "config.toml").exists()


def test_a_link_to_somewhere_else_is_refused(fake_home: Layout, isolated_env: Path) -> None:
    other = isolated_env / "other.toml"
    other.write_text("", encoding="utf-8")
    link = paths.config_file()
    link.parent.mkdir(parents=True)
    link.symlink_to(other)
    code, output = _run(fake_home, _runner(), apply=True)
    assert code == 1
    assert "not to ~/dotfiles/fylgja/.config/fylgja/config.toml" in output
    assert os.readlink(link) == str(other)


def test_a_backup_is_written_before_dotfiles_conf_is_edited(fake_home: Layout) -> None:
    original = (fake_home.dotfiles / DOTFILES_CONF).read_bytes()
    _, output = _run(fake_home, _runner(), apply=True)
    backups = sorted(paths.backups_dir().iterdir())
    assert [b.name for b in backups] == ["20261005T123045Z-dotfiles.conf"]
    assert backups[0].read_bytes() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert "backup: ~/.local/state/fylgja/backups/20261005T123045Z-dotfiles.conf" in output
    # Backups never go into the dotfiles tree.
    assert not list(fake_home.dotfiles.rglob("*20261005T*"))


def test_an_existing_dotfiles_config_is_kept_and_backed_up_only_when_edited(
    fake_home: Layout,
) -> None:
    source = _source(fake_home)
    source.parent.mkdir(parents=True)
    source.write_text("[api]\nport = 7400\n", encoding="utf-8")
    code, _ = _run(fake_home, _runner(), apply=True)
    assert code == 0
    assert source.read_text(encoding="utf-8") == "[api]\nport = 7400\n"
    assert load_config().api.port == 7400
    assert [b.name for b in paths.backups_dir().iterdir()] == ["20261005T123045Z-dotfiles.conf"]


def test_the_conf_entry_is_added_once_even_when_the_file_is_already_listed(
    fake_home: Layout,
) -> None:
    conf = fake_home.dotfiles / DOTFILES_CONF
    conf.write_text(
        conf.read_text(encoding="utf-8").replace(
            ")\n", '    "fylgja:.config/fylgja/config.toml"\n)\n'
        ),
        encoding="utf-8",
    )
    before = conf.read_text(encoding="utf-8")
    _run(fake_home, _runner(), apply=True)
    assert conf.read_text(encoding="utf-8") == before
    assert not paths.backups_dir().exists()


def test_a_missing_dotfiles_checkout_is_reported(isolated_env: Path) -> None:
    layout = Layout(isolated_env / "home", isolated_env / "home" / "dotfiles")
    code, output = _run(layout, _runner(), apply=True)
    assert code == 1
    assert "dotfiles.conf not found" in output


def test_the_template_is_the_defaults_commented_out() -> None:
    template = config_template()
    assert tomllib.loads(template) == {}  # nothing is active
    from importlib import resources

    defaults = resources.files("fylgja").joinpath("defaults.toml").read_text(encoding="utf-8")
    body = template.splitlines()[-len(defaults.splitlines()) :]
    restored: list[str] = []
    for original, shown in zip(defaults.splitlines(), body, strict=True):
        if original.strip() and not original.lstrip().startswith("#"):
            assert shown == f"# {original}"  # a setting is shown commented out
            restored.append(shown[2:])
        else:
            assert shown == original
            restored.append(shown)
    # Uncommenting every setting reproduces the packaged defaults exactly.
    assert tomllib.loads("\n".join(restored)) == load_defaults()
    assert parse_config(tomllib.loads("\n".join(restored)), source="template") == parse_config(
        {}, source="defaults"
    )


def test_add_conf_entry_variants() -> None:
    text = 'DOTFILES=(\n    "a:b"\n)\n'
    assert add_conf_entry(text, ConfEntry("a", "b")) == text
    added = add_conf_entry(text, ConfEntry("c", "d/e"))
    assert added == 'DOTFILES=(\n    "a:b"\n    "c:d/e"\n)\n'
    assert add_conf_entry('DOTFILES=(\n    "a:b"\n)', ConfEntry("x", "y")) == (
        'DOTFILES=(\n    "a:b"\n    "x:y"\n)'
    )
    with pytest.raises(InstallError, match="closing"):
        add_conf_entry("no array here\n", ConfEntry("c", "d"))


def test_parse_dotfiles_conf_ignores_comments_and_() -> None:
    text = '# c\nDOTFILES=(\n    "a:b"  # note\n    # "x:y"\n    c:d/\n)\n'
    assert parse_dotfiles_conf(text) == [ConfEntry("a", "b"), ConfEntry("c", "d/")]


def test_layout_shown_collapses_the_home_directory(isolated_env: Path) -> None:
    layout = Layout(isolated_env / "home", isolated_env / "home" / "dotfiles")
    assert layout.shown(layout.home / "x" / "y") == "~/x/y"
    assert layout.shown(Path("/elsewhere/z")) == "/elsewhere/z"


def test_a_failing_git_is_reported_and_does_not_fail_the_install(fake_home: Layout) -> None:
    runner = FakeProcessRunner()  # no script for git: behaves like a missing command
    code, output = _run(fake_home, runner, apply=True)
    assert code == 0
    assert "git not available" in output
    assert install.CONFIG_ENTRY in parse_dotfiles_conf(
        (fake_home.dotfiles / DOTFILES_CONF).read_text(encoding="utf-8")
    )
