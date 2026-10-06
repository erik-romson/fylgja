"""Every ``fylgja doctor`` check in its ok, warn and fail form, with fakes (WP1.10, R1, A22)."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from fylgja import paths
from fylgja.cli import client
from fylgja.cli.doctor import (
    Check,
    Status,
    check_collector_doctors,
    check_config,
    check_daemon,
    check_daemon_collectors,
    check_dotfiles_layout,
    check_fda_paths,
    check_hook_commands,
    check_plugins,
    check_port,
    command_targets,
    exit_code,
    port_in_use,
    render_checks,
    render_json,
    run_doctor,
)
from fylgja.cli.install import CONFIG_ENTRY, DOTFILES_CONF, Layout
from fylgja.collectors.base import (
    COLLECTOR_API_MAJOR,
    Capability,
    Collector,
    CollectorContext,
    CollectorPlugin,
)
from fylgja.collectors.registry import EntryPointLike, load_registry
from fylgja.config import Config, parse_config
from fylgja.models import HealthReport, HealthState
from fylgja.testing.fakes import FakeProcessRunner

NOW = datetime(2026, 10, 5, tzinfo=UTC)


# --------------------------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Dist:
    name: str
    version: str = "1.0"


@dataclass
class _EntryPoint:
    name: str
    dist: _Dist | None
    loader: Callable[[], object]
    value: str = "mod:plugin"
    loads: int = field(default=0)

    def load(self) -> object:
        self.loads += 1
        return self.loader()


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Probe:
    """A collector whose ``doctor()`` answers as scripted."""

    def __init__(self, answer: Callable[[], Any]) -> None:
        self._answer = answer

    async def run(self) -> None:
        return None

    async def doctor(self) -> HealthReport:
        result = self._answer()
        if asyncio.iscoroutine(result):
            return await result  # type: ignore[no-any-return]
        return result  # type: ignore[no-any-return]


def _plugin(name: str, answer: Callable[[], Any], api_major: int = COLLECTOR_API_MAJOR) -> Any:
    def factory(settings: BaseModel, context: CollectorContext) -> Collector:
        return _Probe(answer)

    return CollectorPlugin(
        name=name,
        api_major=api_major,
        capabilities=frozenset({Capability.CONTEXT}),
        config_model=_Settings,
        factory=factory,
    )


def _report(state: HealthState, detail: str = "detail", kind: str | None = None) -> HealthReport:
    return HealthReport(state=state, detail=detail, error_kind=kind, checked_at=NOW)


def _source(*entries: _EntryPoint) -> Callable[[], list[EntryPointLike]]:
    return lambda: list(entries)  # type: ignore[arg-type]


def _builtin(name: str, plugin: object | None = None, **kw: Any) -> _EntryPoint:
    return _EntryPoint(name, _Dist("fylgja"), lambda: plugin, **kw)


def _config(extra: dict[str, Any] | None = None) -> Config:
    return parse_config(extra or {}, source="test")


# --------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------


def test_config_without_a_file_is_ok_and_names_the_defaults() -> None:
    check, config = check_config()
    assert check.status is Status.OK
    assert "defaults apply" in check.detail
    assert config.api.port == 7319


def test_config_that_is_valid_is_ok() -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("[api]\nport = 7400\n", encoding="utf-8")
    check, config = check_config()
    assert check.status is Status.OK
    assert "is valid" in check.detail
    assert config.api.port == 7400


@pytest.mark.parametrize("text", ["[api\nport = 1", "[api]\nhost = '0.0.0.0'\n"])
def test_config_that_is_invalid_fails_and_the_rest_runs_on_defaults(text: str) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(text, encoding="utf-8")
    check, config = check_config()
    assert check.status is Status.FAIL
    assert "config.toml" in check.detail
    assert "defaults" in check.detail
    assert config.api.port == 7319


# --------------------------------------------------------------------------------------------
# Plugins and sources
# --------------------------------------------------------------------------------------------


def _checks_by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def test_plugins_loaded_disabled_failed_and_duplicate() -> None:
    good = _builtin("good", _plugin("good", lambda: _report(HealthState.OK)))
    broken = _EntryPoint("broken", _Dist("fylgja-broken"), lambda: 1 / 0)
    old = _EntryPoint(
        "old", _Dist("fylgja-old"), lambda: _plugin("old", lambda: None, api_major=99)
    )
    off = _EntryPoint("off", _Dist("fylgja-off"), lambda: pytest.fail("must not be imported"))
    twin_a = _EntryPoint("twin", _Dist("fylgja-a"), lambda: None)
    twin_b = _EntryPoint("twin", _Dist("fylgja-b"), lambda: None)
    config = _config({"collectors": {"broken": {"enabled": True}, "old": {"enabled": True}}})
    registry = load_registry(config, _source(good, broken, old, off, twin_a, twin_b))
    checks = _checks_by_name(check_plugins(registry))
    assert checks["plugin:good"].status is Status.OK
    assert "loaded, collector API 1" in checks["plugin:good"].detail
    assert checks["plugin:off"] == Check(
        "plugin:off", Status.OK, "external: disabled (not imported)"
    )
    assert off.loads == 0
    assert checks["plugin:broken"].status is Status.FAIL
    assert "import" in checks["plugin:broken"].detail
    assert checks["plugin:old"].status is Status.FAIL
    assert "api_major" in checks["plugin:old"].detail
    assert "99" in checks["plugin:old"].detail
    duplicates = [c for c in check_plugins(registry) if c.name == "plugin:twin"]
    assert len(duplicates) == 2
    assert all(c.status is Status.FAIL and "duplicate" in c.detail for c in duplicates)


def test_a_disabled_built_in_is_ok_and_not_listed_as_not_imported() -> None:
    entry = _builtin("quiet", _plugin("quiet", lambda: None))
    registry = load_registry(_config({"collectors": {"quiet": {"enabled": False}}}), _source(entry))
    (check,) = check_plugins(registry)
    assert check == Check("plugin:quiet", Status.OK, "built-in: disabled")


def _collector_checks(*entries: _EntryPoint, config: Config | None = None) -> dict[str, Check]:
    registry = load_registry(config or _config(), _source(*entries))
    return _checks_by_name(asyncio.run(check_collector_doctors(registry)))


@pytest.mark.parametrize(
    ("state", "status"),
    [
        (HealthState.OK, Status.OK),
        (HealthState.STARTING, Status.WARN),
        (HealthState.DEGRADED, Status.WARN),
        (HealthState.FAILED, Status.FAIL),
    ],
)
def test_each_collectors_doctor_maps_its_state(state: HealthState, status: Status) -> None:
    def answer() -> HealthReport:
        return _report(state, "binary found", "binary" if state is HealthState.FAILED else None)

    checks = _collector_checks(_builtin("probe", _plugin("probe", answer)))
    check = checks["source:probe"]
    assert check.status is status
    assert state.value in check.detail
    assert "binary found" in check.detail


def test_a_collector_doctor_that_raises_fails_without_its_message() -> None:
    def explode() -> HealthReport:
        raise RuntimeError("secret detail")

    check = _collector_checks(_builtin("probe", _plugin("probe", explode)))["source:probe"]
    assert check.status is Status.FAIL
    assert "RuntimeError" in check.detail
    assert "secret detail" not in check.detail


def test_a_collector_doctor_that_hangs_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fylgja.cli.doctor.COLLECTOR_DOCTOR_TIMEOUT", 0.05)

    async def hang() -> HealthReport:
        await asyncio.sleep(30)
        raise AssertionError

    check = _collector_checks(_builtin("probe", _plugin("probe", hang)))["source:probe"]
    assert check.status is Status.WARN
    assert "did not answer" in check.detail


def test_only_enabled_and_loadable_collectors_are_asked() -> None:
    off = _builtin("off", _plugin("off", lambda: pytest.fail("not asked")))
    checks = _collector_checks(off, config=_config({"collectors": {"off": {"enabled": False}}}))
    assert checks == {}


# --------------------------------------------------------------------------------------------
# FDA-path guard
# --------------------------------------------------------------------------------------------


def _home(isolated_env: Path) -> Path:
    return isolated_env / "home"


def test_no_configured_path_under_a_protected_folder_is_ok(isolated_env: Path) -> None:
    check = check_fda_paths(_config(), _home(isolated_env))
    assert check.status is Status.OK


def test_a_configured_path_under_library_containers_fails(isolated_env: Path) -> None:
    home = _home(isolated_env)
    protected = home / "Library" / "Containers" / "com.example.app" / "bin" / "tool"
    check = check_fda_paths(_config({"paths": {"gh": str(protected)}}), home)
    assert check.status is Status.FAIL
    assert "paths.gh" in check.detail
    assert "Full Disk Access" in check.detail
    assert not protected.parent.exists()  # never created, never read


def test_a_collector_setting_under_group_containers_fails(isolated_env: Path) -> None:
    home = _home(isolated_env)
    path = "~/Library/Group Containers/group.example/data"
    config = _config({"collectors": {"probe": {"root": path, "nested": {"dirs": [path]}}}})
    check = check_fda_paths(config, home)
    assert check.status is Status.FAIL
    assert "collectors.probe.root" in check.detail
    assert "collectors.probe.nested.dirs[0]" in check.detail


def test_a_symlink_that_resolves_into_a_protected_folder_fails(isolated_env: Path) -> None:
    home = _home(isolated_env)
    link = isolated_env / "innocent"
    link.symlink_to(home / "Library" / "Containers" / "com.example.app")
    check = check_fda_paths(_config({"paths": {"git": str(link / "git")}}), home)
    assert check.status is Status.FAIL
    assert "resolves into a protected folder" in check.detail


def test_a_data_directory_under_containers_fails(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(isolated_env)
    monkeypatch.setenv("FYLGJA_DATA_DIR", str(home / "Library" / "Containers" / "x"))
    check = check_fda_paths(_config(), home)
    assert check.status is Status.FAIL
    assert "data directory" in check.detail


def test_a_sibling_with_a_similar_name_is_not_protected(isolated_env: Path) -> None:
    home = _home(isolated_env)
    check = check_fda_paths(
        _config({"paths": {"gh": str(home / "Library" / "ContainersBackup" / "gh")}}), home
    )
    assert check.status is Status.OK


# --------------------------------------------------------------------------------------------
# Daemon and port
# --------------------------------------------------------------------------------------------


class _Fetch:
    """A fake ``client.get_json`` that records every request (doctor only ever issues GETs)."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.requests: list[tuple[str, str | None]] = []

    def __call__(self, path: str, *, url: str | None = None, timeout: float | None = None) -> Any:
        self.requests.append((path, url))
        answer = self.answers[path]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def test_daemon_up_is_ok_and_uses_the_configured_port() -> None:
    fetch = _Fetch({"/health": {"status": "ok"}})
    check, up = check_daemon(_config({"api": {"port": 7400}}), fetch)
    assert (check.status, up) == (Status.OK, True)
    assert fetch.requests == [("/health", "http://127.0.0.1:7400")]


