"""``fylgja install hooks`` against a copy of the fixture dotfiles tree (WP2.4, A2, A21, A22, Q10).

This is the ``PLAN.md`` §4.9 configuration test: today's ``settings.json`` shape (with the two
``SessionEnd`` matchers), every Fylgja hook async, Lidskjalv's entries byte for byte unchanged,
and the same results when there are no Lidskjalv entries at all.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import textwrap
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from dotfiles_support import (
    GIT_BIN,
    hooks_runner,
    load_settings,
    settings_source,
    tree_state,
    without_lidskjalv,
    write_settings,
)

from fylgja import paths
from fylgja.cli.install import (
    CLAUDE_EVENT_MIN_VERSION,
    Layout,
    fylgja_hook_command,
    fylgja_hook_event,
    iter_hooks,
    merge_hooks,
    parse_claude_version,
    run_install_hooks,
    supported_events,
)
from fylgja.config import load_defaults
from fylgja.testing.fakes import FakeProcessRunner

STAMP = datetime(2026, 10, 6, 9, 0, 0, tzinfo=UTC)
LABEL = "claude/.claude/settings.json"


def _default_events() -> list[str]:
    return list(load_defaults()["hooks"]["events"])


def _run(
    layout: Layout,
    runner: FakeProcessRunner | None = None,
    *,
    apply: bool,
    events: Sequence[str] | None = None,
) -> tuple[int, str]:
    lines: list[str] = []
    code = asyncio.run(
        run_install_hooks(
            apply=apply,
            runner=hooks_runner() if runner is None else runner,
            layout=layout,
            events=events,
            git=GIT_BIN,
            now=lambda: STAMP,
            echo=lines.append,
        )
    )
    return code, "\n".join(lines)


def _fylgja(settings: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(e, h) for e, h in iter_hooks(settings) if fylgja_hook_event(h.get("command"))]


def _lidskjalv_groups(settings: dict[str, Any]) -> dict[str, list[Any]]:
    """Every group that holds a Lidskjalv command, per event, as it is in the file."""
    found: dict[str, list[Any]] = {}
    for event, groups in settings["hooks"].items():
        kept = [g for g in groups if "lidskjalv.sh" in json.dumps(g)]
        if kept:
            found[event] = kept
    return found


# --------------------------------------------------------------------------------------------
# The entry format
# --------------------------------------------------------------------------------------------


def test_the_command_is_guarded_and_names_the_executable_under_home() -> None:
    command = fylgja_hook_command("Stop")
    assert command == (
        '[ -x "$HOME/.local/bin/fylgja" ] && "$HOME/.local/bin/fylgja" hook Stop; exit 0'
    )
    assert fylgja_hook_event(command) == "Stop"
    assert fylgja_hook_event("~/.claude/lidskjalv.sh Stop") is None
    assert fylgja_hook_event(None) is None
    assert fylgja_hook_event("fylgja hook") is None  # no event name, not a registration


def test_claude_version_parsing_and_the_support_table() -> None:
    assert parse_claude_version("2.1.289 (Claude Code)") == (2, 1, 289)
    assert parse_claude_version("no version here") is None
    events = _default_events()
    assert set(events) <= set(CLAUDE_EVENT_MIN_VERSION)  # every A2 event has a floor
    supported, skipped = supported_events(events, (2, 1, 289))
    assert supported == events
    assert skipped == []
    supported, skipped = supported_events([*events, "NoSuchEvent"], (2, 1, 250))
    assert skipped == ["PostModelSwitch", "NoSuchEvent"]
    assert "PostModelSwitch" not in supported
    assert supported_events(events, (1, 9, 0)) == ([], events)


# --------------------------------------------------------------------------------------------
# Dry run, apply, idempotence
# --------------------------------------------------------------------------------------------


def test_dry_run_prints_a_diff_and_writes_nothing(fake_home: Layout) -> None:
    before = tree_state(fake_home.home)
    runner = hooks_runner()
    code, output = _run(fake_home, runner, apply=False)
    assert code == 0, output
    assert f"--- a/{LABEL}" in output
    assert f"+++ b/{LABEL}" in output
    assert "fylgja" in output
    assert "--apply" in output
    assert tree_state(fake_home.home) == before
    assert runner.calls == [("claude", "--version")]


def test_every_fylgja_hook_is_async_and_lidskjalvs_entries_are_unchanged(
    fake_home: Layout,
) -> None:
    before = load_settings(fake_home)
    source_before = settings_source(fake_home).read_text(encoding="utf-8")
    code, output = _run(fake_home, apply=True)
    assert code == 0, output
    after = load_settings(fake_home)

    registered = _fylgja(after)
    assert {event for event, _ in registered} == set(_default_events())
    assert len(registered) == len(_default_events())
    for _, hook in registered:
        assert hook["type"] == "command"
        assert hook["async"] is True
        assert hook["command"].startswith('[ -x "$HOME/.local/bin/fylgja" ] && ')
    for event in ("Notification", "SessionEnd"):
        ours = [g for g in after["hooks"][event] if fylgja_hook_event(g["hooks"][0]["command"])]
        assert [g["matcher"] for g in ours] == [""]

    # Lidskjalv's groups: equal as data and as the exact text the file holds.
    assert _lidskjalv_groups(after) == _lidskjalv_groups(before)
    text = settings_source(fake_home).read_text(encoding="utf-8")
    for groups in _lidskjalv_groups(before).values():
        for group in groups:
            exact = textwrap.indent(json.dumps(group, indent=2), " " * 6)
            assert exact in source_before
            assert exact in text
    # Every original entry of every event is still there, first and in order.
    for event, groups in before["hooks"].items():
        assert after["hooks"][event][: len(groups)] == groups
    assert {k: v for k, v in after.items() if k != "hooks"} == {
        k: v for k, v in before.items() if k != "hooks"
    }
    assert text != source_before


def test_session_end_keeps_its_two_lidskjalv_matchers_and_gains_one_fylgja_group(
    fake_home: Layout,
) -> None:
    _run(fake_home, apply=True)
    groups = load_settings(fake_home)["hooks"]["SessionEnd"]
    assert [g["matcher"] for g in groups] == ["clear", "resume|logout|prompt_input_exit|other", ""]
    assert fylgja_hook_event(groups[2]["hooks"][0]["command"]) == "SessionEnd"


def test_the_wp24_done_condition_only_fylgja_additions_and_a_second_run_changes_nothing(
    fake_home: Layout,
) -> None:
    code, dry = _run(fake_home, apply=False)
    assert code == 0
    body = [
        line
        for line in dry.splitlines()
        if not line.startswith(("---", "+++", "@@", "(dry run")) and line[:1] in "+-"
    ]
    assert body
    assert all(line.startswith("+") for line in body)
    added = "\n".join(line[1:] for line in body)
    assert "lidskjalv" not in added
    assert "fylgja" in added

    assert _run(fake_home, apply=True)[0] == 0
    state = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 0
    assert "nothing to do" in output
    assert tree_state(fake_home.home) == state
    assert _run(fake_home, apply=False)[1].startswith("install hooks: nothing to do")


def test_apply_backs_up_first_and_reports_git_without_committing(fake_home: Layout) -> None:
    original = settings_source(fake_home).read_bytes()
    runner = hooks_runner()
    code, output = _run(fake_home, runner, apply=True)
    assert code == 0
    backups = sorted(paths.backups_dir().iterdir())
    assert [b.name for b in backups] == ["20261006T090000Z-claude_.claude_settings.json"]
    assert backups[0].read_bytes() == original
    assert runner.calls == [
        ("claude", "--version"),
        (GIT_BIN, "-C", str(fake_home.dotfiles), "diff", "--stat", "--", LABEL),
    ]
    assert "wrote ~/dotfiles/claude/.claude/settings.json" in output
    assert f'git -C ~/dotfiles commit -m "fylgja: install hooks" -- {LABEL}' in output
    assert "never commits" in output
    assert not any(call[1:2] == ("commit",) for call in runner.calls)
    assert (fake_home.home / ".claude" / "settings.json").is_symlink()


def test_a_second_apply_makes_no_second_backup(fake_home: Layout) -> None:
    _run(fake_home, apply=True)
    _run(fake_home, apply=True)
    assert len(list(paths.backups_dir().iterdir())) == 1


def test_a_hook_that_lost_async_is_repaired_and_other_keys_are_kept(fake_home: Layout) -> None:
    _run(fake_home, apply=True)
    settings = load_settings(fake_home)
    stop = next(h for e, h in _fylgja(settings) if e == "Stop")
    stop["async"] = False
    stop["timeout"] = 5
    write_settings(fake_home, settings)
    assert _run(fake_home, apply=True)[0] == 0
    stop = next(h for e, h in _fylgja(load_settings(fake_home)) if e == "Stop")
    assert stop["async"] is True
    assert stop["timeout"] == 5
    assert len(_fylgja(load_settings(fake_home))) == len(_default_events())


def test_an_event_outside_the_configuration_is_registered_when_asked(fake_home: Layout) -> None:
    assert _run(fake_home, apply=True, events=["Stop"])[0] == 0
    assert [e for e, _ in _fylgja(load_settings(fake_home))] == ["Stop"]
    assert _run(fake_home, apply=True, events=[])[0] == 0  # no events: nothing more to do


def test_the_configured_event_set_comes_from_config_toml(fake_home: Layout) -> None:
    (paths.config_dir()).mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text('[hooks]\nevents = ["Stop", "SessionEnd"]\n', encoding="utf-8")
    assert _run(fake_home, apply=True)[0] == 0
    assert sorted(e for e, _ in _fylgja(load_settings(fake_home))) == ["SessionEnd", "Stop"]


def test_a_broken_config_is_reported(fake_home: Layout) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("[hooks\n", encoding="utf-8")
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert output.startswith("fylgja: install hooks:")


# --------------------------------------------------------------------------------------------
# The installed Claude version
# --------------------------------------------------------------------------------------------


def test_only_events_the_faked_claude_version_supports_are_registered(fake_home: Layout) -> None:
    runner = hooks_runner("2.1.250 (Claude Code)\n")
    code, output = _run(fake_home, runner, apply=True)
    assert code == 0
    registered = {event for event, _ in _fylgja(load_settings(fake_home))}
    assert "PostModelSwitch" not in registered
    assert registered == set(_default_events()) - {"PostModelSwitch"}
    assert "not registered (1): PostModelSwitch" in output
    assert "2.1.250" in output


def test_an_old_enough_version_registers_nothing_and_adds_no_hooks_key(fake_home: Layout) -> None:
    settings = without_lidskjalv(load_settings(fake_home))
    del settings["hooks"]
    write_settings(fake_home, settings)
    code, output = _run(fake_home, hooks_runner("1.0.0 (Claude Code)\n"), apply=True)
    assert code == 0
    assert "hooks" not in load_settings(fake_home)
    assert "nothing to do" in output or "not registered" in output


@pytest.mark.parametrize(
    "script",
    [
        {"error": FileNotFoundError("claude")},
        {"timeout": True},
        {"stdout": "garbled\n"},
        {"stdout": "2.1.289\n", "returncode": 1},
    ],
)
def test_an_unknown_claude_version_refuses_and_changes_nothing(
    fake_home: Layout, script: dict[str, Any]
) -> None:
    runner = FakeProcessRunner()
    runner.script("claude", "--version", **script)
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, runner, apply=True)
    assert code == 1
    assert output.startswith("fylgja: install hooks:")
    assert "Claude Code supports are unknown" in output
    assert tree_state(fake_home.home) == before


def test_claude_missing_from_the_runner_is_refused(fake_home: Layout) -> None:
    code, output = _run(fake_home, FakeProcessRunner(), apply=False)
    assert code == 1
    assert "cannot run `claude --version`" in output


# --------------------------------------------------------------------------------------------
# Independence (A21)
# --------------------------------------------------------------------------------------------


def test_the_same_holds_with_no_lidskjalv_entries_at_all(fake_home: Layout) -> None:
    bare = without_lidskjalv(load_settings(fake_home))
    write_settings(fake_home, bare)
    assert "lidskjalv" not in settings_source(fake_home).read_text(encoding="utf-8")
    code, output = _run(fake_home, apply=True)
    assert code == 0, output
    after = load_settings(fake_home)
    registered = _fylgja(after)
    assert len(registered) == len(_default_events())
    assert all(hook["async"] is True for _, hook in registered)
    assert _lidskjalv_groups(after) == {}
    for event, groups in bare["hooks"].items():
        assert after["hooks"][event][: len(groups)] == groups
    state = tree_state(fake_home.home)
    assert _run(fake_home, apply=True)[0] == 0
    assert tree_state(fake_home.home) == state


def test_a_group_shared_with_another_tool_keeps_its_other_hooks(fake_home: Layout) -> None:
    settings = load_settings(fake_home)
    stop = settings["hooks"]["Stop"][0]["hooks"]
    stop.append(
        {"type": "command", "command": fylgja_hook_command("Stop")}
    )  # sync, in Lidskjalv's group
    write_settings(fake_home, settings)
    original = copy.deepcopy(settings["hooks"]["Stop"][0]["hooks"][0])
    assert _run(fake_home, apply=True)[0] == 0
    group = load_settings(fake_home)["hooks"]["Stop"]
    assert len(group) == 1  # the existing Fylgja hook was repaired, none was added
    assert group[0]["hooks"][0] == original
    assert group[0]["hooks"][1]["async"] is True


# --------------------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------------------


def test_a_regular_file_instead_of_the_link_is_refused(fake_home: Layout) -> None:
    link = fake_home.home / ".claude" / "settings.json"
    content = settings_source(fake_home).read_bytes()
    link.unlink()
    link.write_bytes(content)
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "is a regular file, but dotfiles expects a link" in output
    assert "ln -s" in output
    assert tree_state(fake_home.home) == before


def test_a_missing_link_is_refused_with_the_command_to_restore_it(fake_home: Layout) -> None:
    (fake_home.home / ".claude" / "settings.json").unlink()
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "does not exist" in output
    assert "ln -s" in output
    assert not (fake_home.home / ".claude" / "settings.json").exists()


def test_a_link_to_somewhere_else_is_refused(fake_home: Layout) -> None:
    link = fake_home.home / ".claude" / "settings.json"
    elsewhere = fake_home.home / "elsewhere.json"
    elsewhere.write_text("{}\n", encoding="utf-8")
    link.unlink()
    link.symlink_to(elsewhere)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "not to ~/dotfiles/claude/.claude/settings.json" in output
    assert elsewhere.read_text(encoding="utf-8") == "{}\n"


def test_a_dangling_link_is_refused(fake_home: Layout) -> None:
    settings_source(fake_home).unlink()
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "dangling" in output


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{ not json", "is not valid JSON"),
        ('{"hooks": {"Stop": [}', "is not valid JSON"),
        ("[1, 2]\n", "is not a JSON object"),
        ('{\n  "permissions": {"allow": []}\n}\n', "not formatted the way Claude Code writes it"),
        ('{"a": 1}', "not formatted the way Claude Code writes it"),
    ],
)
def test_a_malformed_settings_file_is_refused_and_left_untouched(
    fake_home: Layout, content: str, message: str
) -> None:
    settings_source(fake_home).write_text(content, encoding="utf-8")
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert message in output
    assert "Nothing was changed" in output
    assert tree_state(fake_home.home) == before
    assert not paths.backups_dir().exists()


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"hooks": []}, '"hooks" in settings.json is not an object'),
        ({"hooks": {"Stop": {}}}, '"hooks.Stop" in settings.json is not a list'),
    ],
)
def test_a_hooks_structure_of_the_wrong_shape_is_refused(
    fake_home: Layout, settings: dict[str, Any], message: str
) -> None:
    write_settings(fake_home, settings)
    before = tree_state(fake_home.home)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert message in output
    assert tree_state(fake_home.home) == before


def test_odd_entries_are_left_alone(fake_home: Layout) -> None:
    settings = {"hooks": {"Stop": ["text", {"hooks": "x"}, {"matcher": "", "hooks": [7]}]}}
    write_settings(fake_home, settings)
    assert _run(fake_home, apply=True, events=["Stop"])[0] == 0
    stop = load_settings(fake_home)["hooks"]["Stop"]
    assert stop[:3] == settings["hooks"]["Stop"]
    assert len(stop) == 4


def test_merge_hooks_with_no_events_does_not_touch_settings() -> None:
    settings: dict[str, Any] = {}
    merge_hooks(settings, [])
    assert settings == {}


def test_an_unreadable_settings_file_is_reported(fake_home: Layout) -> None:
    settings_source(fake_home).write_bytes(b"\xff\xfe{}")
    code, output = _run(fake_home, apply=False)
    assert code == 1
    assert "cannot read" in output


def test_a_directory_link_for_dot_claude_is_refused(fake_home: Layout) -> None:
    real = fake_home.home / "real-claude"
    os.rename(fake_home.home / ".claude", real)
    (fake_home.home / ".claude").symlink_to(real)
    code, output = _run(fake_home, apply=True)
    assert code == 1
    assert "is a link to a directory" in output


def test_the_cli_commands_are_registered() -> None:
    from typer.testing import CliRunner

    from fylgja.cli.app import app

    result = CliRunner().invoke(app, ["install", "--help"])
    assert result.exit_code == 0
    assert "hooks" in result.output
    assert "statusline" in result.output
    for command in ("hooks", "statusline"):
        assert CliRunner().invoke(app, ["install", command, "--help"]).exit_code == 0
