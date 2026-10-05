"""Installers: ``fylgja install config`` and ``fylgja install launchd`` (WP1.10, A22, Q10).

This module is the shared installer machinery that later steps reuse for hooks, statusline, skill,
keybinding and secret. It imports no CLI framework and runs locally: the installers are one of
the exceptions to "the CLI is an HTTP client" (A7).

The rules, from A22:

- Everything the user keeps lives in ``~/dotfiles/<package>/<path>`` and is reached through a
  symlink from ``$HOME``, named by an entry ``package:path`` in ``dotfiles.conf``.
- A regular file where a link is expected is refused, with the steps to restore the link.
- Before an existing file is edited, it is copied to ``paths.backups_dir()`` with a timestamp.
- Without ``--apply`` the installer prints a unified diff and writes nothing. With ``--apply`` it
  writes, then prints ``git -C ~/dotfiles diff --stat`` for the files it touched and a suggested
  ``git -C ~/dotfiles commit`` limited to them. It **never** commits and never stages (Q10).
- A rerun changes nothing and makes no backup.

The launchd plist is machine-specific: it goes to ``~/Library/LaunchAgents`` and never into
dotfiles. ``--apply`` then runs ``launchctl bootstrap gui/<uid> <plist>`` through the process
runner, which tests replace with :class:`~fylgja.testing.fakes.FakeProcessRunner`.
"""

from __future__ import annotations

import difflib
import os
import plistlib
import re
import shlex
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from fylgja import paths
from fylgja.collectors.seams import ProcessRunner

__all__ = [
    "CONFIG_ENTRY",
    "DOTFILES_CONF",
    "GIT",
    "LAUNCHD_LABEL",
    "ConfEntry",
    "DotfilesTarget",
    "InstallError",
    "InstallPlan",
    "Layout",
    "LinkPlan",
    "TextEdit",
    "add_conf_entry",
    "apply_plan",
    "config_template",
    "installed_targets",
    "launchd_plist_path",
    "launchd_plist_text",
    "parse_dotfiles_conf",
    "plan_config",
    "plan_launchd",
    "plan_link",
    "render_plan",
    "run_install_config",
    "run_install_launchd",
]

LAUNCHD_LABEL: Final = "io.github.erik-romson.fylgja"
DOTFILES_CONF: Final = "dotfiles.conf"
GIT: Final = "/usr/bin/git"
LAUNCHCTL: Final = "launchctl"
LAUNCHD_THROTTLE_SECONDS: Final = 10
LAUNCHCTL_TIMEOUT: Final = 30.0
GIT_TIMEOUT: Final = 15.0

BLOCK_BEGIN: Final = "# >>> fylgja >>>"
BLOCK_END: Final = "# <<< fylgja <<<"
"""Delimiters of an installed text block (A22); later installers use them."""

Echo = Callable[[str], None]


class InstallError(Exception):
    """The installer refuses or cannot continue. The message says what to do."""


# --------------------------------------------------------------------------------------------
# Layout and dotfiles.conf
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """Where ``$HOME`` and the dotfiles repository are."""

    home: Path
    dotfiles: Path

    @classmethod
    def current(cls) -> Layout:
        home = Path.home()
        return cls(home=home, dotfiles=home / "dotfiles")

    def shown(self, path: Path) -> str:
        """``path`` with the home directory written as ``~``."""
        try:
            return "~/" + path.relative_to(self.home).as_posix()
        except ValueError:
            return str(path)


@dataclass(frozen=True)
class ConfEntry:
    """One ``package:path`` line of ``dotfiles.conf``."""

    package: str
    path: str

    def __str__(self) -> str:
        return f"{self.package}:{self.path}"


_CONF_LINE: Final = re.compile(r"""^\s*["']?([A-Za-z0-9_.-]+):([^"'\s#]+)["']?\s*(?:#.*)?$""")
_CONF_CLOSE: Final = re.compile(r"^\s*\)\s*$")


def parse_dotfiles_conf(text: str) -> list[ConfEntry]:
    """The entries of ``dotfiles.conf`` (a shell array of ``"package:path"`` strings)."""
    entries: list[ConfEntry] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _CONF_LINE.match(line)
        if match is not None:
            entries.append(ConfEntry(match.group(1), match.group(2)))
    return entries


def add_conf_entry(text: str, entry: ConfEntry) -> str:
    """``text`` with ``entry`` added before the closing ``)``, unchanged when it is listed."""
    if entry in parse_dotfiles_conf(text):
        return text
    lines = text.splitlines(keepends=True)
    for index in range(len(lines) - 1, -1, -1):
        if _CONF_CLOSE.match(lines[index]):
            new = f'    "{entry}"\n'
            if index > 0 and not lines[index - 1].endswith("\n"):
                lines[index - 1] += "\n"
            return "".join([*lines[:index], new, *lines[index:]])
    raise InstallError(
        f"{DOTFILES_CONF} has no closing ')' for its DOTFILES array, so no entry was added. "
        f'Add "{entry}" by hand and run the command again.'
    )


