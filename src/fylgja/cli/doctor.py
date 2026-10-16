"""``fylgja doctor``: local diagnostics (WP1.10, A22, R1, ``PLAN-final-plan.md`` §4.4).

Runs in the CLI process (an A7 exception) and, when the daemon is up, also reads ``/collectors``.
Doctor changes nothing: it makes no request except ``GET``, writes no file and starts nothing. It
imports a plugin only under the registry's rules (a built-in, or an external with an explicit
``enabled = true``).

Each check is ``ok``, ``warn`` or ``fail``. Doctor exits 0 when nothing fails (warnings are
allowed), else 1.

The checks, in order: configuration, plugins, the side-effect-free ``doctor()`` of each enabled
collector, the FDA-path guard, daemon reachability, the daemon's view of the collectors, the port,
dangling hook commands in ``~/.claude/settings.json`` (R1), the A22 dotfiles layout, and the
hook installation (WP2.4): the executable, async registrations, the installed Claude version, the
inbox (owner and mode, backlog age, ``dropped.count``), uncommitted dotfiles changes and, only when
Lidskjalv's entries are present, an informational look at them that never affects health (A21).
Then local messaging (WP3.6): whether it is on, and per live Claude session its version
compatibility, socket safety and, in plain words, what it will do with a message ("messages to
this session will be held for approval"). Doctor never sends a probe message.
Last come the AppleScript switches of application context (WP5.3, §4.4): each switch that is on
runs its osascript with a 3 s timeout, and a timeout reports the switch as degraded and disabled
until doctor is rerun.

Doctor changes nothing except one small file: the AppleScript switch record in the state
directory (:mod:`fylgja.collectors.appcontext.switches`), written only for a switch that is on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import socket
import stat
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from fylgja import paths
from fylgja.cli import client
from fylgja.cli.install import (
    BLOCK_BEGIN,
    DOTFILES_CONF,
    GIT,
    SETTINGS_ENTRY,
    STATUSLINE_ENTRY,
    DotfilesTarget,
    InstallError,
    Layout,
    detect_claude_version,
    fylgja_hook_event,
    installed_targets,
    iter_hooks,
    parse_dotfiles_conf,
    supported_events,
)
from fylgja.collectors.appcontext import switches as applescript
from fylgja.collectors.base import CollectorPlugin, RecordSink
from fylgja.collectors.claude import parse as registry_parse
from fylgja.collectors.claude.adapter import PsFailure, ps_start_times
from fylgja.collectors.context import build_context
from fylgja.collectors.registry import EntryPointSource, RegisteredCollector, load_registry
from fylgja.collectors.seams import ProcessRunner
from fylgja.config import Config, ConfigError, load_config, parse_config
from fylgja.core.system import LocalFileSystem
from fylgja.messaging.claude import policy
from fylgja.messaging.claude.encoders import default_encoder
from fylgja.messaging.claude.encoders.v1 import SUPPORTED as FRAME_SUPPORT
from fylgja.messaging.claude.messenger import registry_dir
from fylgja.messaging.claude.resolve import (
    PS,
    Refusal,
    check_socket,
    default_roots,
    parse_version,
    read_rows,
    version_gate,
)
from fylgja.models import HealthState, Record

__all__ = [
    "Check",
    "Status",
    "check_collector_doctors",
    "check_config",
    "check_daemon",
    "check_daemon_collectors",
    "check_dotfiles_layout",
    "check_fda_paths",
    "check_applescript",
    "check_claude_version",
    "check_hook_async",
    "check_hook_commands",
    "check_hook_executable",
    "check_hooks_committed",
    "check_inbox",
    "check_lidskjalv_hooks",
    "check_messaging",
    "check_notify",
    "check_plugins",
    "check_port",
    "command_targets",
    "exit_code",
    "fylgja_registrations",
    "render_checks",
    "read_settings",
    "render_json",
    "run_doctor",
]

BACKLOG_WARN_SECONDS: Final = 300.0
BACKLOG_FAIL_SECONDS: Final = 3600.0
DROPPED_FAIL: Final = 1000
LIDSKJALV_SCRIPT: Final = "lidskjalv.sh"
COLLECTOR_DOCTOR_TIMEOUT: Final = 10.0
GIT_TIMEOUT: Final = 15.0
MAX_LISTED: Final = 5
NTFY_TOPIC_MIN: Final = 20


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    informational: bool = False
    """True for a finding about something that is not Fylgja's: it is shown, but it never counts
    towards the exit code or the summary (A21)."""


FetchJson = Callable[..., Any]
PortProbe = Callable[[int], bool]
IsExecutable = Callable[[str], bool]


def _worst(statuses: Sequence[Status]) -> Status:
    if Status.FAIL in statuses:
        return Status.FAIL
    return Status.WARN if Status.WARN in statuses else Status.OK


def _listed(items: Sequence[str]) -> str:
    shown = "; ".join(items[:MAX_LISTED])
    extra = len(items) - MAX_LISTED
    return shown if extra <= 0 else f"{shown}; and {extra} more"


# --------------------------------------------------------------------------------------------
# Configuration, plugins and sources
# --------------------------------------------------------------------------------------------


def check_config(path: Path | None = None) -> tuple[Check, Config]:
    """The configuration check, and the configuration the other checks should use.

    A broken configuration fails the check; the later checks then run on the defaults so that one
    mistake does not hide every other finding.
    """
    target = paths.config_file() if path is None else path
    try:
        config = load_config(target)
    except ConfigError as exc:
        return (
            Check("config", Status.FAIL, f"{exc} (the checks below use the defaults)"),
            parse_config({}, source="defaults.toml"),
        )
    detail = f"{target} is valid" if target.exists() else f"no {target.name}; defaults apply"
    return Check("config", Status.OK, detail), config


def check_items(config: Config) -> Check:
    """The ticket prefixes and the default ranker that the navigation items use."""
    prefixes = config.items.ticket_prefixes
    tickets = f"ticket prefixes: {', '.join(prefixes)}" if prefixes else "ticket detection off"
    return Check("items", Status.OK, f"{tickets} · default ranker: {config.items.default_ranker}")


def check_plugins(registry: Sequence[RegisteredCollector]) -> list[Check]:
    """One check per discovered plugin: duplicates, API mismatch and import failures fail."""
    checks: list[Check] = []
    for item in registry:
        origin = item.entry.origin.value
        name = f"plugin:{item.name}"
        health = item.health
        if item.rejected or (health is not None and health.state is HealthState.FAILED):
            kind = "failed" if health is None else (health.error_kind or "failed")
            detail = "" if health is None else (health.detail or "")
            checks.append(Check(name, Status.FAIL, f"{origin}: {kind}: {detail}".rstrip(": ")))
        elif not item.enabled:
            note = "disabled (not imported)" if item.plugin is None else "disabled"
            checks.append(Check(name, Status.OK, f"{origin}: {note}"))
        else:
            version = item.plugin.api_major if item.plugin is not None else "?"
            checks.append(Check(name, Status.OK, f"{origin}: loaded, collector API {version}"))
    return checks


class _DiscardSink:
    """A sink for the collectors doctor builds: doctor never records anything."""

    def emit(self, record: Record) -> None:
        return None

    async def emit_committed(self, record: Record) -> None:
        return None


_SINK: RecordSink = _DiscardSink()

_QUIET = logging.getLogger("fylgja.doctor")
_QUIET.addHandler(logging.NullHandler())
_QUIET.propagate = False
"""Collectors built by doctor log here, so a failing source never prints to the terminal."""

_HEALTH_STATUS: Final = {
    HealthState.OK: Status.OK,
    HealthState.DISABLED: Status.OK,
    HealthState.STARTING: Status.WARN,
    HealthState.DEGRADED: Status.WARN,
    HealthState.FAILED: Status.FAIL,
}


async def _one_collector_doctor(item: RegisteredCollector, plugin: CollectorPlugin) -> Check:
    name = f"source:{item.name}"
    try:
        assert item.settings is not None
        collector = plugin.factory(item.settings, build_context(item.name, _SINK, logger=_QUIET))
        report = await asyncio.wait_for(collector.doctor(), COLLECTOR_DOCTOR_TIMEOUT)
    except TimeoutError:
        return Check(
            name, Status.WARN, f"doctor() did not answer in {COLLECTOR_DOCTOR_TIMEOUT:g} s"
        )
    except Exception as exc:
        return Check(name, Status.FAIL, f"doctor() raised {type(exc).__name__}")
    status = _HEALTH_STATUS[report.state]
    detail = report.detail or report.state.value
    if report.error_kind:
        detail = f"{report.error_kind}: {detail}"
    return Check(name, status, f"{report.state.value}: {detail}")


async def check_collector_doctors(registry: Sequence[RegisteredCollector]) -> list[Check]:
    """Each enabled, loadable collector's own ``doctor()``."""
    checks: list[Check] = []
    for item in registry:
        if item.runnable and item.plugin is not None:
            checks.append(await _one_collector_doctor(item, item.plugin))
    return checks


