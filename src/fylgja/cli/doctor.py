"""``fylgja doctor``: local diagnostics (WP1.10, A22, R1, ``PLAN-final-plan.md`` §4.4).

Runs in the CLI process (an A7 exception) and, when the daemon is up, also reads ``/collectors``.
Doctor changes nothing: it makes no request except ``GET``, writes no file and starts nothing. It
imports a plugin only under the registry's rules (a built-in, or an external with an explicit
``enabled = true``).

Each check is ``ok``, ``warn`` or ``fail``. Doctor exits 0 when nothing fails (warnings are
allowed), else 1.

The checks, in order: configuration, plugins, the side-effect-free ``doctor()`` of each enabled
collector, the FDA-path guard, daemon reachability, the daemon's view of the collectors, the port,
dangling hook commands in ``~/.claude/settings.json`` (R1), and the A22 dotfiles layout.
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
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from fylgja import paths
from fylgja.cli import client
from fylgja.cli.install import (
    DOTFILES_CONF,
    GIT,
    DotfilesTarget,
    Layout,
    installed_targets,
    parse_dotfiles_conf,
)
from fylgja.collectors.base import CollectorPlugin, RecordSink
from fylgja.collectors.context import build_context
from fylgja.collectors.registry import EntryPointSource, RegisteredCollector, load_registry
from fylgja.collectors.seams import ProcessRunner
from fylgja.config import Config, ConfigError, load_config, parse_config
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
    "check_hook_commands",
    "check_plugins",
    "check_port",
    "command_targets",
    "exit_code",
    "render_checks",
    "render_json",
    "run_doctor",
]

COLLECTOR_DOCTOR_TIMEOUT: Final = 10.0
GIT_TIMEOUT: Final = 15.0
MAX_LISTED: Final = 5


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


FetchJson = Callable[..., Any]
PortProbe = Callable[[int], bool]


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
) -> list[Check]:
    """Run every check. Never raises for a finding; never changes anything."""
    where = Layout.current() if layout is None else layout
    config_check, config = check_config()
    checks = [config_check]
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
    return checks


def exit_code(checks: Sequence[Check]) -> int:
    """0 when nothing failed (warnings are allowed), else 1."""
    return 1 if any(c.status is Status.FAIL for c in checks) else 0


def _summary(checks: Sequence[Check]) -> dict[str, int]:
    counts = Counter(c.status.value for c in checks)
    return {status.value: counts.get(status.value, 0) for status in Status}


def render_json(checks: Sequence[Check]) -> str:
    return json.dumps(
        {
            "checks": [
                {"name": c.name, "status": c.status.value, "detail": c.detail} for c in checks
            ],
            "summary": _summary(checks),
        },
        indent=2,
        ensure_ascii=False,
    )


def render_checks(checks: Sequence[Check], width: int = 100) -> str:
    """A table of the checks and a one-line summary."""
    from fylgja.cli import render

    rows = [[c.status.value.upper(), c.name, c.detail] for c in checks]
    counts = _summary(checks)
    summary = f"{counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail"
    return render.table(["STATUS", "CHECK", "DETAIL"], rows, width) + "\n" + summary