def test_daemon_down_warns_and_says_how_to_start_it() -> None:
    fetch = _Fetch({"/health": client.DaemonDown("http://127.0.0.1:7319", "connection refused")})
    check, up = check_daemon(_config(), fetch)
    assert (check.status, up) == (Status.WARN, False)
    assert "fylgja serve" in check.detail
    assert "install launchd" in check.detail


def test_something_else_answering_on_the_port_fails() -> None:
    fetch = _Fetch({"/health": client.ApiError("not JSON")})
    check, up = check_daemon(_config(), fetch)
    assert (check.status, up) == (Status.FAIL, False)
    assert "not as Fylgja" in check.detail


def _rows(*states: tuple[str, str, str]) -> list[dict[str, Any]]:
    return [
        {"name": name, "state": state, "health": {"state": health}}
        for name, state, health in states
    ]


@pytest.mark.parametrize(
    ("rows", "status", "fragment"),
    [
        (_rows(("a", "enabled", "ok"), ("b", "disabled", "disabled")), Status.OK, "1 enabled"),
        (_rows(("a", "enabled", "ok"), ("b", "enabled", "degraded")), Status.WARN, "b degraded"),
        (_rows(("a", "enabled", "failed")), Status.FAIL, "a failed"),
        ([{"name": "x", "state": "enabled", "health": "not_loaded"}], Status.WARN, "x unknown"),
        ("nope", Status.FAIL, "did not return a list"),
    ],
)
def test_the_daemons_collectors(rows: Any, status: Status, fragment: str) -> None:
    check = check_daemon_collectors(_config(), _Fetch({"/collectors": rows}))
    assert check.status is status
    assert fragment in check.detail


