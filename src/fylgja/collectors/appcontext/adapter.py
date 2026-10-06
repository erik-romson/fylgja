"""I/O of the ``appcontext`` collector (``PLAN-final-plan.md`` WP5.3). It reads, never writes.

* **Windows and focus** come from the daemon's own store, read-only (``mode=ro`` and
  ``query_only``, :func:`fylgja.store.db.connect_reader`): the open windows and the latest focus
  event, which is what the live view holds. A collector has no other view of the live view, and the
  store has one writer: this is a reader like the API's. A store that does not exist yet has no
  windows.
* **Files** are read through the :class:`~fylgja.collectors.seams.FileSystem` seam, under the user's
  home: cmux's session store, JetBrains' ``recentProjects.xml``, Obsidian's ``obsidian.json`` and
  Chrome's ``Local State``. A missing file is "not installed" (``None``); anything else that goes
  wrong raises :class:`InputError` with a short code that never holds a path or file content.
* **AppleScript** goes through the process runner as ``osascript`` with a 3 s timeout, only for a
  switch that is on (:mod:`~fylgja.collectors.appcontext.switches` builds the command line).
* **Chrome history** (a switch of its own) is read with SQLite on the ``History`` file as
  ``file:<path>?mode=ro&immutable=1``: read-only, and Chrome's lock is never taken.

Nothing under ``~/Library/Containers`` or ``~/Library/Group Containers`` is ever opened:
:func:`check_readable` refuses such a path before any read. Teams, Slack and Outlook stores are not
read at all.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from fylgja.collectors.appcontext import parse, switches
from fylgja.collectors.seams import FileSystem, ProcessRunner
from fylgja.core.system import LocalFileSystem, SubprocessRunner
from fylgja.store import db, queries

__all__ = [
    "AppleScriptError",
    "AppleScriptSwitches",
    "AppContextInputs",
    "ChromeHistory",
    "FocusFacts",
    "HistoryReader",
    "InputError",
    "SourceError",
    "StoreWindowSource",
    "Visit",
    "WindowSnapshot",
    "WindowSource",
    "check_readable",
    "default_inputs",
    "read_input",
    "run_applescript",
]

_PROTECTED: Final = frozenset({"Containers", "Group Containers"})
_PROFILE_DIR: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.\-]*")
MAX_VISITS: Final = 50


class SourceError(Exception):
    """The windows and focus could not be read. ``kind`` is a short, safe code."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class InputError(Exception):
    """A tier-2 input could not be read or understood. ``kind`` is a short, safe code."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class AppleScriptError(Exception):
    """An osascript run failed. ``kind`` is ``timeout``, ``missing``, ``failed`` or ``os_error``."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


# --------------------------------------------------------------------------------------------
# Windows and focus
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FocusFacts:
    """The latest focus event."""

    window_id: str | None
    observed_at: datetime
    bundle_id: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class WindowSnapshot:
    windows: tuple[parse.WindowFacts, ...]
    focus: FocusFacts | None


@runtime_checkable
class WindowSource(Protocol):
    def read(self) -> WindowSnapshot:
        """The open windows and the latest focus. Raises :class:`SourceError`."""
        ...


class StoreWindowSource:
    """Windows and focus from the store, read-only."""

    def __init__(self, database: Path) -> None:
        self._database = database

    def read(self) -> WindowSnapshot:
        try:
            if not self._database.exists():
                return WindowSnapshot((), None)
            connection = db.connect_reader(self._database)
            try:
                rows = queries.load_open_windows(connection)
                focus = queries.latest_focus(connection)
            finally:
                connection.close()
        except (OSError, sqlite3.Error, KeyError, ValueError) as exc:
            raise SourceError(f"store_{type(exc).__name__}") from exc
        windows = tuple(
            parse.WindowFacts(
                window_id=row.window_id,
                bundle_id=row.app_bundle_id,
                title=row.title,
                workspace=row.workspace,
            )
            for row in rows
        )
        return WindowSnapshot(
            windows,
            None
            if focus is None
            else FocusFacts(focus.window_id, focus.observed_at, focus.app_bundle_id, focus.title),
        )


# --------------------------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------------------------


def check_readable(path: Path | str, home: Path) -> None:
    """Refuse a path under ``~/Library/Containers`` or ``~/Library/Group Containers``."""
    candidate = Path(path)
    library = home / "Library"
    try:
        relative = candidate.relative_to(library)
    except ValueError:
        return
    if relative.parts and relative.parts[0] in _PROTECTED:
        raise InputError("protected_path")


def read_input(fs: FileSystem, path: Path, home: Path) -> str | None:
    """The text of ``path``; ``None`` when it does not exist. Raises :class:`InputError`."""
    check_readable(path, home)
    try:
        return fs.read_text(path)
    except FileNotFoundError:
        return None
    except PermissionError as exc:
        raise InputError("permission_denied") from exc
    except IsADirectoryError as exc:
        raise InputError("is_a_directory") from exc
    except UnicodeDecodeError as exc:
        raise InputError("not_text") from exc
    except OSError as exc:
        raise InputError("os_error") from exc


def cmux_store(home: Path) -> Path:
    return home / ".cmuxterm" / "claude-hook-sessions.json"


def jetbrains_root(home: Path) -> Path:
    return home / "Library" / "Application Support" / "JetBrains"