@dataclass(frozen=True)
class DotfilesTarget:
    """A file Fylgja installs through dotfiles: ``link`` is a symlink to ``source``."""

    entry: ConfEntry
    link: Path
    source: Path


CONFIG_ENTRY: Final = ConfEntry("fylgja", ".config/fylgja/config.toml")


def installed_targets(layout: Layout) -> list[DotfilesTarget]:
    """Every dotfiles file a Fylgja installer manages (later installers add theirs here)."""
    return [
        DotfilesTarget(
            CONFIG_ENTRY,
            link=paths.config_file(),
            source=layout.dotfiles / CONFIG_ENTRY.package / CONFIG_ENTRY.path,
        )
    ]


# --------------------------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TextEdit:
    """A text file to create or rewrite. ``before`` is ``None`` for a new file."""

    path: Path
    before: str | None
    after: str
    label: str
    """Name shown in the diff (a path relative to the dotfiles repository)."""
    executable: bool = False

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def diff(self) -> str:
        old = "" if self.before is None else self.before
        lines = difflib.unified_diff(
            old.splitlines(keepends=True),
            self.after.splitlines(keepends=True),
            fromfile="/dev/null" if self.before is None else f"a/{self.label}",
            tofile=f"b/{self.label}",
        )
        return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


@dataclass(frozen=True)
class LinkPlan:
    """A symlink from ``link`` to ``target``. ``create`` is False when it is already right."""

    link: Path
    target: Path
    create: bool


@dataclass
class InstallPlan:
    name: str
    edits: list[TextEdit] = field(default_factory=list)
    links: list[LinkPlan] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)
    """Paths relative to the dotfiles repository, for ``git diff --stat`` and the commit hint."""

    @property
    def changed(self) -> bool:
        return any(e.changed for e in self.edits) or any(link.create for link in self.links)


def _same_file(a: Path, b: Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def plan_link(link: Path, target: Path, layout: Layout) -> LinkPlan:
    """Decide what it takes to make ``link`` a symlink to ``target``, or refuse.

    Refused: a regular file or directory at ``link``, a link that points somewhere else, and a
    parent directory that is itself a link (a directory link would pull the API secret that lives
    beside the config file into dotfiles).
    """
    shown, source = layout.shown(link), layout.shown(target)
    parent = link.parent
    if parent.is_symlink():
        raise InstallError(
            f"{layout.shown(parent)} is a link to a directory. Fylgja links single files only, so "
            f"the API secret beside the config never enters dotfiles. Replace the directory link "
            f"with a real directory (rm {shlex.quote(str(parent))} && mkdir "
            f"{shlex.quote(str(parent))}) and run the command again."
        )
    if link.is_symlink():
        if _same_file(link, target) and target.exists():
            return LinkPlan(link, target, create=False)
        raise InstallError(
            f"{shown} is a link to {os.readlink(link)}, not to {source}. Remove it if it is "
            f"stale (rm {shlex.quote(str(link))}) and run the command again."
        )
    if link.exists():
        kind = "a directory" if link.is_dir() else "a regular file"
        raise InstallError(
            f"{shown} is {kind}, but dotfiles expects a link to {source}. Nothing was changed. "
            f"To restore the link: keep what you need from it (mv {shlex.quote(str(link))} "
            f"{shlex.quote(str(target))} when {source} does not exist yet), then "
            f"ln -s {shlex.quote(str(target))} {shlex.quote(str(link))}."
        )
    return LinkPlan(link, target, create=True)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError(f"cannot read {path}: {exc}") from exc


def config_template() -> str:
    """The user's ``config.toml``: the packaged defaults, every setting commented out."""
    from importlib import resources

    defaults = resources.files("fylgja").joinpath("defaults.toml").read_text(encoding="utf-8")
    body = [
        line if (not line.strip() or line.lstrip().startswith("#")) else f"# {line}"
        for line in defaults.splitlines()
    ]
    header = [
        "# Fylgja configuration. Your settings are merged over the packaged defaults.",
        "# Every setting below is commented out and shows its default: uncomment a line to",
        "# override it. A list here replaces the default list. The API secret sits beside",
        "# this file (api-secret, mode 0600) and is not part of it.",
        "",
    ]
    return "\n".join([*header, *body]) + "\n"


def plan_config(layout: Layout) -> InstallPlan:
    """Plan ``install config``: the file in dotfiles, its ``dotfiles.conf`` line and the link."""
    conf_path = layout.dotfiles / DOTFILES_CONF
    conf = _read(conf_path)
    if conf is None:
        raise InstallError(
            f"{layout.shown(conf_path)} not found. Fylgja installs through a dotfiles checkout at "
            f"{layout.shown(layout.dotfiles)} with a {DOTFILES_CONF} file."
        )
    target = DotfilesTarget(
        CONFIG_ENTRY,
        link=paths.config_file(),
        source=layout.dotfiles / CONFIG_ENTRY.package / CONFIG_ENTRY.path,
    )
    if target.source.is_symlink():
        raise InstallError(f"{layout.shown(target.source)} is a link; it must be a regular file.")
    existing = _read(target.source)
    plan = InstallPlan("config")
    plan.edits.append(
        TextEdit(
            target.source,
            existing,
            config_template() if existing is None else existing,
            f"{CONFIG_ENTRY.package}/{CONFIG_ENTRY.path}",
        )
    )
    plan.edits.append(TextEdit(conf_path, conf, add_conf_entry(conf, CONFIG_ENTRY), DOTFILES_CONF))
    plan.links.append(plan_link(target.link, target.source, layout))
    plan.touched = [e.label for e in plan.edits if e.changed]
    return plan


def launchd_plist_path(layout: Layout) -> Path:
    return layout.home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def launchd_plist_text(layout: Layout) -> str:
    """The LaunchAgent plist: ``~/.local/bin/fylgja serve`` kept alive with an explicit PATH."""
    path = ":".join(
        [
            str(layout.home / ".local" / "bin"),
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
    )
    document: dict[str, object] = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(layout.home / ".local" / "bin" / "fylgja"), "serve"],
        "EnvironmentVariables": {"PATH": path},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": LAUNCHD_THROTTLE_SECONDS,
        "StandardOutPath": str(paths.state_dir() / "launchd.out.log"),
        "StandardErrorPath": str(paths.state_dir() / "launchd.err.log"),
    }
    return plistlib.dumps(document, sort_keys=True).decode("utf-8")


