"""Pure logic of the ``appcontext`` collector (``PLAN-final-plan.md`` WP5.3).

Nothing here does I/O or reads a clock. The title parsers are in :mod:`~.titles` (tier 1); this
module holds the rest:

* the **context item** a window gets, and the record that carries it (:func:`to_record`,
  :func:`gone_record`);
* the parsers of the second tier's inputs: cmux's session store, JetBrains' ``recentProjects.xml``,
  Obsidian's ``obsidian.json`` and Chrome's ``Local State``;
* the small clean-ups every output goes through (URLs lose their query, user information and
  fragment).

**Record shape.** One ``context_item`` event per window and item key. ``entity_id`` is
``<window id>:<key>``, ``context_kind`` is the item's kind and ``data`` holds ``window_id``,
``key``, ``tier``, ``fields`` (the parsed values), and when present ``unread``, ``confidence`` and
``link``. A ``gone`` record (``data.gone = true``) withdraws an item. The ingest gate redacts every
string of the record before it reaches SQLite or SSE, so nothing here redacts.

**What is read, and what is never read.** Only the inputs named below. Nothing under
``~/Library/Containers`` or ``Group Containers``, and no Teams, Slack or Outlook store.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import quote, urlsplit

from pydantic import JsonValue

from fylgja.collectors.appcontext.titles import (
    BUNDLE_DATAGRIP,
    JETBRAINS_BUNDLES,
    TitleContext,
)
from fylgja.models import ContextItemEvent, ContextItemPayload

__all__ = [
    "BUNDLE_ITERM2",
    "MAX_CONFIG_BYTES",
    "SOURCE",
    "ChromeProfile",
    "CmuxTab",
    "Item",
    "JetBrainsProject",
    "Vault",
    "WindowFacts",
    "chrome_profile_dir",
    "chrome_time_from_unix",
    "gone_record",
    "jetbrains_product_prefixes",
    "obsidian_link",
    "parse_chrome_profiles",
    "parse_cmux_tabs",
    "parse_iterm2_output",
    "parse_jetbrains_projects",
    "parse_obsidian_vaults",
    "pick_cmux_tab",
    "resolve_jetbrains_project",
    "resolve_vault",
    "sanitize_url",
    "title_item",
    "title_summary",
    "to_record",
    "unix_from_chrome_time",
]

SOURCE: Final = "appcontext"
BUNDLE_ITERM2: Final = "com.googlecode.iterm2"

MAX_CONFIG_BYTES: Final = 8 * 1024 * 1024
"""Input files larger than this are not parsed (a hostile or runaway file must not cost memory)."""
MAX_URL_CHARS: Final = 300
MAX_SUMMARY_CHARS: Final = 120
MAX_PROJECTS: Final = 500

TIER_TITLE: Final = 1
TIER_FOCUS: Final = 2

KEY_TITLE: Final = "title"


@dataclass(frozen=True)
class WindowFacts:
    """What the collector knows of one window: a row of the store's open windows."""

    window_id: str
    bundle_id: str | None
    title: str | None
    workspace: str | None = None


@dataclass(frozen=True)
class Item:
    """One context item before it becomes a record."""

    key: str
    """Unique per window: ``title``, ``cmux``, ``chrome``, ``jetbrains`` and so on."""
    kind: str
    tier: int
    title: str | None = None
    fields: Mapping[str, JsonValue] = field(default_factory=dict[str, JsonValue])
    unread: int | bool | None = None
    confidence: str | None = None
    link: str | None = None

    def fingerprint(self) -> str:
        """Equal fingerprints mean the same item: nothing is emitted again."""
        return json.dumps(
            [
                self.kind,
                self.title,
                dict(self.fields),
                self.unread,
                self.confidence,
                self.link,
            ],
            sort_keys=True,
            ensure_ascii=False,
        )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------


