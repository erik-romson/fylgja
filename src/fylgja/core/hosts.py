"""Host adapters: which window of which app hosts a session (``PLAN-final-plan.md`` WP1.8, A1, A3).

The resolver walks a session's ancestor chain until a pid owns an AeroSpace window; that window's
app is the *host*. An adapter then narrows the host's windows down to one window where the app
gives it a way to, and to the exact cmux workspace and surface where cmux does. Every join says
how it was made (``via``) and how far to trust it (``confidence``), and a join that failed says
why (``unresolved``).

* :class:`CmuxHost` (primary; decision 0025). The cmux CLI is refused outside a cmux terminal, so
  it is not used for the join. The order is: the hook-time ``CMUX_*`` ids of the envelope (A3,
  ``cmux-hook-env``), then cmux's own session store matched on session id and pid
  (``cmux-session-store``), then the window level of every host. A cmux **workspace** is called a
  workspace here and a "tab" by cmux's AppleScript dictionary; the AeroSpace workspace is a
  different thing and is always called that.
* :class:`JetBrainsHost`: the opened project whose path contains the cwd (longest path wins), then
  the window whose title names it.
* :class:`Iterm2Host`: best effort, by window title.
* :class:`GenericHost`: any other app, at window level.

The joins are pure functions over :class:`~fylgja.store.rows.WindowRow` records and the host data
that :func:`load_host_data` read through the injected seams: only ``cmux --version`` (a listing
command) and file reads happen there, never a focusing command.

Window titles and session names are compared and never logged.
"""

from __future__ import annotations

import json
import logging
import os
import plistlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Protocol

from fylgja.collectors.seams import FileSystem, ProcessRunner
from fylgja.store.rows import WindowRow

__all__ = [
    "CMUX_BUNDLE_ID",
    "CMUX_CLI",
    "TESTED_CMUX_VERSIONS",
    "CmuxData",
    "CmuxHost",
    "CmuxIds",
    "CmuxRecord",
    "CmuxSource",
    "CmuxStatus",
    "CmuxStore",
    "GenericHost",
    "HostAdapter",
    "HostData",
    "HostSession",
    "Iterm2Host",
    "JetBrainsHost",
    "JetBrainsProject",
    "Join",
    "adapter_for",
    "default_adapters",
    "host_kind",
    "host_loader",
    "jetbrains_stem_for",
    "load_host_data",
    "load_jetbrains",
    "newest_product_dirs",
    "parse_cmux_store",
    "parse_cmux_version",
    "parse_recent_projects",
    "path_contains",
    "title_project",
    "version_is_tested",
    "window_match",
]

logger = logging.getLogger("fylgja.hosts")

CMUX_BUNDLE_ID: Final = "com.cmuxterm.app"
CMUX_CLI: Final = "/Applications/cmux.app/Contents/Resources/bin/cmux"
CMUX_INFO_PLIST: Final = "/Applications/cmux.app/Contents/Info.plist"
CMUX_STORE_PARTS: Final = (".cmuxterm", "claude-hook-sessions.json")
TESTED_CMUX_VERSIONS: Final = ("0.64",)
"""``[collectors.cmux] tested`` default: dotted version prefixes."""
CMUX_STORE_VERSION: Final = 1
MAX_STORE_BYTES: Final = 8 << 20
"""A store of a few dozen sessions is tens of kilobytes; a bigger file is not read."""

# Join confidence, best first. ``window-only`` is exact for the window and says nothing finer.
HIGH: Final = "high"
MEDIUM: Final = "medium"
LOW: Final = "low"
WINDOW_ONLY: Final = "window-only"
NONE: Final = "none"

VIA_CMUX_HOOK: Final = "cmux-hook-env"
VIA_CMUX_STORE: Final = "cmux-session-store"
VIA_CHAIN: Final = "parent-chain"
VIA_PROJECT_PATH: Final = "parent-chain+project-path"
VIA_TITLE: Final = "parent-chain+title"