def plan_launchd(layout: Layout) -> InstallPlan:
    """Plan ``install launchd``: only the plist, which never goes into dotfiles."""
    plist = launchd_plist_path(layout)
    if plist.is_symlink():
        raise InstallError(f"{layout.shown(plist)} is a link; the plist must be a regular file.")
    plan = InstallPlan("launchd")
    plan.edits.append(
        TextEdit(plist, _read(plist), launchd_plist_text(layout), layout.shown(plist))
    )
    return plan


# --------------------------------------------------------------------------------------------
# Output and application
# --------------------------------------------------------------------------------------------


def render_plan(plan: InstallPlan, layout: Layout) -> str:
    """The dry-run report: a unified diff per changed file and the links to create."""
    parts: list[str] = []
    for edit in plan.edits:
        if edit.changed:
            parts.append(edit.diff().rstrip("\n"))
    for link in plan.links:
        if link.create:
            parts.append(f"link: {layout.shown(link.link)} -> {layout.shown(link.target)}")
    if not parts:
        return f"install {plan.name}: nothing to do, everything is already in place."
    return "\n".join(parts)


def _write_text(path: Path, text: str, *, executable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o755 if executable else 0o644
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def backup_file(path: Path, label: str, stamp: str) -> Path:
    """Copy ``path`` into ``paths.backups_dir()``; returns the copy."""
    directory = paths.backups_dir()
    directory.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_")
    target = directory / f"{stamp}-{slug}"
    target.write_bytes(path.read_bytes())
    os.chmod(target, 0o600)
    return target


def apply_files(plan: InstallPlan, now: datetime) -> list[Path]:
    """Write every changed edit (backing up files that exist) and create the missing links.

    Returns the backups made. Planning has already refused everything unsafe.
    """
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    backups: list[Path] = []
    for edit in plan.edits:
        if not edit.changed:
            continue
        if edit.before is not None:
            backups.append(backup_file(edit.path, edit.label, stamp))
        _write_text(edit.path, edit.after, executable=edit.executable)
    for link in plan.links:
        if link.create:
            link.link.parent.mkdir(parents=True, exist_ok=True)
            link.link.symlink_to(link.target)
    return backups


async def _git_report(
    plan: InstallPlan, layout: Layout, runner: ProcessRunner, git: str, echo: Echo
) -> None:
    """Print ``git diff --stat`` for the touched files and a suggested commit. Never commits."""
    if not plan.touched:
        return
    base = [git, "-C", str(layout.dotfiles)]
    try:
        result = await runner.run(
            [*base, "diff", "--stat", "--", *plan.touched], timeout=GIT_TIMEOUT
        )
    except (FileNotFoundError, TimeoutError):
        echo(f"(git not available: run `git -C {layout.shown(layout.dotfiles)} status` yourself)")
    else:
        stat = result.stdout.decode("utf-8", errors="replace").rstrip()
        if stat:
            echo(stat)
    files = " ".join(shlex.quote(p) for p in plan.touched)
    root = layout.shown(layout.dotfiles)
    echo(
        "Review, then commit when you are satisfied (Fylgja never commits):\n"
        f"  git -C {root} add -- {files}\n"
        f'  git -C {root} commit -m "fylgja: install {plan.name}" -- {files}'
    )


async def apply_plan(
    plan: InstallPlan,
    layout: Layout,
    runner: ProcessRunner,
    *,
    git: str = GIT,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    echo: Echo,
) -> None:
    """Apply a dotfiles plan: write, report ``git diff --stat``, suggest a commit."""
    if not plan.changed:
        echo(render_plan(plan, layout))
        return
    backups = apply_files(plan, now())
    for edit in plan.edits:
        if edit.changed:
            echo(f"wrote {layout.shown(edit.path)}")
    for link in plan.links:
        if link.create:
            echo(f"linked {layout.shown(link.link)} -> {layout.shown(link.target)}")
    for backup in backups:
        echo(f"backup: {layout.shown(backup)}")
    await _git_report(plan, layout, runner, git, echo)


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------


async def run_install_config(
    *,
    apply: bool,
    runner: ProcessRunner,
    layout: Layout | None = None,
    git: str = GIT,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    echo: Echo,
) -> int:
    """``fylgja install config [--apply]``; returns the exit code."""
    where = Layout.current() if layout is None else layout
    try:
        plan = plan_config(where)
        if not apply:
            echo(render_plan(plan, where))
            if plan.changed:
                echo("(dry run: pass --apply to write these changes)")
            return 0
        await apply_plan(plan, where, runner, git=git, now=now, echo=echo)
    except InstallError as exc:
        echo(f"fylgja: install config: {exc}")
        return 1
    return 0


def _launchd_domain(uid: int) -> str:
    return f"gui/{uid}"


async def run_install_launchd(
    *,
    apply: bool,
    runner: ProcessRunner,
    layout: Layout | None = None,
    uid: int | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    echo: Echo,
) -> int:
    """``fylgja install launchd [--apply]``; returns the exit code.

    A new or changed plist is bootstrapped directly (one ``launchctl bootstrap gui/<uid>
    <plist>``). An unchanged plist is bootstrapped only when the service is not loaded yet.
    """
    where = Layout.current() if layout is None else layout
    domain = _launchd_domain(os.getuid() if uid is None else uid)
    try:
        plan = plan_launchd(where)
    except InstallError as exc:
        echo(f"fylgja: install launchd: {exc}")
        return 1
    plist = launchd_plist_path(where)
    bootstrap = [LAUNCHCTL, "bootstrap", domain, str(plist)]
    if not apply:
        echo(render_plan(plan, where))
        echo(f"(dry run: --apply would then run `{' '.join(bootstrap)}`)")
        return 0
    if plan.changed:
        paths.state_dir().mkdir(parents=True, exist_ok=True)
        for backup in apply_files(plan, now()):
            echo(f"backup: {where.shown(backup)}")
        echo(f"wrote {where.shown(plist)}")
    else:
        try:
            loaded = await runner.run(
                [LAUNCHCTL, "print", f"{domain}/{LAUNCHD_LABEL}"], timeout=LAUNCHCTL_TIMEOUT
            )
        except (FileNotFoundError, TimeoutError):
            echo("fylgja: install launchd: launchctl is not available")
            return 1
        if loaded.returncode == 0:
            echo(f"install launchd: {LAUNCHD_LABEL} is already loaded; nothing to do.")
            return 0
    try:
        result = await runner.run(bootstrap, timeout=LAUNCHCTL_TIMEOUT)
    except (FileNotFoundError, TimeoutError) as exc:
        echo(f"fylgja: install launchd: launchctl bootstrap failed ({type(exc).__name__})")
        return 1
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        echo(
            f"fylgja: install launchd: launchctl bootstrap exited {result.returncode}"
            + (f": {detail}" if detail else "")
            + f"\nIf the agent is already loaded with an older plist, run "
            f"`launchctl bootout {domain}/{LAUNCHD_LABEL}` and then this command again."
        )
        return 1
    echo(f"bootstrapped {LAUNCHD_LABEL} in {domain}")
    return 0
