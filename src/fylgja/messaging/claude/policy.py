"""What a Claude session will do with a Fylgja message: the effective inbound policy (WP3.6, R10).

Doctor explains this per live session and never tests it: it sends no probe and never changes
``crossSessionInbound``. The rules are Anthropic's (settings reference, ``crossSessionInbound`` and
``dialogExpiry``; cross-session messaging, "Control inbound messages"; checked 2026-10-06):

* **Trusted sources** are read in order (managed settings, the ``--settings`` flag, user settings)
  and the first value found applies.
* **Project and local** values apply only when stricter than that value (``refuse`` > ``hold`` >
  ``accept``). When no trusted source sets a value, a project or local ``hold`` or ``refuse`` still
  applies; their ``accept`` does not.
* **An unrecognized value** (Claude Code 2.1.248 and later) in managed settings counts as
  ``refuse``; in any other file it holds every message, though a ``refuse`` set elsewhere still
  applies. Earlier versions ignore it.
* **When no value applies**, the per-message default decides from the permission-mode classes. A
  Fylgja message claims no class (it sends no auth line), so a receiver that bypasses permission
  prompts holds it behind an approval dialog that drops it after ``dialogExpiry`` (default
  ``5m``), and a receiver that prompts delivers it. Plan mode counts as bypassing in an
  interactive session with bypass permissions available.

What doctor cannot see is said, not guessed: server-managed or MDM policy, a settings file it
cannot read, a permission mode switched inside the session, and the per-session
``CLAUDE_CODE_USER_DIALOG_TIMEOUT_MS`` (another process's environment is never read).

The ``--settings`` value and the permission flags come from the session's ``ps -o args=`` line,
which can hold secrets of other tools; :func:`parse_flags` extracts four flags and nothing from it
is ever shown, logged or stored.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from fylgja.messaging.claude.encoders import Version

__all__ = [
    "BYPASS_MODES_FROM_PROJECT_UNTIL",
    "DEFAULT_DIALOG_EXPIRY",
    "MANAGED_SETTINGS",
    "UNREADABLE",
    "UNRECOGNIZED_SINCE",
    "Effective",
    "Inbound",
    "InboundSources",
    "PermissionClass",
    "PermissionMode",
    "ProcessFlags",
    "Source",
    "Unreadable",
    "describe_inbound",
    "dialog_expiry",
    "effective_inbound",
    "flag_settings",
    "parse_flags",
    "parse_ps_args",
    "permission_class",
    "permission_mode",
    "read_settings_file",
    "setting",
]

MANAGED_SETTINGS: Final = Path("/Library/Application Support/ClaudeCode/managed-settings.json")
"""The managed-settings file on macOS (server-managed and MDM policy are not files doctor reads)."""
DEFAULT_DIALOG_EXPIRY: Final = "5m"
UNRECOGNIZED_SINCE: Final[Version] = (2, 1, 248)
"""Before this version Claude Code ignored an unrecognized ``crossSessionInbound`` value."""
BYPASS_MODES_FROM_PROJECT_UNTIL: Final[Version] = (2, 1, 257)
"""From this version ``auto`` and ``bypassPermissions`` do not apply from project or local files."""
MAX_SETTINGS_BYTES: Final = 1 << 20

_PROMPTING_MODES: Final = frozenset({"default", "acceptEdits", "auto", "dontAsk"})
_EXPIRY_TEXT: Final = {"60s": "60 s", "5m": "5 minutes", "10m": "10 minutes"}


class Inbound(StrEnum):
    ACCEPT = "accept"
    HOLD = "hold"
    REFUSE = "refuse"


_RANK: Final = {Inbound.ACCEPT: 0, Inbound.HOLD: 1, Inbound.REFUSE: 2}


class Source(StrEnum):
    MANAGED = "managed settings"
    FLAG = "--settings"
    USER = "user settings"
    PROJECT = "project settings"
    LOCAL = "local settings"


_TRUSTED: Final = (Source.MANAGED, Source.FLAG, Source.USER)
_REPOSITORY: Final = (Source.PROJECT, Source.LOCAL)


class Unreadable:
    """A settings source that exists but could not be read or parsed."""

    def __repr__(self) -> str:
        return "UNREADABLE"


UNREADABLE: Final = Unreadable()

SettingsFile = Mapping[str, Any] | Unreadable | None
"""A parsed settings file, :data:`UNREADABLE`, or ``None`` when there is none."""


def setting(settings: SettingsFile, *keys: str) -> object:
    """The value at ``keys`` in ``settings``; ``None`` when absent or unreadable."""
    node: object = settings
    for key in keys:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def read_settings_file(path: Path, protected: Iterable[Path] = ()) -> SettingsFile:
    """A settings JSON file. Never follows into ``protected`` folders (doctor passes the
    Full-Disk-Access roots), never reads more than 1 MiB."""
    lexical = os.path.normpath(os.path.expanduser(path))
    roots = [str(root) for root in protected]
    if any(lexical == root or lexical.startswith(root + os.sep) for root in roots):
        return UNREADABLE
    try:
        info = os.stat(lexical)
    except FileNotFoundError:
        return None
    except OSError:
        return UNREADABLE
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SETTINGS_BYTES:
        return UNREADABLE
    try:
        with open(lexical, "rb") as handle:  # noqa: PTH123
            data = json.loads(handle.read(MAX_SETTINGS_BYTES))
    except (OSError, ValueError, RecursionError):
        return UNREADABLE
    return data if isinstance(data, dict) else UNREADABLE


# --------------------------------------------------------------------------------------------
# crossSessionInbound
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundSources:
    """Each source's settings file (``None``: none; :data:`UNREADABLE`: could not be read)."""

    managed: SettingsFile = None
    flag: SettingsFile = None
    user: SettingsFile = None
    project: SettingsFile = None
    local: SettingsFile = None

    def of(self, source: Source) -> SettingsFile:
        return {
            Source.MANAGED: self.managed,
            Source.FLAG: self.flag,
            Source.USER: self.user,
            Source.PROJECT: self.project,
            Source.LOCAL: self.local,
        }[source]

    @property
    def unreadable(self) -> tuple[Source, ...]:
        return tuple(s for s in Source if isinstance(self.of(s), Unreadable))