# --------------------------------------------------------------------------------------------
# FDA-path guard
# --------------------------------------------------------------------------------------------


def _strings(value: object, key: str) -> Iterator[tuple[str, str]]:
    """Every string in ``value`` with its dotted key."""
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, Mapping):
        for name, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            yield from _strings(item, f"{key}.{name}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _strings(item, f"{key}[{index}]")


def configured_paths(config: Config) -> list[tuple[str, str]]:
    """``(setting, path)`` for every configured path: ``[paths]``, collector settings and the
    Fylgja directories. A collector setting counts when its string starts with ``/`` or ``~``."""
    found: list[tuple[str, str]] = [
        (f"paths.{name}", str(value)) for name, value in config.paths.model_dump().items()
    ]
    for collector, table in sorted(config.collectors.items()):
        found.extend(
            (name, text)
            for name, text in _strings(table, f"collectors.{collector}")
            if text.startswith(("/", "~"))
        )
    found.extend(
        [
            ("config directory", str(paths.config_dir())),
            ("data directory", str(paths.data_dir())),
            ("state directory", str(paths.state_dir())),
        ]
    )
    return found


def _under(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def check_fda_paths(config: Config, home: Path | None = None) -> Check:
    """No configured path may resolve under ``~/Library/Containers`` or ``Group Containers``.

    The lexical path is tested first and never resolved when it already falls under a protected
    root, so the guard does not touch the directories it protects.
    """
    base = Path.home() if home is None else home
    roots = [
        str(base / "Library" / "Containers"),
        str(base / "Library" / "Group Containers"),
    ]
    real_roots = [os.path.realpath(root) for root in roots]
    offenders: list[str] = []
    for setting, text in configured_paths(config):
        lexical = os.path.normpath(os.path.expanduser(text))
        if _under(lexical, roots) or _under(lexical, real_roots):
            offenders.append(f"{setting} = {text}")
            continue
        if _under(os.path.realpath(lexical), real_roots):
            offenders.append(f"{setting} = {text} (resolves into a protected folder)")
    if offenders:
        return Check(
            "fda-paths",
            Status.FAIL,
            "configured path under ~/Library/Containers or ~/Library/Group Containers, which "
            f"needs Full Disk Access and is never read: {_listed(offenders)}",
        )
    return Check("fda-paths", Status.OK, "no configured path is under a protected folder")


# --------------------------------------------------------------------------------------------
# Daemon and port
# --------------------------------------------------------------------------------------------


def _base_url(config: Config) -> str:
    return f"http://{client.LOOPBACK}:{config.api.port}"


def check_daemon(config: Config, fetch: FetchJson = client.get_json) -> tuple[Check, bool]:
    """Is the daemon reachable? A daemon that is down is a warning: doctor works without it."""
    url = _base_url(config)
    try:
        fetch("/health", url=url, timeout=client.TIMEOUT)
    except client.DaemonDown as exc:
        return (
            Check(
                "daemon",
                Status.WARN,
                f"not running at {url} ({exc.reason}); start it with `fylgja serve` "
                "or `fylgja install launchd --apply`",
            ),
            False,
        )
    except client.ApiError as exc:
        return Check("daemon", Status.FAIL, f"answers at {url}, but not as Fylgja: {exc}"), False
    return Check("daemon", Status.OK, f"answers at {url}"), True


def check_daemon_collectors(config: Config, fetch: FetchJson = client.get_json) -> Check:
    """The daemon's ``/collectors``: what the running supervisor says about each collector."""
    try:
        rows = fetch("/collectors", url=_base_url(config), timeout=client.TIMEOUT)
    except (client.DaemonDown, client.ApiError) as exc:
        return Check("daemon-collectors", Status.WARN, f"cannot read /collectors: {exc}")
    if not isinstance(rows, list):
        return Check("daemon-collectors", Status.FAIL, "/collectors did not return a list")
    states: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("state") != "enabled":
            continue
        health = row.get("health")
        state = health.get("state", "unknown") if isinstance(health, dict) else "unknown"
        states.append((str(row.get("name")), str(state)))
    problems = [f"{name} {state}" for name, state in states if state != "ok"]
    status = Status.OK
    if any(state == "failed" for _, state in states):
        status = Status.FAIL
    elif problems:
        status = Status.WARN
    if problems:
        return Check("daemon-collectors", status, f"not ok: {_listed(problems)}")
    return Check("daemon-collectors", Status.OK, f"{len(states)} enabled collectors, all ok")


def port_in_use(port: int) -> bool:
    """True when something already holds ``port`` on the loopback address."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((client.LOOPBACK, port))
        except OSError:
            return True
    return False


def check_port(config: Config, daemon_up: bool, probe: PortProbe = port_in_use) -> Check:
    """The configured port: held by the Fylgja daemon is fine, by anything else is a failure."""
    port = config.api.port
    if port == 0:
        return Check("port", Status.OK, "the port is 0: the system picks a free one")
    if daemon_up:
        return Check("port", Status.OK, f"{port} is held by the Fylgja daemon")
    if probe(port):
        return Check(
            "port",
            Status.FAIL,
            f"{port} is held by another process, so `fylgja serve` cannot start; stop it or "
            "set [api] port in config.toml",
        )
    return Check("port", Status.OK, f"{port} is free")


# --------------------------------------------------------------------------------------------
# Hook commands (R1)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandTarget:
    """A file or command a hook line depends on."""

    value: str
    """The path (expanded) or the bare command name."""
    is_path: bool
    run: bool
    """True when the line executes it (so it should be executable); False for ``source``."""
    guarded: bool = False
    """True when the line tests for it first, as the A22 ``[ -x … ] &&`` guard does."""


_ASSIGNMENT: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEPARATORS: Final = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"}
_REDIRECTS: Final = {">", ">>", "<", "<<", "<&", ">&", "&>", "&>>", ">|"}
_KEYWORDS: Final = {
    "if",
    "then",
    "else",
    "elif",
    "fi",
    "do",
    "done",
    "while",
    "until",
    "!",
    "{",
    "}",
}
_PREFIXES: Final = {"exec", "command", "nohup", "time", "env", "builtin"}
_SOURCES: Final = {"source", "."}
_TESTERS: Final = {"[", "[[", "test"}
_TEST_FLAGS: Final = {"-x", "-f", "-e", "-s", "-r", "-L", "-h"}
_BUILTINS: Final = {
    "echo", "printf", "exit", "true", "false", ":", "cd", "export", "set", "unset", "read",
    "return", "shift", "trap", "wait", "pwd", "type", "alias", "local", "eval", "kill",
}  # fmt: skip


def _expand(word: str, home: Path) -> str | None:
    """``word`` with ``~`` and ``$HOME`` expanded, or ``None`` if it needs any other expansion."""
    text = word
    if text == "~" or text.startswith("~/"):
        text = str(home) + text[1:]
    text = text.replace("${HOME}", str(home)).replace("$HOME", str(home))
    if "$" in text or "`" in text:
        return None
    return text


def _tokens(command: str) -> list[str] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _segments(tokens: Sequence[str]) -> Iterator[list[str]]:
    current: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in _SEPARATORS:
            if current:
                yield current
            current = []
        elif token in _REDIRECTS:
            skip_next = True
        else:
            current.append(token)
    if current:
        yield current


def command_targets(command: str, home: Path) -> list[CommandTarget]:
    """The executables and sourced files in a shell command line.

    Understands ``&&``, ``||``, ``;``, pipes, leading ``VAR=value``, ``source``/``.``, redirects
    and the ``[ -x path ] &&`` guard. Words that need an expansion other than ``~`` and ``$HOME``
    are skipped, as are shell builtins and anything it cannot parse.
    """
    tokens = _tokens(command)
    if tokens is None:
        return []
    targets: list[CommandTarget] = []
    guarded: set[str] = set()
    for words in _segments(tokens):
        index = 0
        while index < len(words) and (_ASSIGNMENT.match(words[index]) or words[index] in _KEYWORDS):
            index += 1
        while index < len(words) and words[index] in _PREFIXES:
            index += 1
            while index < len(words) and _ASSIGNMENT.match(words[index]):
                index += 1
        if index >= len(words):
            continue
        head, rest = words[index], words[index + 1 :]
        if head in _TESTERS:
            for flag, operand in zip(rest, rest[1:], strict=False):
                expanded = _expand(operand, home)
                if flag in _TEST_FLAGS and expanded is not None:
                    guarded.add(os.path.normpath(expanded))
            continue
        if head in _SOURCES:
            if rest:
                expanded = _expand(rest[0], home)
                if expanded is not None and "/" in expanded:
                    targets.append(CommandTarget(os.path.normpath(expanded), True, run=False))
            continue
        if head in _BUILTINS:
            continue
        expanded = _expand(head, home)
        if expanded is None:
            continue
        if "/" in expanded:
            if os.path.isabs(expanded):
                targets.append(CommandTarget(os.path.normpath(expanded), True, run=True))
        else:
            targets.append(CommandTarget(expanded, False, run=True))
    return [
        CommandTarget(t.value, t.is_path, t.run, guarded=t.is_path and t.value in guarded)
        for t in targets
    ]


def _hook_commands(settings: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """``(event, command)`` for every hook command and the status line command."""
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        for event, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                inner = entry.get("hooks") if isinstance(entry, dict) else None
                for hook in inner if isinstance(inner, list) else []:
                    command = hook.get("command") if isinstance(hook, dict) else None
                    if isinstance(command, str):
                        yield str(event), command
    status_line = settings.get("statusLine")
    if isinstance(status_line, dict) and isinstance(status_line.get("command"), str):
        yield "statusLine", status_line["command"]


def _problem(target: CommandTarget, layout: Layout) -> tuple[Status, str] | None:
    if not target.is_path:
        if shutil.which(target.value) is None:
            return Status.WARN, f"{target.value} (not found on PATH)"
        return None
    path = target.value
    shown = layout.shown(Path(path))
    if os.path.exists(path):
        if target.run and not os.access(path, os.X_OK) and os.path.isfile(path):
            return Status.WARN, f"{shown} (not executable)"
        return None
    if target.guarded:
        return None
    if os.path.islink(path):
        return Status.FAIL, f"{shown} (dangling link to {os.readlink(path)})"
    return Status.FAIL, f"{shown} (missing)"


def check_hook_commands(settings_path: Path | None = None, layout: Layout | None = None) -> Check:
    """Every hook command in ``~/.claude/settings.json`` must point at something that exists.

    Read-only. A target that a ``[ -x path ] &&`` guard tests first is allowed to be missing
    (the A22 guard exists for dotfiles shared with machines that have no Fylgja).
    """
    where = Layout.current() if layout is None else layout
    path = where.home / ".claude" / "settings.json" if settings_path is None else settings_path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Check("hook-commands", Status.OK, f"no {where.shown(path)}; nothing to check")
    except (OSError, UnicodeDecodeError) as exc:
        return Check("hook-commands", Status.WARN, f"cannot read {where.shown(path)}: {exc}")
    try:
        settings = json.loads(text)
    except json.JSONDecodeError as exc:
        return Check("hook-commands", Status.FAIL, f"{where.shown(path)} is not JSON: {exc.msg}")
    if not isinstance(settings, dict):
        return Check("hook-commands", Status.FAIL, f"{where.shown(path)} is not a JSON object")
    commands = sorted(set(_hook_commands(settings)))
    problems: list[tuple[Status, str]] = []
    seen: set[str] = set()
    checked = 0
    for event, command in commands:
        for target in command_targets(command, where.home):
            checked += 1
            problem = _problem(target, where)
            if problem is None or problem[1] in seen:
                continue
            seen.add(problem[1])
            problems.append((problem[0], f"{event}: {problem[1]}"))
    if not problems:
        return Check(
            "hook-commands",
            Status.OK,
            f"{len(commands)} hook commands, {checked} targets, none missing",
        )
    status = _worst([s for s, _ in problems])
    return Check(
        "hook-commands",
        status,
        f"{len(problems)} problem(s) in {len(commands)} hook commands: "
        + _listed([text for _, text in problems]),
    )


# --------------------------------------------------------------------------------------------
# Dotfiles layout (A22)
# --------------------------------------------------------------------------------------------


def _target_problems(
    target: DotfilesTarget, listed: set[tuple[str, str]], layout: Layout
) -> list[str] | None:
    """Problems with one installed file, ``[]`` when sound, ``None`` when nothing is installed."""
    link, source = target.link, target.source
    entry = (target.entry.package, target.entry.path)
    into_dotfiles = link.is_symlink() and Path(os.path.realpath(link)).is_relative_to(
        os.path.realpath(layout.dotfiles)
    )
    if not (into_dotfiles or source.exists() or entry in listed):
        # Nothing of Fylgja's is in dotfiles. A config.toml written by hand is not managed there.
        return None
    problems: list[str] = []
    shown_link = layout.shown(link)
    if link.is_symlink():
        resolved = os.path.realpath(link)
        if resolved != os.path.realpath(source):
            problems.append(
                f"{shown_link} points to {os.readlink(link)}, not into {layout.shown(source)}"
            )
    elif link.exists():
        problems.append(
            f"{shown_link} is a regular file, not a link into dotfiles; move it to "
            f"{layout.shown(source)} and run `ln -s {source} {link}`"
        )
    else:
        problems.append(f"{shown_link} is missing; link it to {layout.shown(source)}")
    if not source.exists():
        problems.append(f"{layout.shown(source)} does not exist")
    if entry not in listed:
        problems.append(f"{target.entry} is not listed in {DOTFILES_CONF}")
    return problems


async def _uncommitted(
    touched: Sequence[Path], layout: Layout, runner: ProcessRunner, git: str
) -> list[str]:
    relative = [
        str(p.relative_to(layout.dotfiles)) for p in touched if p.is_relative_to(layout.dotfiles)
    ]
    if not relative or not (layout.dotfiles / ".git").exists():
        return []
    try:
        result = await runner.run(
            [git, "-C", str(layout.dotfiles), "status", "--porcelain", "--", *relative],
            timeout=GIT_TIMEOUT,
        )
    except (FileNotFoundError, TimeoutError):
        return []
    if result.returncode != 0:
        return []
    return [line[3:] for line in result.stdout.decode("utf-8", errors="replace").splitlines()]


async def check_dotfiles_layout(
    layout: Layout | None = None, runner: ProcessRunner | None = None, git: str = GIT
) -> list[Check]:
    """Every file Fylgja installed is a symlink into ``~/dotfiles`` and listed in
    ``dotfiles.conf``. Uncommitted Fylgja changes there are a warning (A22)."""
    where = Layout.current() if layout is None else layout
    targets = installed_targets(where)
    conf_path = where.dotfiles / DOTFILES_CONF
    try:
        listed = {(e.package, e.path) for e in parse_dotfiles_conf(conf_path.read_text("utf-8"))}
    except (OSError, UnicodeDecodeError):
        listed = set()
    checks: list[Check] = []
    installed: list[Path] = []
    for target in targets:
        problems = _target_problems(target, listed, where)
        name = f"dotfiles:{target.entry}"
        if problems is None:
            checks.append(Check(name, Status.OK, "not installed"))
        elif problems:
            checks.append(Check(name, Status.FAIL, "; ".join(problems)))
        else:
            checks.append(
                Check(name, Status.OK, f"{where.shown(target.link)} links into dotfiles, listed")
            )
            installed.append(target.source)
    if installed and runner is not None:
        dirty = await _uncommitted([*installed, conf_path], where, runner, git)
        if dirty:
            checks.append(
                Check(
                    "dotfiles:uncommitted",
                    Status.WARN,
                    f"uncommitted changes in {where.shown(where.dotfiles)}: {_listed(dirty)}; "
                    "review them and commit with `git -C ~/dotfiles commit`",
                )
            )
    return checks


# --------------------------------------------------------------------------------------------
# Hook installation (WP2.4, A2, A4, A21, Q10)
# --------------------------------------------------------------------------------------------

HookRegistration = tuple[str, dict[str, Any]]
Which = Callable[[str], str | None]


def read_settings(layout: Layout | None = None) -> dict[str, Any] | None:
    """``~/.claude/settings.json`` as an object, or ``None`` when it is missing or unusable.

    ``check_hook_commands`` already reports a file that is unreadable or not JSON.
    """
    where = Layout.current() if layout is None else layout
    try:
        settings = json.loads((where.home / ".claude" / "settings.json").read_text("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return settings if isinstance(settings, dict) else None


def fylgja_registrations(settings: Mapping[str, Any] | None) -> list[HookRegistration]:
    """``(event, hook)`` for every ``fylgja hook`` command in ``settings``."""
    if settings is None:
        return []
    return [
        (event, hook)
        for event, hook in iter_hooks(settings)
        if fylgja_hook_event(hook.get("command")) is not None
    ]


def check_hook_executable(
    registered: bool, layout: Layout | None = None, which: Which = shutil.which
) -> Check:
    """``~/.local/bin/fylgja``, which every registered hook command runs.

    Missing is a failure once hooks are registered (the guard keeps Claude quiet, but no event is
    recorded), or a warning when ``fylgja`` is on PATH somewhere else.
    """
    where = Layout.current() if layout is None else layout
    path = where.home / ".local" / "bin" / "fylgja"
    shown = where.shown(path)
    if not registered:
        return Check("hooks:executable", Status.OK, "no Fylgja hooks registered; nothing runs it")
    if os.path.exists(path):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return Check("hooks:executable", Status.OK, f"{shown} is executable")
        return Check(
            "hooks:executable", Status.FAIL, f"{shown} exists but is not an executable file"
        )
    if os.path.islink(path):
        return Check(
            "hooks:executable", Status.FAIL, f"{shown} is a dangling link to {os.readlink(path)}"
        )
    elsewhere = which("fylgja")
    if elsewhere is not None:
        return Check(
            "hooks:executable",
            Status.WARN,
            f"{shown} is missing, so the hooks do nothing; fylgja is at {elsewhere}: "
            f"`ln -s {elsewhere} {path}`",
        )
    return Check(
        "hooks:executable",
        Status.FAIL,
        f"{shown} is missing, so the registered hooks record nothing; "
        "run `uv tool install .` from the Fylgja checkout",
    )


def check_hook_async(registrations: Sequence[HookRegistration]) -> Check:
    """Every Fylgja registration must have ``async: true``, or it blocks Claude Code."""
    if not registrations:
        return Check(
            "hooks:async", Status.OK, "no Fylgja hooks registered (`fylgja install hooks --apply`)"
        )
    blocking = sorted({event for event, hook in registrations if hook.get("async") is not True})
    if blocking:
        return Check(
            "hooks:async",
            Status.FAIL,
            f"not async, so they block Claude Code: {_listed(blocking)}; "
            "run `fylgja install hooks --apply`",
        )
    events = {event for event, _ in registrations}
    return Check(
        "hooks:async",
        Status.OK,
        f"{len(registrations)} registrations over {len(events)} events, all async",
    )


async def check_claude_version(
    registrations: Sequence[HookRegistration], runner: ProcessRunner | None, claude: str = "claude"
) -> Check:
    """The installed Claude Code version, and whether it supports every registered event."""
    if not registrations:
        return Check("claude-version", Status.OK, "not checked: no Fylgja hooks registered")
    if runner is None:
        return Check("claude-version", Status.OK, "not checked: no process runner")
    try:
        version = await detect_claude_version(runner, claude)
    except InstallError as exc:
        return Check("claude-version", Status.WARN, str(exc))
    text = ".".join(map(str, version))
    events = sorted({event for event, _ in registrations})
    _, unsupported = supported_events(events, version)
    if unsupported:
        return Check(
            "claude-version",
            Status.WARN,
            f"Claude Code {text} does not support registered events: {_listed(unsupported)}",
        )
    return Check(
        "claude-version", Status.OK, f"Claude Code {text} supports all {len(events)} events"
    )


def _dir_problem(path: Path, layout: Layout) -> str | None:
    """Why the hook would refuse ``path`` (a symlink, not a directory, foreign owner, mode)."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{layout.shown(path)}: {exc.strerror or exc}"
    shown = layout.shown(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return f"{shown} is not a plain directory"
    if info.st_uid != os.getuid():
        return f"{shown} is owned by uid {info.st_uid}, not you"
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o700:
        return f"{shown} has mode {mode:04o}, expected 0700 (`chmod 700 {path}`)"
    return None


def _read_int(path: Path) -> int | None | str:
    """The decimal in ``path``; ``None`` when absent, the raw text when it is not a number."""
    try:
        text = path.read_text("utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return "unreadable"
    return int(text) if text.isdigit() else (text or "empty")


def check_inbox(
    layout: Layout | None = None, source: str = "claude", now: Callable[[], float] = time.time
) -> list[Check]:
    """The hook inbox: owner and mode, backlog age and ``dropped.count`` (all read-only)."""
    where = Layout.current() if layout is None else layout
    inbox = paths.inbox_dir(source)
    problems = [
        problem
        for problem in (_dir_problem(inbox, where), _dir_problem(paths.statusline_dir(), where))
        if problem is not None
    ]
    if problems:
        access = Check(
            "inbox:access",
            Status.FAIL,
            f"the hook refuses to write here: {_listed(problems)}",
        )
    elif inbox.exists():
        access = Check("inbox:access", Status.OK, f"{where.shown(inbox)} is yours, mode 0700")
    else:
        access = Check("inbox:access", Status.OK, "no inbox yet; the first hook event creates it")
    return [access, _check_backlog(inbox, where, now()), _check_dropped(inbox, where)]


def _read_stamp(inbox: Path) -> bytes:
    try:
        return (inbox / ".backlog-stamp").read_bytes()[:16].strip()
    except OSError:
        return b""


def _check_backlog(inbox: Path, layout: Layout, now: float) -> Check:
    try:
        with os.scandir(inbox) as entries:
            mtimes = [
                entry.stat(follow_symlinks=False).st_mtime
                for entry in entries
                if entry.name.endswith(".json") and not entry.name.startswith(".")
            ]
        stamp = _read_stamp(inbox) if mtimes else b""
    except FileNotFoundError:
        return Check("inbox:backlog", Status.OK, "no inbox yet")
    except OSError as exc:
        return Check("inbox:backlog", Status.WARN, f"cannot read the inbox: {exc.strerror or exc}")
    if stamp == b"full":
        return Check(
            "inbox:backlog",
            Status.FAIL,
            f"{len(mtimes)} events wait in {layout.shown(inbox)} and the hook drops new ones; "
            "is the daemon running (`fylgja serve`)?",
        )
    if not mtimes:
        return Check("inbox:backlog", Status.OK, "empty: the daemon keeps up")
    age = max(0.0, now - min(mtimes))
    detail = f"{len(mtimes)} events waiting, the oldest {age:.0f} s old"
    if age >= BACKLOG_FAIL_SECONDS:
        return Check(
            "inbox:backlog", Status.FAIL, f"{detail}; the daemon is not draining the inbox"
        )
    if age >= BACKLOG_WARN_SECONDS:
        return Check("inbox:backlog", Status.WARN, f"{detail}; is the daemon running?")
    return Check("inbox:backlog", Status.OK, detail)


def _check_dropped(inbox: Path, layout: Layout) -> Check:
    count = _read_int(inbox / "dropped.count")
    if count is None or count == 0:
        return Check("inbox:dropped", Status.OK, "no events dropped")
    if isinstance(count, str):
        return Check("inbox:dropped", Status.WARN, f"dropped.count is {count}, not a number")
    detail = (
        f"{count} events were dropped because the backlog was full; "
        f"delete {layout.shown(inbox / 'dropped.count')} once the daemon keeps up"
    )
    return Check("inbox:dropped", Status.FAIL if count >= DROPPED_FAIL else Status.WARN, detail)


async def check_hooks_committed(
    layout: Layout | None, runner: ProcessRunner | None, git: str = GIT
) -> Check | None:
    """Fylgja's edits to ``settings.json`` and ``statusline.sh`` are committed (a warning only).

    ``None`` when Fylgja has put nothing into either file. Read-only: ``git status --porcelain``.
    """
    where = Layout.current() if layout is None else layout
    if runner is None:
        return None
    files: list[Path] = []
    settings = where.dotfiles / SETTINGS_ENTRY.package / SETTINGS_ENTRY.path
    try:
        if fylgja_registrations(json.loads(settings.read_text("utf-8"))):
            files.append(settings)
    except (OSError, UnicodeDecodeError, ValueError, AttributeError):
        pass
    statusline = where.dotfiles / STATUSLINE_ENTRY.package / STATUSLINE_ENTRY.path
    try:
        if BLOCK_BEGIN in statusline.read_text("utf-8"):
            files.append(statusline)
    except (OSError, UnicodeDecodeError):
        pass
    if not files:
        return None
    dirty = await _uncommitted(files, where, runner, git)
    names = ", ".join(f.name for f in files)
    if dirty:
        return Check(
            "dotfiles:hooks-committed",
            Status.WARN,
            f"uncommitted changes in {_listed(dirty)}; review and commit them with "
            f"`git -C ~/dotfiles commit -- {' '.join(dirty)}` (Fylgja never commits)",
        )
    return Check("dotfiles:hooks-committed", Status.OK, f"{names} committed in dotfiles")


def check_lidskjalv_hooks(
    settings: Mapping[str, Any] | None, layout: Layout | None = None
) -> Check | None:
    """Lidskjalv's entries, when there are any: still as it registered them, target present.

    Informational (A21): it is Lidskjalv's, never Fylgja's health, so the check is neither
    counted nor able to fail doctor. ``None`` when settings hold no Lidskjalv entry.
    """
    where = Layout.current() if layout is None else layout
    if settings is None:
        return None
    entries = [
        (event, hook)
        for event, hook in iter_hooks(settings)
        if LIDSKJALV_SCRIPT in str(hook.get("command", ""))
    ]
    if not entries:
        return None
    notes: list[str] = []
    changed = sorted({e for e, hook in entries if not set(hook) <= {"type", "command", "timeout"}})
    if changed:
        notes.append(f"changed from the plain registration: {_listed(changed)}")
    missing: list[str] = []
    for event, hook in entries:
        for target in command_targets(str(hook.get("command", "")), where.home):
            problem = _problem(target, where)
            if problem is not None and f"{event}: {problem[1]}" not in missing:
                missing.append(f"{event}: {problem[1]}")
    if missing:
        notes.append(f"target problem: {_listed(missing)}")
    events = len({event for event, _ in entries})
    if notes:
        return Check(
            "lidskjalv-hooks",
            Status.WARN,
            f"{len(entries)} entries over {events} events; {'; '.join(notes)} "
            "(informational: Lidskjalv's, not Fylgja's)",
            informational=True,
        )
    return Check(
        "lidskjalv-hooks",
        Status.OK,
        f"{len(entries)} entries over {events} events, untouched, target exists (informational)",
        informational=True,
    )


# --------------------------------------------------------------------------------------------
# Local messaging (WP3.6, A9, A14, R10)
# --------------------------------------------------------------------------------------------

PS_TIMEOUT: Final = 5.0
_NEVER: Final = "doctor never sends a probe and never changes crossSessionInbound"


def _protected(home: Path) -> list[Path]:
    return [home / "Library" / "Containers", home / "Library" / "Group Containers"]


async def _ps_args(runner: ProcessRunner, pids: Sequence[int]) -> dict[int, str] | None:
    """``ps -o pid=,args=`` of ``pids``; ``None`` when ps cannot tell. The lines can hold other
    tools' secrets: only :func:`policy.parse_flags` looks at them, and nothing is shown."""
    argv = [PS, "-o", "pid=,args=", "-p", ",".join(str(pid) for pid in sorted(pids))]
    try:
        result = await runner.run(argv, timeout=PS_TIMEOUT)
    except (OSError, TimeoutError):
        return None
    if result.returncode not in (0, 1):
        return None
    return policy.parse_ps_args(result.stdout.decode("utf-8", errors="replace"))


def _messaging_summary(config: Config, seen: str) -> Check:
    """The ``messaging`` line; ``seen`` says what doctor found in the registry."""
    settings = config.actions.message
    encoder_ready = bool(FRAME_SUPPORT)
    if not settings.enabled:
        return Check(
            "messaging",
            Status.OK,
            "off ([actions.message] enabled = false): Fylgja sends no messages and "
            f"focus-and-copy is the action; {seen}; {_NEVER}",
        )
    if not encoder_ready:
        return Check(
            "messaging",
            Status.WARN,
            "on, but no message frame is supported (spike 0004: no-go), so every send reports "
            f"unsupported and focus-and-copy is the action; {seen}; {_NEVER}",
        )
    return Check(  # pragma: no cover - needs a supported frame (spike 0004 is no-go)
        "messaging",
        Status.OK,
        f"on, at most {settings.max_chars} characters per message; {seen}; {_NEVER}",
    )


def _check_registry(directory: Path, uid: int, layout: Layout) -> Check:
    """The registry directory the rows come from: yours, and writable by nobody else."""
    shown = layout.shown(directory)
    try:
        info = os.stat(directory)
    except OSError as exc:
        return Check("messaging:registry", Status.WARN, f"cannot read {shown}: {exc.strerror}")
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != uid or mode & 0o022:
        return Check(
            "messaging:registry",
            Status.WARN,
            f"{shown} is owned by uid {info.st_uid} with mode {mode:04o}; another user could "
            "plant a row, so messages may go nowhere you meant",
        )
    return Check("messaging:registry", Status.OK, f"{shown} is yours, mode {mode:04o}")


def _session_check(
    row: registry_parse.RegistryRow,
    *,
    flags: policy.ProcessFlags | None,
    managed: policy.SettingsFile,
    user: policy.SettingsFile,
    roots: Sequence[str],
    uid: int,
    home: Path,
    lstat: Callable[[str], os.stat_result],
) -> Check:
    version = parse_version(row.version)
    shown_version = row.version or "unknown version"
    protocol = "unknown" if row.peer_protocol is None else str(row.peer_protocol)
    parts = [f"Claude {shown_version}, peer protocol {protocol}"]
    status = Status.OK
    try:
        gated, peer = version_gate(row.version, row.peer_protocol)
    except Refusal as refusal:
        parts.append(f"sending unsupported ({refusal.kind}; A9 needs 2.1.224 or later)")
    else:
        if default_encoder().supports(gated, peer):  # pragma: no cover - no-go: never true
            parts.append("version supported")
        else:
            parts.append("version passes the A9 gate, but no frame is supported (spike 0004)")
    if row.messaging_socket_path is None:
        parts.append("no inbox socket")
    else:
        try:
            check_socket(
                row.messaging_socket_path,
                registry_path=row.messaging_socket_path,
                roots=roots,
                uid=uid,
                lstat=lstat,
            )
        except Refusal as refusal:
            status = Status.WARN
            parts.append(f"socket refused ({refusal.kind}, A14)")
        else:
            parts.append("socket safe (A14)")
    protected = _protected(home)
    project = Path(row.cwd) / ".claude"
    sources = policy.InboundSources(
        managed=managed,
        flag=policy.flag_settings(flags, protected, row.cwd),
        user=user,
        project=policy.read_settings_file(project / "settings.json", protected),
        local=policy.read_settings_file(project / "settings.local.json", protected),
    )
    interactive = row.kind in (None, "interactive")
    mode = policy.permission_mode(flags, sources, version=version, interactive=interactive)
    effective = policy.effective_inbound(sources, version)
    parts.append(policy.describe_inbound(effective, mode, expiry=policy.dialog_expiry(sources)))
    return Check(f"messaging:{row.pid}", status, "; ".join(parts))


async def check_messaging(
    config: Config,
    *,
    layout: Layout | None = None,
    runner: ProcessRunner | None = None,
    registry: Path | None = None,
    roots: Sequence[str] | None = None,
    uid: int | None = None,
    managed_settings: Path = policy.MANAGED_SETTINGS,
    lstat: Callable[[str], os.stat_result] = os.lstat,
) -> list[Check]:
    """The message action (WP3.6): one summary, then one line per live Claude session with its
    version compatibility (A9), its socket safety (A14) and what it will do with a message (R10).

    Read-only: the registry rows (never ``*.key``), ``ps`` for start times and the four flags of
    each session's command line, and the settings files. It sends no probe and changes nothing.
    Without a process runner, liveness cannot be proven, so no session line is shown.
    """
    where = Layout.current() if layout is None else layout
    owner = os.getuid() if uid is None else uid
    allowed = tuple(default_roots(owner) if roots is None else roots)
    directory = registry_dir(config) if registry is None else registry
    rows = read_rows(LocalFileSystem(), directory)
    if not rows:
        return [_messaging_summary(config, "no Claude session in the registry")]
    if runner is None:
        return [_messaging_summary(config, "sessions not checked (no process runner)")]
    pids = [row.pid for row in rows]
    try:
        starts = await ps_start_times(runner, PS, pids, timeout=PS_TIMEOUT)
    except PsFailure as exc:
        return [
            _messaging_summary(config, "sessions not checked"),
            Check("messaging:sessions", Status.WARN, f"liveness unknown: {exc.detail}"),
        ]
    live = [
        row
        for row in rows
        if registry_parse.check_liveness(row, starts.get(row.pid)) is registry_parse.Liveness.LIVE
        and row.proc_start is not None
    ]
    args = await _ps_args(runner, [row.pid for row in live]) if live else {}
    protected = _protected(where.home)
    managed = policy.read_settings_file(managed_settings, protected)
    user = policy.read_settings_file(where.home / ".claude" / "settings.json", protected)
    skipped = len(rows) - len(live)
    seen = f"{len(live)} live Claude session(s)"
    if skipped:
        seen += f", {skipped} stale or unprovable row(s) ignored"
    checks = [_messaging_summary(config, seen), _check_registry(directory, owner, where)]
    for row in sorted(live, key=lambda r: r.pid):
        flags = None if args is None or row.pid not in args else policy.parse_flags(args[row.pid])
        checks.append(
            _session_check(
                row,
                flags=flags,
                managed=managed,
                user=user,
                roots=allowed,
                uid=owner,
                home=where.home,
                lstat=lstat,
            )
        )
    return checks


# --------------------------------------------------------------------------------------------
# AppleScript switches (WP5.3, §4.4)
# --------------------------------------------------------------------------------------------

_NOT_AUTHORIZED: Final = "-1743"
"""osascript's error number for an Automation permission that was refused."""


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _short_ntfy_topic(argv: Sequence[str]) -> bool:
    """An element is an ``http(s)`` URL of an ntfy host whose topic is under 20 characters."""
    for element in argv:
        if not element.startswith(("http://", "https://")):
            continue
        try:
            parts = urlsplit(element)
            host = parts.hostname or ""
        except ValueError:
            continue
        topic = parts.path.rstrip("/").rsplit("/", 1)[-1]
        if "ntfy" in host and len(topic) < NTFY_TOPIC_MIN:
            return True
    return False


def _notify_command(name: str, argv: Sequence[str], is_executable: IsExecutable) -> Check:
    """One command slot: element 0 only is ever printed, because another element can be a secret."""
    if not is_executable(argv[0]):
        return Check(name, Status.FAIL, f"{argv[0]} is missing or not executable")
    if _short_ntfy_topic(argv):
        return Check(name, Status.WARN, "the ntfy topic is shorter than 20 characters")
    return Check(name, Status.OK, f"{argv[0]} is executable")


def _notify_delivery(config: Config, fetch: FetchJson) -> Check:
    try:
        pipeline = fetch("/inspect/pipeline", url=_base_url(config), timeout=client.TIMEOUT)
    except (client.DaemonDown, client.ApiError) as exc:
        return Check("notify:delivery", Status.WARN, f"cannot read /inspect/pipeline: {exc}")
    block = pipeline.get("notify") if isinstance(pipeline, dict) else None
    if not isinstance(block, dict):
        return Check("notify:delivery", Status.WARN, "cannot read /inspect/pipeline")
    kind = block.get("last_failure_kind")
    if kind:
        return Check("notify:delivery", Status.WARN, f"last delivery failed: {kind}")
    return Check("notify:delivery", Status.OK, f"{block.get('sent', 0)} sent")


def check_notify(
    config: Config,
    daemon_up: bool,
    fetch: FetchJson = client.get_json,
    is_executable: IsExecutable = _is_executable,
) -> list[Check]:
    """The ``[notify]`` commands and the daemon's last delivery failure (R18).

    Empty while alerts are off. Nothing is run, and no argv element but ``argv[0]`` is printed.
    """
    settings = config.notify
    if not settings.enabled:
        return []
    checks: list[Check] = []
    if settings.desk_command:
        checks.append(_notify_command("notify:desk", settings.desk_command, is_executable))
    if settings.away_command:
        checks.append(_notify_command("notify:away", settings.away_command, is_executable))
    if daemon_up:
        checks.append(_notify_delivery(config, fetch))
    return checks


def _record_switch(
    switch: str, *, ok: bool, now: datetime, reason: str | None, state: Path | None
) -> str:
    """Write doctor's outcome for ``switch``; the words to add to the check when that fails."""
    try:
        applescript.write_record(switch, ok=ok, at=now, reason=reason, path=state)
    except OSError as exc:
        return f"; the switch record could not be written ({exc.strerror or 'os error'})"
    return ""


async def _probe_switch(
    switch: str, runner: ProcessRunner, now: datetime, state: Path | None
) -> Check:
    name = f"applescript:{switch}"
    try:
        result = await runner.run(
            applescript.probe_argv(switch), timeout=applescript.APPLESCRIPT_TIMEOUT
        )
    except TimeoutError:
        note = _record_switch(switch, ok=False, now=now, reason="applescript_timeout", state=state)
        return Check(
            name,
            Status.WARN,
            f"degraded: osascript did not answer within {applescript.APPLESCRIPT_TIMEOUT:g} s, "
            "so a permission dialog is likely pending. The switch is disabled until doctor is "
            f"rerun (answer the dialog, then run `fylgja doctor`){note}",
        )
    except FileNotFoundError:
        return Check(name, Status.WARN, f"{applescript.OSASCRIPT} is missing: switch unusable")
    except OSError as exc:
        return Check(name, Status.WARN, f"osascript could not run ({exc.strerror or 'os error'})")
    if result.returncode != 0:
        refused = _NOT_AUTHORIZED in result.stderr.decode("utf-8", errors="replace")
        if refused:
            note = _record_switch(switch, ok=False, now=now, reason="not_authorized", state=state)
            return Check(
                name,
                Status.WARN,
                "degraded: the Automation permission was refused. The switch is disabled until "
                f"doctor is rerun after you allow it in System Settings{note}",
            )
        return Check(name, Status.WARN, f"osascript exited {result.returncode}: switch unusable")
    note = _record_switch(switch, ok=True, now=now, reason=None, state=state)
    return Check(
        name,
        Status.OK,
        f"on, and osascript answered within {applescript.APPLESCRIPT_TIMEOUT:g} s{note}",
    )


async def check_applescript(
    config: Config,
    *,
    runner: ProcessRunner | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    state: Path | None = None,
) -> list[Check]:
    """One line per AppleScript switch (``[actions.applescript]``).

    A switch that is off runs nothing. A switch that is on runs its script with a 3 s timeout: a
    timeout (a permission dialog is likely pending) or a refusal reports it degraded and records it
    as disabled until doctor is rerun; an answer records it as enabled. The scripts do not launch
    the app, and they read only the front tab's URL or the current session's path and job.
    """
    settings = config.actions.applescript
    checks: list[Check] = []
    for switch in applescript.SWITCHES:
        name = f"applescript:{switch}"
        if not getattr(settings, switch):
            checks.append(
                Check(
                    name,
                    Status.OK,
                    f"off ([actions.applescript] {switch} = false): no osascript is run",
                )
            )
        elif runner is None:
            checks.append(Check(name, Status.WARN, "on, but not checked (no process runner)"))
        else:
            checks.append(await _probe_switch(switch, runner, now(), state))
    return checks


# --------------------------------------------------------------------------------------------
# Running and rendering
# --------------------------------------------------------------------------------------------


async def run_doctor(
    *,
    runner: ProcessRunner | None = None,
    layout: Layout | None = None,
    fetch: FetchJson = client.get_json,
    probe: PortProbe = port_in_use,
    source: EntryPointSource | None = None,
    git: str = GIT,
    now: Callable[[], float] = time.time,
    which: Which = shutil.which,
) -> list[Check]:
    """Run every check. Never raises for a finding; changes only the AppleScript switch record."""
    where = Layout.current() if layout is None else layout
    config_check, config = check_config()
    checks = [config_check, check_items(config)]
    registry = load_registry(config, source)
    checks.extend(check_plugins(registry))
    checks.extend(await check_collector_doctors(registry))
    checks.append(check_fda_paths(config, where.home))
    daemon_check, daemon_up = check_daemon(config, fetch)
    checks.append(daemon_check)
    if daemon_up:
        checks.append(check_daemon_collectors(config, fetch))
    checks.append(check_port(config, daemon_up, probe))
    checks.append(check_hook_commands(layout=where))
    checks.extend(await check_dotfiles_layout(where, runner, git))
    settings = read_settings(where)
    registrations = fylgja_registrations(settings)
    checks.append(check_hook_executable(bool(registrations), where, which))
    checks.append(check_hook_async(registrations))
    checks.append(await check_claude_version(registrations, runner))
    checks.extend(check_inbox(where, now=now))
    committed = await check_hooks_committed(where, runner, git)
    if committed is not None:
        checks.append(committed)
    lidskjalv = check_lidskjalv_hooks(settings, where)
    if lidskjalv is not None:
        checks.append(lidskjalv)
    checks.extend(await check_messaging(config, layout=where, runner=runner))
    checks.extend(check_notify(config, daemon_up, fetch))
    checks.extend(await check_applescript(config, runner=runner))
    return checks


def exit_code(checks: Sequence[Check]) -> int:
    """0 when nothing failed (warnings and informational checks are allowed), else 1."""
    return 1 if any(c.status is Status.FAIL and not c.informational for c in checks) else 0


def _summary(checks: Sequence[Check]) -> dict[str, int]:
    counts = Counter(c.status.value for c in checks if not c.informational)
    return {status.value: counts.get(status.value, 0) for status in Status}


def _check_json(check: Check) -> dict[str, Any]:
    row: dict[str, Any] = {"name": check.name, "status": check.status.value, "detail": check.detail}
    if check.informational:
        row["informational"] = True
    return row


def render_json(checks: Sequence[Check]) -> str:
    return json.dumps(
        {
            "checks": [_check_json(c) for c in checks],
            "summary": _summary(checks),
        },
        indent=2,
        ensure_ascii=False,
    )


def render_checks(checks: Sequence[Check], width: int = 100) -> str:
    """A table of the checks and a one-line summary."""
    from fylgja.cli import render

    rows = [
        ["INFO" if c.informational else c.status.value.upper(), c.name, c.detail] for c in checks
    ]
    counts = _summary(checks)
    summary = f"{counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail"
    return render.table(["STATUS", "CHECK", "DETAIL"], rows, width) + "\n" + summary