def test_the_daemons_collectors_unreadable_warns() -> None:
    fetch = _Fetch({"/collectors": client.DaemonDown("u", "timed out")})
    assert check_daemon_collectors(_config(), fetch).status is Status.WARN


def test_port_free_ok_held_by_the_daemon_ok_held_by_another_process_fails() -> None:
    config = _config({"api": {"port": 7400}})
    assert check_port(config, False, lambda port: False) == Check("port", Status.OK, "7400 is free")
    assert check_port(config, True, lambda port: pytest.fail("not probed")).status is Status.OK
    held = check_port(config, False, lambda port: port == 7400)
    assert held.status is Status.FAIL
    assert "another process" in held.detail
    assert check_port(_config({"api": {"port": 0}}), False, lambda p: True).status is Status.OK


def test_the_real_port_probe_sees_a_bound_port_and_a_free_one() -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        busy = holder.getsockname()[1]
        assert port_in_use(busy) is True
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert port_in_use(free) is False


# --------------------------------------------------------------------------------------------
# Hook commands (R1)
# --------------------------------------------------------------------------------------------


def _home_dir(root: Path) -> Path:
    return root / "home"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("~/.claude/x.sh Stop", [("{h}/.claude/x.sh", True, True, False)]),
        ("$HOME/bin/x --flag", [("{h}/bin/x", True, True, False)]),
        ("${HOME}/bin/x", [("{h}/bin/x", True, True, False)]),
        ("FOO=1 BAR=2 /opt/tool/run", [("/opt/tool/run", True, True, False)]),
        ("source ~/.claude/env.sh switch && echo ok", [("{h}/.claude/env.sh", True, False, False)]),
        (". ~/.claude/env.sh", [("{h}/.claude/env.sh", True, False, False)]),
        ("a-tool arg | b-tool", [("a-tool", False, True, False), ("b-tool", False, True, False)]),
        ('echo hi >> "$CLAUDE_ENV_FILE" && exit 0', []),
        ("jq . 2>&1 >/dev/null; true", [("jq", False, True, False)]),
        ("$UNKNOWN/bin/x", []),
        ("./relative/x", []),
        ("nohup env FOO=1 /opt/tool/run &", [("/opt/tool/run", True, True, False)]),
        ("if [ -x ~/a ]; then ~/a go; fi", [("{h}/a", True, True, True)]),
        (
            "[ -x ~/.local/bin/fylgja ] && ~/.local/bin/fylgja hook Stop || exit 0",
            [("{h}/.local/bin/fylgja", True, True, True)],
        ),
        ("test -f ~/g && ~/g", [("{h}/g", True, True, True)]),
        ("unterminated 'quote", []),
        ("", []),
    ],
)
def test_command_targets(
    command: str, expected: list[tuple[str, bool, bool, bool]], isolated_env: Path
) -> None:
    home = _home_dir(isolated_env)
    got = [(t.value, t.is_path, t.run, t.guarded) for t in command_targets(command, home)]
    assert got == [(v.format(h=home), *rest) for v, *rest in expected]