@dataclass(frozen=True)
class Effective:
    """The ``crossSessionInbound`` value that applies, if any, and where it comes from."""

    value: Inbound | None
    """``None``: no value applies, so the per-message default decides."""
    source: Source | None = None
    unrecognized: bool = False
    """The value comes from an unrecognized setting (a hold, or a managed refuse)."""
    unreadable: tuple[Source, ...] = ()


class _Unrecognized:
    """A ``crossSessionInbound`` value that is set but is not ``accept``, ``hold`` or ``refuse``."""


_UNRECOGNIZED: Final = _Unrecognized()
_VALUES: Final = frozenset(member.value for member in Inbound)


def _parse(raw: object) -> Inbound | _Unrecognized | None:
    """A recognized value, ``None`` when unset, or :data:`_UNRECOGNIZED` for anything else."""
    if raw is None:
        return None
    if isinstance(raw, str) and raw in _VALUES:
        return Inbound(raw)
    return _UNRECOGNIZED


def effective_inbound(sources: InboundSources, version: Version | None = None) -> Effective:
    """Apply the precedence rules (module docstring) to ``sources``."""
    honor_unrecognized = version is None or version >= UNRECOGNIZED_SINCE
    best: tuple[Inbound, Source, bool] | None = None

    def consider(value: Inbound, source: Source, unrecognized: bool = False) -> None:
        nonlocal best
        if best is None or _RANK[value] > _RANK[best[0]]:
            best = (value, source, unrecognized)

    def value_of(source: Source) -> Inbound | _Unrecognized | None:
        return _parse(setting(sources.of(source), "crossSessionInbound"))

    trusted: Inbound | None = None
    for source in _TRUSTED:
        value = value_of(source)
        if value is None:
            continue
        if isinstance(value, _Unrecognized):
            if honor_unrecognized and source is Source.MANAGED:
                consider(Inbound.REFUSE, source, True)
                trusted = Inbound.REFUSE
                break
            continue  # elsewhere it is no value, but it holds (below)
        consider(value, source)
        trusted = value
        break
    for source in _REPOSITORY:
        value = value_of(source)
        if isinstance(value, Inbound) and (trusted is not None or value is not Inbound.ACCEPT):
            consider(value, source)
    if honor_unrecognized:
        for source in (Source.FLAG, Source.USER, *_REPOSITORY):
            if isinstance(value_of(source), _Unrecognized):
                consider(Inbound.HOLD, source, True)
    if best is None:
        return Effective(None, unreadable=sources.unreadable)
    return Effective(best[0], best[1], best[2], sources.unreadable)