JETBRAINS_BUNDLE_PREFIX: Final = "com.jetbrains."
JETBRAINS_PRODUCT: Final = {"intellij": "intellijidea"}
"""Bundle-id suffix to product directory stem, where they differ."""
_TITLE_SPLIT: Final = re.compile(r"\s[–—-]\s")
_TITLE_PATH: Final = re.compile(r"^(?P<name>.*?) \[(?P<path>[^\]]+)\]$")
_CMUX_VERSION: Final = re.compile(r"^cmux\s+(\d+(?:\.\d+)*)(?:\s+\(([^)]*)\))?")
MAX_IDE_NAME: Final = 200


# --------------------------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CmuxIds:
    """The allowlisted ``CMUX_*`` ids of a hook envelope (A3). Exact when present."""

    workspace_id: str | None = None
    surface_id: str | None = None


@dataclass(frozen=True)
class HostSession:
    """What an adapter needs to know about one session."""

    session_id: str
    pid: int | None
    cwd: str
    name: str | None = None
    """The session's title: the ``ai-title`` or the registry name."""
    cmux_ids: CmuxIds | None = None


@dataclass(frozen=True)
class Join:
    """The result of one host join. ``unresolved`` says why no window was found."""

    host: str
    via: str
    confidence: str
    window_id: str | None = None
    workspace: str | None = None
    """The AeroSpace workspace of the window (or of all candidate windows)."""
    host_ref: str | None = None
    """cmux: ``<cmux workspace id>/<surface id>``. JetBrains: the IDE project path."""
    cmux_workspace_id: str | None = None
    cmux_surface_id: str | None = None
    host_project: str | None = None
    """JetBrains: the opened project that contains the cwd."""
    note: str = ""
    unresolved: str | None = None


def _unresolved(host: str, reason: str, **extra: Any) -> Join:
    return Join(host=host, via=NONE, confidence=NONE, unresolved=reason, **extra)


def _window_level(
    host: str,
    windows: Sequence[WindowRow],
    *,
    via: str,
    single: str,
    what: str,
    **extra: Any,
) -> Join:
    """One window is exact. Several windows on one AeroSpace workspace fix only the workspace."""
    if len(windows) == 1:
        window = windows[0]
        return Join(
            host=host,
            via=via,
            confidence=single,
            window_id=window.window_id,
            workspace=window.workspace,
            **extra,
        )
    return _workspace_level(
        host, windows, via=via, note=f"{len(windows)} {what}; window ambiguous", **extra
    )


def _workspace_level(
    host: str, windows: Sequence[WindowRow], *, via: str, note: str, **extra: Any
) -> Join:
    spaces = {window.workspace for window in windows}
    if len(spaces) == 1 and None not in spaces:
        return Join(
            host=host,
            via=via,
            confidence=LOW,
            workspace=next(iter(spaces)),
            note=note,
            **extra,
        )
    return _unresolved(host, f"{note}, on different AeroSpace workspaces", **extra)


# --------------------------------------------------------------------------------------------
# Adapter protocol
# --------------------------------------------------------------------------------------------


class HostAdapter(Protocol):
    """Joins a session to a window of one kind of host app."""

    kind: str

    def handles(self, bundle_id: str) -> bool: ...

    def join(self, session: HostSession, windows: Sequence[WindowRow]) -> Join:
        """``windows`` are all the windows of the host process (never empty)."""
        ...


def host_kind(bundle_id: str) -> str:
    """``cmux``, ``jetbrains``, ``iterm2`` or ``other`` for an app's bundle id."""
    if bundle_id == CMUX_BUNDLE_ID:
        return "cmux"
    if bundle_id.startswith(JETBRAINS_BUNDLE_PREFIX):
        return "jetbrains"
    if bundle_id == "com.googlecode.iterm2":
        return "iterm2"
    return "other"


class GenericHost:
    """Any other app: the window when it has one, else the AeroSpace workspace they share."""

    kind = "other"

    def handles(self, bundle_id: str) -> bool:
        return True

    def join(self, session: HostSession, windows: Sequence[WindowRow]) -> Join:
        return _window_level(
            self.kind, windows, via=VIA_CHAIN, single=WINDOW_ONLY, what="windows of the host app"
        )


# --------------------------------------------------------------------------------------------
# cmux
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CmuxRecord:
    """The allowlisted fields of one session in cmux's store. Nothing else is kept."""

    session_id: str
    workspace_id: str
    surface_id: str
    pid: int | None = None
    cwd: str | None = None
    lifecycle: str | None = None
    pid_exists: bool | None = None