def obsidian_config(home: Path) -> Path:
    return home / "Library" / "Application Support" / "obsidian" / "obsidian.json"


def chrome_root(home: Path) -> Path:
    return home / "Library" / "Application Support" / "Google" / "Chrome"


def jetbrains_recent_projects(fs: FileSystem, home: Path, prefixes: Sequence[str]) -> Path | None:
    """The ``recentProjects.xml`` of the newest product directory that matches ``prefixes``.

    Newest is the most recently modified file, with the name as the tie-break (``…2026.1`` before
    ``…2025.2``). ``None`` when the IDE has no product directory with the file.
    """
    root = jetbrains_root(home)
    check_readable(root, home)
    try:
        names = fs.listdir(root)
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return None
    except OSError as exc:
        raise InputError("os_error") from exc
    best: tuple[float, str, Path] | None = None
    for name in names:
        if not name.startswith(tuple(prefixes)):
            continue
        candidate = root / name / "options" / "recentProjects.xml"
        try:
            modified = fs.stat(candidate).mtime
        except OSError:
            continue
        if best is None or (modified, name) > (best[0], best[1]):
            best = (modified, name, candidate)
    return None if best is None else best[2]


# --------------------------------------------------------------------------------------------
# AppleScript
# --------------------------------------------------------------------------------------------


async def run_applescript(runner: ProcessRunner, switch: str) -> str:
    """Run the script of ``switch`` and return its output (without the final newline).

    The caller has checked that the switch is on. Raises :class:`AppleScriptError`.
    """
    try:
        result = await runner.run(
            switches.osascript_argv(switch), timeout=switches.APPLESCRIPT_TIMEOUT
        )
    except TimeoutError as exc:
        raise AppleScriptError("timeout") from exc
    except FileNotFoundError as exc:
        raise AppleScriptError("missing") from exc
    except OSError as exc:
        raise AppleScriptError("os_error") from exc
    if result.returncode != 0:
        raise AppleScriptError("failed")
    return result.stdout.decode("utf-8", errors="replace").rstrip("\n")


@dataclass(frozen=True)
class AppleScriptSwitches:
    """``[actions.applescript]``: both off unless the user turned them on."""

    chrome: bool = False
    iterm2: bool = False

    def enabled(self, switch: str) -> bool:
        return {switches.CHROME: self.chrome, switches.ITERM2: self.iterm2}.get(switch, False)


def configured_switches() -> AppleScriptSwitches:
    """The switches of the user's configuration; both off when it cannot be loaded."""
    from fylgja.config import ConfigError, load_config

    try:
        actions = load_config().actions.applescript
    except ConfigError:
        return AppleScriptSwitches()
    return AppleScriptSwitches(chrome=actions.chrome, iterm2=actions.iterm2)


# --------------------------------------------------------------------------------------------
# Chrome history
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Visit:
    url: str
    title: str | None
    at: datetime


@runtime_checkable
class HistoryReader(Protocol):
    def recent(self, profile_dir: str, since: datetime, limit: int) -> list[Visit]:
        """The most recent visits after ``since``, newest first. Raises :class:`InputError`."""
        ...


def history_uri(history_file: Path) -> str:
    """``file:<path>?mode=ro&immutable=1``: read-only, and no lock on Chrome's file."""
    return f"{history_file.resolve().as_uri()}?mode=ro&immutable=1"


class ChromeHistory:
    """Chrome's ``History`` database, opened read-only and immutable."""

    def __init__(self, home: Path) -> None:
        self._home = home

    def recent(self, profile_dir: str, since: datetime, limit: int) -> list[Visit]:
        if _PROFILE_DIR.fullmatch(profile_dir) is None or profile_dir.startswith(".."):
            raise InputError("bad_profile_directory")
        history_file = chrome_root(self._home) / profile_dir / "History"
        check_readable(history_file, self._home)
        if not history_file.is_file():
            return []
        try:
            connection = sqlite3.connect(history_uri(history_file), uri=True, autocommit=True)
        except sqlite3.Error as exc:
            raise InputError("history_open_failed") from exc
        try:
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                "SELECT url, title, last_visit_time FROM urls "
                "WHERE last_visit_time > ? ORDER BY last_visit_time DESC LIMIT ?",
                (parse.chrome_time_from_unix(since), min(limit, MAX_VISITS)),
            ).fetchall()
        except sqlite3.Error as exc:
            raise InputError("history_read_failed") from exc
        finally:
            connection.close()
        visits: list[Visit] = []
        for url, title, stamp in rows:
            if isinstance(url, str) and isinstance(stamp, int):
                visits.append(
                    Visit(
                        url,
                        title if isinstance(title, str) and title else None,
                        parse.unix_from_chrome_time(stamp),
                    )
                )
        return visits


# --------------------------------------------------------------------------------------------
# Everything the collector reads through
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AppContextInputs:
    source: WindowSource
    runner: ProcessRunner
    fs: FileSystem
    home: Path
    history: HistoryReader
    switches: AppleScriptSwitches = AppleScriptSwitches()


def default_inputs(database: Path) -> AppContextInputs:
    """The real inputs. The plugin looks this up at call time so tests can replace it."""
    home = Path.home()
    return AppContextInputs(
        source=StoreWindowSource(database),
        runner=SubprocessRunner(),
        fs=LocalFileSystem(),
        home=home,
        history=ChromeHistory(home),
        switches=configured_switches(),
    )