# --------------------------------------------------------------------------------------------
# Permission mode
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessFlags:
    """The four flags doctor takes from a session's command line (nothing else is kept)."""

    skip_permissions: bool = False
    allow_skip_permissions: bool = False
    permission_mode: str | None = None
    settings: str | None = None
    """The ``--settings`` value: a file path or inline JSON. Read, never shown."""
    print_mode: bool = False


def parse_ps_args(text: str) -> dict[int, str]:
    """``ps -o pid=,args=`` output as ``{pid: args}``."""
    found: dict[int, str] = {}
    for line in text.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():
            found[int(pid)] = args.strip()
    return found


def _settings_value(args: str, start: int) -> str:
    """The ``--settings`` value starting at ``start``: inline JSON (which may hold spaces) or one
    word."""
    rest = args[start:].lstrip()
    if rest.startswith("{"):
        try:
            _, end = json.JSONDecoder().raw_decode(rest)
        except ValueError:
            return rest.split(" ", 1)[0]
        return rest[:end]
    return rest.split(" ", 1)[0]


def parse_flags(args: str) -> ProcessFlags:
    """The permission flags, ``--settings`` and ``-p`` in a ``ps -o args=`` line (best effort:
    ``ps`` joins the arguments with spaces)."""
    skip = allow = print_mode = False
    mode: str | None = None
    settings: str | None = None
    words = args.split()
    for index, word in enumerate(words):
        following = words[index + 1] if index + 1 < len(words) else None
        if word == "--dangerously-skip-permissions":
            skip = True
        elif word == "--allow-dangerously-skip-permissions":
            allow = True
        elif word in ("-p", "--print"):
            print_mode = True
        elif word == "--permission-mode" and following is not None:
            mode = following
        elif word.startswith("--permission-mode="):
            mode = word.partition("=")[2] or None
        elif word == "--settings" and settings is None:
            position = args.find("--settings ")
            settings = _settings_value(args, position + len("--settings ")) or None
        elif word.startswith("--settings=") and settings is None:
            position = args.find("--settings=")
            settings = _settings_value(args, position + len("--settings=")) or None
    return ProcessFlags(skip, allow, mode, settings, print_mode)


def flag_settings(
    flags: ProcessFlags | None, protected: Iterable[Path] = (), cwd: str | None = None
) -> SettingsFile:
    """The settings the ``--settings`` flag gives: inline JSON, or the file it names."""
    if flags is None or flags.settings is None:
        return None
    value = flags.settings
    if value.startswith("{"):
        try:
            data = json.loads(value)
        except ValueError:
            return UNREADABLE
        return data if isinstance(data, dict) else UNREADABLE
    path = Path(value).expanduser()
    if not path.is_absolute():
        if cwd is None:
            return UNREADABLE
        path = Path(cwd) / path
    found = read_settings_file(path, protected)
    return UNREADABLE if found is None else found


class PermissionClass(StrEnum):
    BYPASS = "bypass"
    PROMPTING = "prompting"


@dataclass(frozen=True)
class PermissionMode:
    """The mode a session started in, as far as doctor can tell."""

    mode: str | None
    """``None``: it could not be determined."""
    source: str
    """Where it comes from, for the explanation."""
    bypass_available: bool = False
    interactive: bool = True