def to_record(item: Item, window: WindowFacts, observed_at: datetime) -> ContextItemEvent:
    """The ``context_item`` event that attaches ``item`` to ``window``."""
    data: dict[str, JsonValue] = {
        "window_id": window.window_id,
        "key": item.key,
        "tier": item.tier,
        "fields": dict(item.fields),
    }
    if item.unread is not None:
        data["unread"] = item.unread
    if item.confidence is not None:
        data["confidence"] = item.confidence
    if item.link is not None:
        data["link"] = item.link
    return ContextItemEvent(
        source=SOURCE,
        entity_id=f"{window.window_id}:{item.key}",
        observed_at=observed_at,
        payload=ContextItemPayload(
            context_kind=item.kind,
            app_bundle_id=window.bundle_id,
            title=item.title,
            data=data,
        ),
    )


def gone_record(
    window_id: str, key: str, bundle_id: str | None, observed_at: datetime
) -> ContextItemEvent:
    """The event that withdraws the item ``key`` of a window."""
    return ContextItemEvent(
        source=SOURCE,
        entity_id=f"{window_id}:{key}",
        observed_at=observed_at,
        payload=ContextItemPayload(
            context_kind="gone",
            app_bundle_id=bundle_id,
            data={"window_id": window_id, "key": key, "gone": True},
        ),
    )


# --------------------------------------------------------------------------------------------
# Tier 1: the title context as an item
# --------------------------------------------------------------------------------------------

_SUMMARY_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    "ide": ("project", "file"),
    "database": ("project", "console"),
    "browser": ("page_title",),
    "mail": ("folder",),
    "notes": ("note", "vault"),
    "terminal": ("workspace",),
}
_CHAT_FIELDS: Final = ("chat_partner", "channel", "context", "section")


def title_summary(context: TitleContext) -> str | None:
    """One short line for a parsed title: what the window is about (the third one-liner source)."""
    fields = context.fields
    if context.kind == "chat":
        for name in _CHAT_FIELDS:
            value = fields.get(name)
            if value:
                return _clip(value, MAX_SUMMARY_CHARS)
        return None
    names = _SUMMARY_FIELDS.get(context.kind, ())
    parts = [fields[name] for name in names if fields.get(name)]
    if not parts:
        return None
    return _clip(" · ".join(parts), MAX_SUMMARY_CHARS)


def title_item(context: TitleContext) -> Item:
    """Tier 1: the item a parsed title makes. The title field is the one-line summary."""
    return Item(
        key=KEY_TITLE,
        kind=context.kind,
        tier=TIER_TITLE,
        title=title_summary(context),
        fields=dict(context.fields),
        unread=context.unread,
        confidence=context.confidence,
    )


# --------------------------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------------------------


def sanitize_url(url: str) -> str | None:
    """``scheme://host[:port]/path`` of an http(s) URL; the query, fragment and user information
    are dropped. Anything else (``chrome://``, ``about:``, ``javascript:``, garbage) gives ``None``.
    """
    text = url.strip()
    if not text or len(text) > 4 * MAX_URL_CHARS or any(c.isspace() for c in text):
        return None
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    shown = f"{parts.scheme}://{host}" + (f":{port}" if port else "") + parts.path
    return _clip(shown, MAX_URL_CHARS)


# --------------------------------------------------------------------------------------------
# cmux: the session store (decision 0025)
# --------------------------------------------------------------------------------------------

CMUX_STORE_VERSION: Final = 1


@dataclass(frozen=True)
class CmuxTab:
    """The active session of one cmux workspace: its workspace, surface (tab) and working dir."""

    workspace_id: str
    surface_id: str
    cwd: str | None
    lifecycle: str | None
    updated_at: float


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def parse_cmux_tabs(text: str) -> list[CmuxTab] | None:
    """The active session of each workspace in ``~/.cmuxterm/claude-hook-sessions.json``.

    The shape check is decision 0025's (``version == 1`` and a ``sessions`` collection); anything
    else gives ``None``. Of each record only ``workspaceId``, ``surfaceId``, ``cwd``,
    ``agentLifecycle`` and ``updatedAt`` are read: ``launchCommand``, the argument arrays and
    ``transcriptPath`` can carry secrets and are never looked at. A workspace with no active
    session, or whose active session is not in ``sessions``, has no tab.
    """
    if len(text) > MAX_CONFIG_BYTES:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("version") != CMUX_STORE_VERSION:
        return None
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return None
    active = data.get("activeSessionsByWorkspace")
    if not isinstance(active, dict):
        return []
    tabs: list[CmuxTab] = []
    for workspace_id, entry in active.items():
        if not isinstance(workspace_id, str) or not isinstance(entry, dict):
            continue
        session = sessions.get(_text(entry.get("sessionId")) or "")
        if not isinstance(session, dict):
            continue
        surface_id = _text(session.get("surfaceId"))
        if surface_id is None or _text(session.get("workspaceId")) not in (None, workspace_id):
            continue
        tabs.append(
            CmuxTab(
                workspace_id=workspace_id,
                surface_id=surface_id,
                cwd=_text(session.get("cwd")),
                lifecycle=_text(session.get("agentLifecycle")),
                updated_at=max(_number(session.get("updatedAt")), _number(entry.get("updatedAt"))),
            )
        )
    return tabs