def _write_settings(layout: Layout, settings: dict[str, Any] | str) -> Path:
    path = layout.home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        path.unlink()
    path.write_text(settings if isinstance(settings, str) else json.dumps(settings), "utf-8")
    return path


def _hooks(*commands: str, event: str = "Stop") -> dict[str, Any]:
    return {
        "hooks": {
            event: [{"matcher": "", "hooks": [{"type": "command", "command": c} for c in commands]}]
        }
    }


def test_hook_commands_of_the_fixture_settings_are_ok(fake_home: Layout) -> None:
    check = check_hook_commands(layout=fake_home)
    assert check.status is Status.OK, check.detail
    assert "none missing" in check.detail


def test_a_dangling_hook_target_fails_and_names_the_link(fake_home: Layout) -> None:
    (fake_home.dotfiles / "claude" / ".claude" / "lidskjalv.sh").unlink()
    check = check_hook_commands(layout=fake_home)
    assert check.status is Status.FAIL
    assert "~/.claude/lidskjalv.sh" in check.detail
    assert "dangling link to" in check.detail
    assert "problem(s)" in check.detail
    # 14 registrations call the one script: it is reported once.
    assert check.detail.count("~/.claude/lidskjalv.sh") == 1


def test_a_missing_hook_target_fails(fake_home: Layout) -> None:
    _write_settings(fake_home, _hooks("~/.claude/nowhere.sh Stop"))
    check = check_hook_commands(layout=fake_home)
    assert check.status is Status.FAIL
    assert "Stop: ~/.claude/nowhere.sh (missing)" in check.detail