def permission_mode(
    flags: ProcessFlags | None,
    sources: InboundSources,
    *,
    version: Version | None = None,
    interactive: bool = True,
) -> PermissionMode:
    """The start mode: the command-line flags, else ``permissions.defaultMode`` (managed,
    ``--settings``, local, project, user), else ``default``."""
    if flags is None:
        return PermissionMode(None, "its command line could not be read", interactive=interactive)
    available = flags.skip_permissions or flags.allow_skip_permissions
    live = interactive and not flags.print_mode
    if flags.skip_permissions:
        return PermissionMode("bypassPermissions", "--dangerously-skip-permissions", True, live)
    if flags.permission_mode is not None:
        return PermissionMode(flags.permission_mode, "--permission-mode", available, live)
    late = version is None or version >= BYPASS_MODES_FROM_PROJECT_UNTIL
    for source in (Source.MANAGED, Source.FLAG, Source.LOCAL, Source.PROJECT, Source.USER):
        mode = setting(sources.of(source), "permissions", "defaultMode")
        if not isinstance(mode, str) or not mode:
            continue
        if source in _REPOSITORY and late and mode in ("auto", "bypassPermissions"):
            continue
        return PermissionMode(mode, f"permissions.defaultMode in {source}", available, live)
    return PermissionMode("default", "no mode flag or defaultMode", available, live)


def permission_class(mode: PermissionMode) -> PermissionClass | None:
    """The class the inbound default uses; ``None`` when the mode is unknown."""
    if mode.mode == "bypassPermissions":
        return PermissionClass.BYPASS
    if mode.mode == "plan":
        bypassing = mode.bypass_available and mode.interactive
        return PermissionClass.BYPASS if bypassing else PermissionClass.PROMPTING
    if mode.mode in _PROMPTING_MODES:
        return PermissionClass.PROMPTING
    return None


def dialog_expiry(sources: InboundSources) -> str:
    """``dialogExpiry`` from managed, then user settings (its only scopes); default ``5m``."""
    for source in (Source.MANAGED, Source.USER):
        value = setting(sources.of(source), "dialogExpiry")
        if isinstance(value, str) and (value in _EXPIRY_TEXT or value == "never"):
            return value
    return DEFAULT_DIALOG_EXPIRY


# --------------------------------------------------------------------------------------------
# The sentence
# --------------------------------------------------------------------------------------------


def _from(effective: Effective) -> str:
    assert effective.source is not None
    assert effective.value is not None
    if effective.unrecognized:
        return f"an unrecognized crossSessionInbound value in {effective.source}"
    return f"crossSessionInbound = {effective.value.value} in {effective.source}"


def describe_inbound(effective: Effective, mode: PermissionMode, *, expiry: str) -> str:
    """One plain sentence: what this session will do with a Fylgja message."""
    if effective.value is Inbound.REFUSE:
        text = (
            "messages to this session will be dropped without delivery, and Fylgja is not told "
            f"({_from(effective)})"
        )
    elif effective.value is Inbound.HOLD:
        text = (
            "messages to this session will be held with a notice and not delivered until an "
            f"accept applies ({_from(effective)})"
        )
    elif effective.value is Inbound.ACCEPT:
        text = f"messages to this session will be delivered to Claude ({_from(effective)})"
    else:
        text = _describe_default(mode, expiry)
    if effective.unreadable:
        names = ", ".join(str(source) for source in effective.unreadable)
        text += f"; doctor could not read {names}, which may change this"
    return text


def _describe_default(mode: PermissionMode, expiry: str) -> str:
    klass = permission_class(mode)
    why = "no crossSessionInbound value applies"
    if klass is None:
        return (
            "messages to this session will be delivered if it prompts for permissions, or held "
            f"for approval if it bypasses them: {why} and its permission mode is unknown "
            f"({mode.source})"
        )
    if klass is PermissionClass.PROMPTING:
        return (
            f"messages to this session will be delivered: {why} and it prompts for permissions "
            f"({mode.mode}, from {mode.source}); a Fylgja message claims no permission class"
        )
    reason = f"{why} and it bypasses permission prompts ({mode.mode}, from {mode.source})"
    if expiry == "never":
        until = "the approval dialog does not expire"
    else:
        until = f"an unanswered approval dialog drops the message after {_EXPIRY_TEXT[expiry]}"
    if not mode.interactive:
        if expiry == "never":
            until = "it keeps them until the session ends"
        else:
            until = f"it keeps each for {_EXPIRY_TEXT[expiry]} and then drops it"
        return (
            f"messages to this session will be held for approval: {reason}; it cannot show the "
            f"dialog, so {until} unless its mode or settings change"
        )
    return f"messages to this session will be held for approval: {reason}; {until}"