@dataclass(frozen=True)
class CmuxStore:
    records: tuple[CmuxRecord, ...] = ()


def _first(data: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _record(data: object) -> CmuxRecord | None:
    if not isinstance(data, dict):
        return None
    session_id = _text(_first(data, "sessionId", "session_id"))
    workspace_id = _text(_first(data, "workspaceId", "workspace_id"))
    surface_id = _text(_first(data, "surfaceId", "surface_id"))
    if session_id is None or workspace_id is None or surface_id is None:
        return None
    pid = data.get("pid")
    exists = data.get("stored_pid_exists")
    return CmuxRecord(
        session_id=session_id,
        workspace_id=workspace_id,
        surface_id=surface_id,
        pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        cwd=_text(data.get("cwd")),
        lifecycle=_text(_first(data, "agentLifecycle", "agent_lifecycle")),
        pid_exists=exists if isinstance(exists, bool) else None,
    )


def parse_cmux_store(text: str) -> CmuxStore | None:
    """Parse ``~/.cmuxterm/claude-hook-sessions.json``; ``None`` when it is not the known shape.

    The shape check (decision 0025) is ``version == 1`` and a ``sessions`` collection. Of every
    record only the allowlisted fields are read: ``launch_arguments``, ``stored_pid_arguments`` and
    ``transcript_path`` can carry secrets and are never looked at. A record that lacks the session,
    workspace or surface id is skipped.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("version") != CMUX_STORE_VERSION:
        return None
    sessions = data.get("sessions")
    if isinstance(sessions, dict):
        candidates: Sequence[object] = list(sessions.values())
    elif isinstance(sessions, list):
        candidates = sessions
    else:
        return None
    records = (_record(item) for item in candidates)
    return CmuxStore(tuple(record for record in records if record is not None))


@dataclass(frozen=True)
class CmuxStatus:
    """What the adapter knows about cmux itself, for the doctor and the collector status."""

    version: str | None = None
    build: str | None = None
    tested: bool = False
    degraded: str | None = None
    """``unknown cmux version`` when the version is not in the tested list."""
    store_state: str = "absent"
    """``ok``, ``absent`` or ``unrecognized`` (present, but not the known shape)."""


@dataclass(frozen=True)
class CmuxData:
    store: CmuxStore | None = None
    status: CmuxStatus = field(default_factory=CmuxStatus)


def parse_cmux_version(text: str) -> tuple[str, str | None] | None:
    """``(version, build)`` of ``cmux --version`` output (``cmux 0.64.25 (106) [sha]``)."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    match = _CMUX_VERSION.match(first)
    if match is None:
        return None
    return match[1], match[2] or None


def version_is_tested(version: str | None, tested: Collection[str]) -> bool:
    """Is ``version`` one of the dotted prefixes in ``tested`` (``0.64`` covers ``0.64.25``)?"""
    if version is None:
        return False
    return any(version == prefix or version.startswith(prefix + ".") for prefix in tested)


class CmuxHost:
    """cmux, the primary host: hook-time ids, then the session store, then the window."""

    kind = "cmux"

    def __init__(self, data: CmuxData | None = None) -> None:
        self._store = None if data is None else data.store

    def handles(self, bundle_id: str) -> bool:
        return bundle_id == CMUX_BUNDLE_ID

    def join(self, session: HostSession, windows: Sequence[WindowRow]) -> Join:
        ids = session.cmux_ids
        extra: dict[str, Any] = {}
        via, single = VIA_CHAIN, WINDOW_ONLY
        if ids is not None and ids.surface_id:
            via, single = VIA_CMUX_HOOK, HIGH
            extra = self._ids(ids.workspace_id, ids.surface_id)
        else:
            found = self._lookup(session)
            if found is not None:
                record, exact = found
                via, single = VIA_CMUX_STORE, HIGH if exact else MEDIUM
                extra = self._ids(record.workspace_id, record.surface_id)
        join = _window_level(
            self.kind, windows, via=via, single=single, what="cmux windows", **extra
        )
        if not extra and join.unresolved is None:
            return replace(join, note=join.note or "cmux workspace and surface not resolved")
        return join

    @staticmethod
    def _ids(workspace_id: str | None, surface_id: str) -> dict[str, Any]:
        return {
            "cmux_workspace_id": workspace_id,
            "cmux_surface_id": surface_id,
            "host_ref": f"{workspace_id}/{surface_id}" if workspace_id else surface_id,
        }

    def _lookup(self, session: HostSession) -> tuple[CmuxRecord, bool] | None:
        """The store record of the session: ``(record, exact)``, exact meaning the pid matches."""
        if self._store is None:
            return None
        candidates = [
            record
            for record in self._store.records
            if record.session_id == session.session_id and record.pid_exists is not False
        ]
        for record in candidates:
            if session.pid is not None and record.pid == session.pid:
                return record, True
        return (candidates[0], False) if candidates else None


class CmuxSource:
    """Reads cmux's version and session store through the seams.

    Detects the version once with ``cmux --version`` (no socket needed), or from the app's
    ``Info.plist`` when the CLI is not there. A version outside ``tested`` logs one warning and is
    reported as ``degraded``; the store is still used when it passes the shape check.
    """

    def __init__(
        self,
        runner: ProcessRunner,
        fs: FileSystem,
        *,
        home: str,
        cli: str = CMUX_CLI,
        plist: str = CMUX_INFO_PLIST,
        tested: Sequence[str] = TESTED_CMUX_VERSIONS,
        timeout: float = 5.0,  # noqa: ASYNC109 - a subprocess timeout
    ) -> None:
        self._runner = runner
        self._fs = fs
        self._store_path = Path(home, *CMUX_STORE_PARTS)
        self._cli = cli
        self._plist = plist
        self._tested = tuple(tested)
        self._timeout = timeout
        self._version: tuple[str, str | None] | None = None
        self._detected = False
        self._warned = False

    async def load(self) -> CmuxData:
        if not self._detected:
            self._version = await self._detect()
            self._detected = True
        version, build = self._version if self._version is not None else (None, None)
        tested = version_is_tested(version, self._tested)
        degraded = None if tested else "unknown cmux version"
        if degraded is not None and not self._warned:
            self._warned = True
            logger.warning("cmux version %s is not in the tested list", version or "unknown")
        store, state = self._read_store()
        status = CmuxStatus(
            version=version, build=build, tested=tested, degraded=degraded, store_state=state
        )
        return CmuxData(store=store, status=status)

    async def _detect(self) -> tuple[str, str | None] | None:
        try:
            result = await self._runner.run([self._cli, "--version"], timeout=self._timeout)
        except (OSError, TimeoutError):
            return self._plist_version()
        if result.returncode != 0:
            return self._plist_version()
        return parse_cmux_version(result.stdout.decode("utf-8", errors="replace"))

    def _plist_version(self) -> tuple[str, str | None] | None:
        try:
            info = plistlib.loads(self._fs.read_bytes(self._plist))
        except (OSError, ValueError, plistlib.InvalidFileException):
            return None
        version = info.get("CFBundleShortVersionString") if isinstance(info, dict) else None
        build = info.get("CFBundleVersion") if isinstance(info, dict) else None
        if not isinstance(version, str) or not version:
            return None
        return version, build if isinstance(build, str) else None

    def _read_store(self) -> tuple[CmuxStore | None, str]:
        try:
            if self._fs.stat(self._store_path).size > MAX_STORE_BYTES:
                return None, "unrecognized"
            text = self._fs.read_text(self._store_path)
        except OSError:
            return None, "absent"
        except UnicodeDecodeError:
            return None, "unrecognized"
        store = parse_cmux_store(text)
        return (store, "ok") if store is not None else (None, "unrecognized")


# --------------------------------------------------------------------------------------------
# JetBrains
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class JetBrainsProject:
    """An opened project of one JetBrains product."""

    path: str
    frame_title: str = ""
    ide_name: str = ""
    """``.idea/.name`` of the project, when it has one."""


def product_stem(dirname: str) -> str:
    """``PyCharm2026.1`` becomes ``pycharm``; ``IntelliJIdea2026.2`` becomes ``intellijidea``."""
    return re.sub(r"[\d.]+$", "", dirname).lower()


def _version_key(dirname: str) -> tuple[int, ...]:
    tail = re.search(r"([\d.]+)$", dirname)
    if not tail:
        return ()
    return tuple(int(part) for part in tail[1].split(".") if part)


def newest_product_dirs(names: Sequence[str]) -> dict[str, str]:
    """The newest directory name (by version number) of each JetBrains product stem."""
    best: dict[str, str] = {}
    for name in names:
        stem = product_stem(name)
        if stem not in best or _version_key(name) > _version_key(best[stem]):
            best[stem] = name
    return best


def jetbrains_stem_for(bundle_id: str) -> str:
    """The product directory stem of a ``com.jetbrains.*`` bundle id."""
    suffix = bundle_id.removeprefix(JETBRAINS_BUNDLE_PREFIX)
    return JETBRAINS_PRODUCT.get(suffix, suffix)


def parse_recent_projects(xml_text: str, home: str) -> list[JetBrainsProject]:
    """The opened projects of one ``recentProjects.xml``. Raises ``ET.ParseError`` on bad XML.

    ``frameTitle`` is often empty. ``displayName`` is deliberately not used: it lists module
    names, not the name that appears in the window title.
    """
    root = ET.fromstring(xml_text)
    projects: list[JetBrainsProject] = []
    for entry in root.iter("entry"):
        key = entry.get("key")
        meta = entry.find(".//RecentProjectMetaInfo")
        if not key or meta is None or meta.get("opened") != "true":
            continue
        path = key.replace("$USER_HOME$", home).rstrip("/")
        projects.append(JetBrainsProject(path=path, frame_title=meta.get("frameTitle") or ""))
    return projects


def title_project(title: str) -> str:
    """The part of a JetBrains window title before ``<space>–<space><file>``."""
    return _TITLE_SPLIT.split(title, maxsplit=1)[0].strip()


def path_contains(project: str, cwd: str) -> bool:
    """Is ``cwd`` the project directory or below it?"""
    return cwd == project or cwd.startswith(project + "/")


def window_match(title: str, project: JetBrainsProject, home: str) -> str | None:
    """How does a window title name this project: ``"path"``, ``"name"`` or not at all (``None``).

    JetBrains shows ``<name> [<path>]`` when two open projects share a name; the path then decides
    exactly. Otherwise only the name is shown (the frame title's project part, the ``.idea/.name``
    or the directory name), and two different projects can share it.
    """
    shown = title_project(title)
    clash = _TITLE_PATH.match(shown)
    if clash:
        same = clash["path"].replace("~", home, 1).rstrip("/") == project.path
        return "path" if same else None
    names = {title_project(project.frame_title), project.ide_name, os.path.basename(project.path)}
    names.discard("")
    return "name" if shown in names else None


class JetBrainsHost:
    """JetBrains IDEs: the project path contains the cwd, the window title names the project."""

    kind = "jetbrains"

    def __init__(self, projects: Mapping[str, Sequence[JetBrainsProject]], *, home: str) -> None:
        self._projects = projects
        self._home = home

    def handles(self, bundle_id: str) -> bool:
        return bundle_id.startswith(JETBRAINS_BUNDLE_PREFIX)

    def join(self, session: HostSession, windows: Sequence[WindowRow]) -> Join:
        cwd = session.cwd.rstrip("/")
        if not cwd:
            return _unresolved(self.kind, "session has no cwd")
        stem = jetbrains_stem_for(windows[0].app_bundle_id or "")
        matching = [p for p in self._projects.get(stem, ()) if path_contains(p.path, cwd)]
        if not matching:
            return _unresolved(self.kind, "no opened JetBrains project contains the cwd")
        project = max(matching, key=lambda p: len(p.path))  # the longest prefix wins
        extra: dict[str, Any] = {"host_ref": project.path, "host_project": project.path}
        matches = [(w, window_match(w.title or "", project, self._home)) for w in windows]
        by_path = [w for w, how in matches if how == "path"]
        candidates = by_path or [w for w, how in matches if how == "name"]
        if len(candidates) == 1:
            window = candidates[0]
            return Join(
                host=self.kind,
                via=VIA_PROJECT_PATH,
                confidence=HIGH if by_path else MEDIUM,
                window_id=window.window_id,
                workspace=window.workspace,
                **extra,
            )
        if not candidates:
            return _workspace_level(
                self.kind,
                windows,
                via=VIA_PROJECT_PATH,
                note="no window title matches the project",
                **extra,
            )
        return _workspace_level(
            self.kind,
            candidates,
            via=VIA_PROJECT_PATH,
            note=f"{len(candidates)} windows share the project name",
            **extra,
        )


def load_jetbrains(fs: FileSystem, home: str) -> dict[str, tuple[JetBrainsProject, ...]]:
    """The opened projects of the newest version of each installed JetBrains product.

    Reads ``recentProjects.xml`` of each product and the ``.idea/.name`` of each opened project
    (the IDE's own project name, which the window title shows). Anything unreadable is skipped.
    """
    base = Path(home, "Library", "Application Support", "JetBrains")
    try:
        dirs = [
            name
            for name in fs.listdir(base)
            if fs.exists(base / name / "options" / "recentProjects.xml")
        ]
    except OSError:
        return {}
    projects: dict[str, tuple[JetBrainsProject, ...]] = {}
    for stem, dirname in newest_product_dirs(dirs).items():
        try:
            text = fs.read_text(base / dirname / "options" / "recentProjects.xml")
            parsed = parse_recent_projects(text, home)
        except (OSError, UnicodeDecodeError, ET.ParseError):
            continue
        projects[stem] = tuple(
            JetBrainsProject(p.path, p.frame_title, _ide_name(fs, p.path)) for p in parsed
        )
    return projects


def _ide_name(fs: FileSystem, project: str) -> str:
    try:
        text = fs.read_text(Path(project, ".idea", ".name"))
    except (OSError, UnicodeDecodeError):
        return ""
    return text.strip().splitlines()[0].strip()[:MAX_IDE_NAME] if text.strip() else ""


# --------------------------------------------------------------------------------------------
# iTerm2
# --------------------------------------------------------------------------------------------


class Iterm2Host:
    """iTerm2, best effort: the window whose title equals the session's title, else the only one."""

    kind = "iterm2"

    def handles(self, bundle_id: str) -> bool:
        return bundle_id == "com.googlecode.iterm2"

    def join(self, session: HostSession, windows: Sequence[WindowRow]) -> Join:
        named = [w for w in windows if session.name and w.title == session.name]
        if len(named) == 1:
            window = named[0]
            return Join(
                host=self.kind,
                via=VIA_TITLE,
                confidence=MEDIUM,
                window_id=window.window_id,
                workspace=window.workspace,
            )
        return _window_level(self.kind, windows, via=VIA_CHAIN, single=LOW, what="iTerm2 windows")


# --------------------------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HostData:
    """What the adapters read from the machine, once per resolver refresh."""

    home: str
    cmux: CmuxData = field(default_factory=CmuxData)
    jetbrains: Mapping[str, tuple[JetBrainsProject, ...]] = field(default_factory=dict)


async def load_host_data(
    source: CmuxSource, fs: FileSystem, *, home: str, with_jetbrains: bool = True
) -> HostData:
    """Read the cmux store and status and the JetBrains opened projects."""
    return HostData(
        home=home,
        cmux=await source.load(),
        jetbrains=load_jetbrains(fs, home) if with_jetbrains else {},
    )


def host_loader(
    runner: ProcessRunner, fs: FileSystem, *, home: str
) -> Callable[[], Awaitable[HostData]]:
    """The loader the daemon hands the resolver: :func:`load_host_data` over the given seams.

    The cmux version is detected once (one ``cmux --version``); the store and the JetBrains lists
    are read on every call.
    """
    source = CmuxSource(runner, fs, home=home)

    async def load() -> HostData:
        return await load_host_data(source, fs, home=home)

    return load


def default_adapters(data: HostData) -> tuple[HostAdapter, ...]:
    """The adapters in match order; :class:`GenericHost` is the fallback of :func:`adapter_for`."""
    return (CmuxHost(data.cmux), JetBrainsHost(data.jetbrains, home=data.home), Iterm2Host())


def adapter_for(bundle_id: str, adapters: Sequence[HostAdapter]) -> HostAdapter:
    """The first adapter that handles ``bundle_id``, else the generic one."""
    for adapter in adapters:
        if adapter.handles(bundle_id):
            return adapter
    return GenericHost()