def _cwd_parts(cwd: str | None) -> set[str]:
    return set() if cwd is None else {part.casefold() for part in PurePosixPath(cwd).parts}


def pick_cmux_tab(tabs: Sequence[CmuxTab], title: str | None) -> tuple[CmuxTab, str] | None:
    """The cmux workspace the window is most likely showing, and how sure that is.

    The store does not say which workspace is selected, and the cmux CLI is refused outside a cmux
    terminal (decision 0025). The window title is a weak hint for the selected workspace's name
    (spike 0003, question 4): a workspace whose active session's working directory has a path
    component equal to the title wins (``medium``); a lone workspace is that one (``medium``);
    otherwise the most recently active workspace is the guess (``low``).
    """
    if not tabs:
        return None
    by_recent = sorted(tabs, key=lambda tab: (-tab.updated_at, tab.workspace_id))
    token = None if title is None else title.strip().casefold()
    if token:
        named = [tab for tab in by_recent if token in _cwd_parts(tab.cwd)]
        if len(named) == 1:
            return named[0], "medium"
        if named:
            return named[0], "low"
    if len(tabs) == 1:
        return tabs[0], "medium"
    return by_recent[0], "low"


# --------------------------------------------------------------------------------------------
# JetBrains: recentProjects.xml
# --------------------------------------------------------------------------------------------

_JETBRAINS_PRODUCTS: Final[Mapping[str, tuple[str, ...]]] = {
    "com.jetbrains.intellij": ("IntelliJIdea", "Idea"),
    "com.jetbrains.intellij.ce": ("IdeaIC", "IntelliJIdeaCE"),
    "com.jetbrains.pycharm": ("PyCharm", "Pycharm"),
    "com.jetbrains.pycharm.ce": ("PyCharmCE",),
    "com.jetbrains.WebStorm": ("WebStorm",),
    "com.jetbrains.goland": ("GoLand",),
    "com.jetbrains.rider": ("Rider",),
    "com.jetbrains.CLion": ("CLion", "Clion"),
    "com.jetbrains.PhpStorm": ("PhpStorm",),
    "com.jetbrains.rubymine": ("RubyMine",),
    "com.jetbrains.rustrover": ("RustRover",),
    "com.jetbrains.dataspell": ("DataSpell",),
    BUNDLE_DATAGRIP: ("DataGrip", "Datagrip"),
    "com.jetbrains.AppCode": ("AppCode",),
}


def jetbrains_product_prefixes(bundle_id: str) -> tuple[str, ...]:
    """Directory-name prefixes of an IDE's product directories under
    ``~/Library/Application Support/JetBrains/``; empty for an IDE that keeps its files elsewhere
    (Android Studio) and for a bundle id that is not a JetBrains IDE."""
    if bundle_id not in JETBRAINS_BUNDLES and bundle_id != BUNDLE_DATAGRIP:
        return ()
    return _JETBRAINS_PRODUCTS.get(bundle_id, ())


@dataclass(frozen=True)
class JetBrainsProject:
    path: str
    names: tuple[str, ...]
    """The names the IDE shows for it: its display name, its frame title and the directory name."""
    opened_ms: int = 0
    opened: bool = False
    """``RecentProjectMetaInfo opened="true"``: the IDE has the project open now."""
    frame_title: str | None = None
    """The window title the IDE last saved for the project."""