def test_a_missing_sourced_file_fails(fake_home: Layout) -> None:
    _write_settings(fake_home, _hooks("source ~/.claude/gone.sh && echo done"))
    assert check_hook_commands(layout=fake_home).status is Status.FAIL


def test_a_guarded_missing_target_is_allowed(fake_home: Layout) -> None:
    command = "[ -x ~/.local/bin/fylgja ] && ~/.local/bin/fylgja hook Stop || exit 0"
    _write_settings(fake_home, _hooks(command))
    check = check_hook_commands(layout=fake_home)
    assert check.status is Status.OK, check.detail


def test_a_missing_bare_command_and_a_non_executable_script_warn(fake_home: Layout) -> None:
    script = fake_home.home / ".claude" / "format-file.sh"
    os.chmod(script.resolve(), 0o644)
    _write_settings(
        fake_home, _hooks("definitely-not-a-command-xyz arg", "~/.claude/format-file.sh")
    )
    check = check_hook_commands(layout=fake_home)
    assert check.status is Status.WARN
    assert "not found on PATH" in check.detail
    assert "not executable" in check.detail
    assert not stat.S_IMODE(script.resolve().stat().st_mode) & 0o111


def test_a_failure_outranks_a_warning(fake_home: Layout) -> None:
    _write_settings(fake_home, _hooks("definitely-not-a-command-xyz", "~/.claude/nowhere.sh"))
    assert check_hook_commands(layout=fake_home).status is Status.FAIL


def test_many_problems_are_summarised(fake_home: Layout) -> None:
    _write_settings(fake_home, _hooks(*[f"~/.claude/missing-{n}.sh" for n in range(9)]))
    check = check_hook_commands(layout=fake_home)
    assert "9 problem(s)" in check.detail
    assert "and 4 more" in check.detail


def test_the_status_line_command_is_checked_too(fake_home: Layout) -> None:
    _write_settings(
        fake_home, {"statusLine": {"type": "command", "command": "~/.claude/no-line.sh"}}
    )
    check = check_hook_commands(layout=fake_home)
    assert check.status is Status.FAIL
    assert "statusLine: ~/.claude/no-line.sh" in check.detail


def test_a_settings_file_that_is_absent_unreadable_or_malformed(fake_home: Layout) -> None:
    path = fake_home.home / ".claude" / "settings.json"
    path.unlink()
    assert check_hook_commands(layout=fake_home).status is Status.OK
    path.mkdir()
    assert check_hook_commands(layout=fake_home).status is Status.WARN  # a directory: unreadable
    path.rmdir()
    _write_settings(fake_home, "{not json")
    assert check_hook_commands(layout=fake_home).status is Status.FAIL
    _write_settings(fake_home, "[]")
    assert check_hook_commands(layout=fake_home).status is Status.FAIL


def test_odd_but_valid_settings_shapes_are_tolerated(fake_home: Layout) -> None:
    _write_settings(
        fake_home,
        {"hooks": {"Stop": "no", "Start": [1, {"hooks": "x"}, {"hooks": [3, {"command": 4}]}]}},
    )
    assert check_hook_commands(layout=fake_home).status is Status.OK


# --------------------------------------------------------------------------------------------
# Dotfiles layout (A22)
# --------------------------------------------------------------------------------------------


def _install_config_files(layout: Layout, *, listed: bool = True) -> Path:
    source = layout.dotfiles / CONFIG_ENTRY.package / CONFIG_ENTRY.path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# config\n", encoding="utf-8")
    if listed:
        conf = layout.dotfiles / DOTFILES_CONF
        conf.write_text(
            conf.read_text(encoding="utf-8").replace(")\n", f'    "{CONFIG_ENTRY}"\n)\n'),
            encoding="utf-8",
        )
    return source


def _layout_checks(layout: Layout, runner: FakeProcessRunner | None = None) -> list[Check]:
    return asyncio.run(check_dotfiles_layout(layout, runner))


def test_nothing_installed_is_ok(fake_home: Layout) -> None:
    (check,) = _layout_checks(fake_home)
    assert check == Check("dotfiles:fylgja:.config/fylgja/config.toml", Status.OK, "not installed")


def test_a_hand_written_config_outside_dotfiles_is_not_fylgjas_to_manage(
    fake_home: Layout,
) -> None:
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text("[api]\nport = 7400\n", encoding="utf-8")
    (check,) = _layout_checks(fake_home)
    assert check.status is Status.OK
    assert check.detail == "not installed"


def test_a_link_into_dotfiles_whose_file_is_gone_fails(fake_home: Layout) -> None:
    source = fake_home.dotfiles / CONFIG_ENTRY.package / CONFIG_ENTRY.path
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().symlink_to(source)  # nothing was ever written there
    (check,) = _layout_checks(fake_home)
    assert check.status is Status.FAIL
    assert "does not exist" in check.detail


def test_an_installed_config_that_is_a_listed_link_into_dotfiles_is_ok(fake_home: Layout) -> None:
    source = _install_config_files(fake_home)
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().symlink_to(source)
    (check,) = _layout_checks(fake_home)
    assert check.status is Status.OK, check.detail
    assert "links into dotfiles, listed" in check.detail


def test_a_regular_file_where_a_link_is_expected_fails(fake_home: Layout) -> None:
    _install_config_files(fake_home)
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text("# a copy\n", encoding="utf-8")
    (check,) = _layout_checks(fake_home)
    assert check.status is Status.FAIL
    assert "regular file, not a link into dotfiles" in check.detail
    assert "ln -s" in check.detail


def test_a_missing_link_a_stale_link_and_an_unlisted_entry_fail(
    fake_home: Layout, isolated_env: Path
) -> None:
    source = _install_config_files(fake_home, listed=False)
    (check,) = _layout_checks(fake_home)
    assert check.status is Status.FAIL
    assert "is missing; link it" in check.detail
    assert f"{CONFIG_ENTRY} is not listed in dotfiles.conf" in check.detail
    paths.config_file().parent.mkdir(parents=True)
    other = isolated_env / "other.toml"
    other.write_text("", encoding="utf-8")
    paths.config_file().symlink_to(other)
    (check,) = _layout_checks(fake_home)
    assert "points to" in check.detail
    assert "not into ~/dotfiles/fylgja/.config/fylgja/config.toml" in check.detail
    paths.config_file().unlink()
    paths.config_file().symlink_to(source)
    source.unlink()
    (check,) = _layout_checks(fake_home)
    assert "does not exist" in check.detail


def test_uncommitted_fylgja_changes_in_dotfiles_warn(fake_home: Layout) -> None:
    source = _install_config_files(fake_home)
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().symlink_to(source)
    (fake_home.dotfiles / ".git").mkdir()
    runner = FakeProcessRunner()
    runner.script("/usr/bin/git", stdout="?? fylgja/.config/fylgja/config.toml\n M dotfiles.conf\n")
    checks = _checks_by_name(_layout_checks(fake_home, runner))
    warn = checks["dotfiles:uncommitted"]
    assert warn.status is Status.WARN
    assert "fylgja/.config/fylgja/config.toml" in warn.detail
    assert "dotfiles.conf" in warn.detail
    assert runner.calls == [
        (
            "/usr/bin/git",
            "-C",
            str(fake_home.dotfiles),
            "status",
            "--porcelain",
            "--",
            "fylgja/.config/fylgja/config.toml",
            "dotfiles.conf",
        )
    ]


@pytest.mark.parametrize("mode", ["clean", "git-error", "git-missing", "no-repo"])
def test_no_uncommitted_warning_when_clean_or_git_is_unavailable(
    fake_home: Layout, mode: str
) -> None:
    source = _install_config_files(fake_home)
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().symlink_to(source)
    if mode != "no-repo":
        (fake_home.dotfiles / ".git").mkdir()
    runner = FakeProcessRunner()
    if mode == "clean":
        runner.script("/usr/bin/git", stdout="")
    elif mode == "git-error":
        runner.script("/usr/bin/git", returncode=128, stderr="fatal")
    elif mode == "git-missing":
        runner.script("/usr/bin/git", error=FileNotFoundError())
    checks = _layout_checks(fake_home, runner)
    assert [c.name for c in checks] == ["dotfiles:fylgja:.config/fylgja/config.toml"]