def _expand_home(path: str, home: str) -> str:
    if path.startswith("$USER_HOME$"):
        return home.rstrip("/") + path[len("$USER_HOME$") :]
    if path == "~" or path.startswith("~/"):
        return home.rstrip("/") + path[1:]
    return path


def _int(value: str | None) -> int:
    try:
        return int(value) if value is not None else 0
    except ValueError:
        return 0


def parse_jetbrains_projects(text: str, home: str) -> list[JetBrainsProject] | None:
    """The entries of ``options/recentProjects.xml``; ``None`` when it is not that XML.

    Each ``<entry key="$USER_HOME$/...">`` is a project; ``$USER_HOME$`` becomes ``home``. The
    names are the entry's ``displayName`` and ``frameTitle`` (``RecentProjectMetaInfo`` attributes)
    and the last path component. The open time is the newest of the ``projectOpenTimestamp`` and
    ``activationTimestamp`` options.
    """
    if len(text) > MAX_CONFIG_BYTES:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if root.tag != "application":
        return None
    projects: list[JetBrainsProject] = []
    for entry in root.iter("entry"):
        key = entry.get("key")
        if not key or ("$USER_HOME$" not in key and not key.startswith(("/", "~"))):
            continue
        path = _expand_home(key, home)
        names: list[str] = []
        is_open = False
        frame_title: str | None = None
        meta = entry.find(".//RecentProjectMetaInfo")
        if meta is not None:
            names += [n for n in (meta.get("displayName"), meta.get("frameTitle")) if n]
            is_open = meta.get("opened") == "true"
            frame_title = meta.get("frameTitle") or None
        base = PurePosixPath(path).name
        if base:
            names.append(base)
        opened = 0
        for option in entry.iter("option"):
            if option.get("name") in ("projectOpenTimestamp", "activationTimestamp"):
                opened = max(opened, _int(option.get("value")))
        projects.append(JetBrainsProject(path, tuple(names), opened, is_open, frame_title))
        if len(projects) >= MAX_PROJECTS:
            break
    return projects


def resolve_jetbrains_project(
    fields: Mapping[str, str],
    projects: Sequence[JetBrainsProject],
    home: str,
    *,
    title: str | None = None,
) -> tuple[str, str, str] | None:
    """``(path, confidence, via)`` of the project a JetBrains title names, or ``None``.

    A title that carries the path (``<project> [~/dir]``) is exact. Otherwise the project name is
    matched, ignoring case, against the names in ``recentProjects.xml``. The rules, in order:

    * one match is ``high``;
    * several matches of which exactly one is ``opened`` is ``high`` (``…:opened``);
    * several open (or none open) matches of which exactly one has a ``frameTitle`` equal to
      ``title`` is ``medium`` (``…:frameTitle``);
    * otherwise the most recently opened of the open matches (all of them when none is open) is
      ``medium``.
    """
    explicit = fields.get("path")
    if explicit:
        return _expand_home(explicit, home), "high", "title"
    name = fields.get("project")
    if not name:
        return None
    wanted = name.casefold()
    matches = [p for p in projects if any(n.casefold() == wanted for n in p.names)]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0].path, "high", "recentProjects.xml"
    open_matches = [p for p in matches if p.opened]
    if len(open_matches) == 1:
        return open_matches[0].path, "high", "recentProjects.xml:opened"
    pool = open_matches or matches
    if title:
        framed = [p for p in pool if p.frame_title == title]
        if len(framed) == 1:
            return framed[0].path, "medium", "recentProjects.xml:frameTitle"
    best = max(pool, key=lambda p: (p.opened_ms, p.path))
    return best.path, "medium", "recentProjects.xml"


# --------------------------------------------------------------------------------------------
# Obsidian: obsidian.json
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Vault:
    path: str
    opened_ms: int = 0

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name


def parse_obsidian_vaults(text: str) -> list[Vault] | None:
    """The vaults of ``~/Library/Application Support/obsidian/obsidian.json``.

    The file is ``{"vaults": {"<id>": {"path": "<dir>", "ts": <ms>, ...}}, "frame": {...}}``. Only
    ``path`` and ``ts`` are read. ``None`` when it is not that shape.
    """
    if len(text) > MAX_CONFIG_BYTES:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    vaults = data.get("vaults") if isinstance(data, dict) else None
    if not isinstance(vaults, dict):
        return None
    found: list[Vault] = []
    for entry in vaults.values():
        if not isinstance(entry, dict):
            continue
        path = _text(entry.get("path"))
        if path is None or not path.startswith("/"):
            continue
        stamp = entry.get("ts")
        opened = stamp if isinstance(stamp, int) and not isinstance(stamp, bool) else 0
        found.append(Vault(path.rstrip("/") or "/", opened))
    return found


def resolve_vault(vaults: Sequence[Vault], name: str | None) -> Vault | None:
    """The vault whose directory name is ``name``; the most recently opened when several match."""
    if not name:
        return None
    matches = [v for v in vaults if v.name == name]
    if not matches:
        return None
    return max(matches, key=lambda v: (v.opened_ms, v.path))


def obsidian_link(vault: str, note: str | None) -> str:
    """``obsidian://open?vault=<name>[&file=<note>]``."""
    link = f"obsidian://open?vault={quote(vault, safe='')}"
    if note:
        link += f"&file={quote(note, safe='')}"
    return link


# --------------------------------------------------------------------------------------------
# Chrome: Local State, and the iTerm2 reply
# --------------------------------------------------------------------------------------------

_PAREN_LABEL: Final = re.compile(r"^.*\((?P<label>[^()]+)\)\s*$")


@dataclass(frozen=True)
class ChromeProfile:
    directory: str
    """The profile directory under Chrome's user data directory (``Default``, ``Profile 2``)."""
    name: str | None = None
    gaia_name: str | None = None
    user_name: str | None = None
    """Only used to recognise a title; never emitted."""


def parse_chrome_profiles(text: str) -> list[ChromeProfile] | None:
    """The profiles of Chrome's ``Local State`` (``profile.info_cache``); ``None`` when it is not
    that JSON. Only ``name``, ``gaia_name`` and ``user_name`` are read."""
    if len(text) > MAX_CONFIG_BYTES:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    profile = data.get("profile") if isinstance(data, dict) else None
    cache = profile.get("info_cache") if isinstance(profile, dict) else None
    if not isinstance(cache, dict):
        return None
    found: list[ChromeProfile] = []
    for directory, entry in cache.items():
        if not isinstance(directory, str) or not isinstance(entry, dict):
            continue
        found.append(
            ChromeProfile(
                directory=directory,
                name=_text(entry.get("name")),
                gaia_name=_text(entry.get("gaia_name")),
                user_name=_text(entry.get("user_name")),
            )
        )
    return found


def chrome_profile_dir(profiles: Sequence[ChromeProfile], title_profile: str | None) -> str | None:
    """The profile directory a title's profile suffix names, or ``None`` when it is not exactly one.

    The suffix is the profile's name, or ``<account name> (<profile name>)``. Matched: the name, the
    account name, the sign-in user name, and the label in parentheses.
    """
    if not title_profile:
        return None
    label = _PAREN_LABEL.match(title_profile)
    inner = None if label is None else label["label"].strip()
    matches: list[str] = []
    for profile in profiles:
        names = {profile.name, profile.gaia_name, profile.user_name} - {None}
        if title_profile in names or (inner is not None and inner in names):
            matches.append(profile.directory)
    return matches[0] if len(matches) == 1 else None


def parse_iterm2_output(text: str) -> tuple[str | None, str | None]:
    """``(path, job name)`` from the two lines the iTerm2 script prints."""
    lines = text.strip("\n").split("\n")
    path = lines[0].strip() if lines else ""
    job = lines[1].strip() if len(lines) > 1 else ""
    return (path or None, job or None)


def unix_from_chrome_time(microseconds: int) -> datetime:
    """Chrome stores times as microseconds since 1601-01-01 UTC."""
    return datetime.fromtimestamp(microseconds / 1_000_000 - 11_644_473_600, UTC)


def chrome_time_from_unix(moment: datetime) -> int:
    return int((moment.timestamp() + 11_644_473_600) * 1_000_000)