def test_a_missing_dotfiles_conf_lists_nothing(fake_home: Layout) -> None:
    source = _install_config_files(fake_home, listed=False)
    (fake_home.dotfiles / DOTFILES_CONF).unlink()
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().symlink_to(source)
    (check,) = _layout_checks(fake_home)
    assert check.status is Status.FAIL
    assert "is not listed" in check.detail


# --------------------------------------------------------------------------------------------
# The whole run and its output
# --------------------------------------------------------------------------------------------


def test_run_doctor_combines_every_check_and_only_reads(fake_home: Layout) -> None:
    good = _builtin("probe", _plugin("probe", lambda: _report(HealthState.OK, "fine")))
    fetch = _Fetch({"/health": {"status": "ok"}, "/collectors": _rows(("probe", "enabled", "ok"))})
    runner = FakeProcessRunner()
    before = {p: p.read_bytes() for p in fake_home.dotfiles.rglob("*") if p.is_file()}
    checks = asyncio.run(
        run_doctor(
            runner=runner, layout=fake_home, fetch=fetch, probe=lambda p: True, source=_source(good)
        )
    )
    names = [c.name for c in checks]
    assert names == [
        "config",
        "plugin:probe",
        "source:probe",
        "fda-paths",
        "daemon",
        "daemon-collectors",
        "port",
        "hook-commands",
        "dotfiles:fylgja:.config/fylgja/config.toml",
        "hooks:executable",
        "hooks:async",
        "claude-version",
        "inbox:access",
        "inbox:backlog",
        "inbox:dropped",
        "lidskjalv-hooks",
        "messaging",
        "applescript:chrome",
        "applescript:iterm2",
    ]
    assert exit_code(checks) == 0
    assert {path for path, _ in fetch.requests} == {"/health", "/collectors"}
    assert runner.calls == []
    assert {p: p.read_bytes() for p in fake_home.dotfiles.rglob("*") if p.is_file()} == before


def test_run_doctor_with_the_daemon_down_warns_and_exits_zero(fake_home: Layout) -> None:
    fetch = _Fetch({"/health": client.DaemonDown("u", "connection refused")})
    checks = asyncio.run(
        run_doctor(
            runner=FakeProcessRunner(),
            layout=fake_home,
            fetch=fetch,
            probe=lambda p: False,
            source=_source(),
        )
    )
    by_name = _checks_by_name(checks)
    assert by_name["daemon"].status is Status.WARN
    assert "daemon-collectors" not in by_name
    assert exit_code(checks) == 0
    assert [path for path, _ in fetch.requests] == ["/health"]


def test_a_failing_check_makes_the_exit_code_one(fake_home: Layout) -> None:
    (fake_home.dotfiles / "claude" / ".claude" / "lidskjalv.sh").unlink()
    checks = asyncio.run(
        run_doctor(
            runner=FakeProcessRunner(),
            layout=fake_home,
            fetch=_Fetch({"/health": client.DaemonDown("u", "refused")}),
            probe=lambda p: False,
            source=_source(),
        )
    )
    assert _checks_by_name(checks)["hook-commands"].status is Status.FAIL
    assert exit_code(checks) == 1


def test_exit_code_allows_warnings() -> None:
    assert exit_code([Check("a", Status.OK, ""), Check("b", Status.WARN, "")]) == 0
    assert exit_code([Check("a", Status.FAIL, "")]) == 1
    assert exit_code([]) == 0


def test_render_json_and_table() -> None:
    checks = [
        Check("config", Status.OK, "fine"),
        Check("daemon", Status.WARN, "down"),
        Check("port", Status.FAIL, "held"),
    ]
    data = json.loads(render_json(checks))
    assert data["summary"] == {"ok": 1, "warn": 1, "fail": 1}
    assert data["checks"][2] == {"name": "port", "status": "fail", "detail": "held"}
    table = render_checks(checks, 80)
    lines = table.splitlines()
    assert lines[0].split() == ["STATUS", "CHECK", "DETAIL"]
    assert any(line.startswith("FAIL") and "port" in line for line in lines)
    assert lines[-1] == "1 ok, 1 warn, 1 fail"
