"""Pure per-app window-title parsers (``PLAN-final-plan.md`` WP5.3 first tier, A15).

``parse_title(bundle_id, title)`` turns the title of a window into a :class:`TitleContext`: a
``kind`` (``terminal``, ``ide``, ``database``, ``browser``, ``chat``, ``mail`` or ``notes``), the
``fields`` the title carries (project, file, path, page title, Chrome profile, chat partner, vault,
note, channel, console and so on), an ``unread`` marker and a ``confidence``.

**Table, not chain.** The parser for an app is looked up in :data:`PARSERS`, a table keyed by bundle
id. An unknown bundle id, an empty title and a title the app's parser cannot read all give ``None``.

**A15: unread comes only from an explicit marker confirmed for that app** in
``docs/revalidation/2026-10-06-app-titles.md``. Today that is Slack alone (the ``! `` prefix and the
``N new items`` segment). Every other parser leaves ``unread`` at ``None``. A number or an asterisk
inside a page title, a file name or a chat name is never a marker, and nothing is inferred where an
app shows no marker.

**Order.** These functions are pure and run in-process on the raw title, before the ingest gate
redacts titles and before anything is persisted or sent over SSE (``PLAN-final-plan.md`` §4.4). The
collector (step 40) parses first and then lets the gate redact the title and every string field of
the result. Nothing here logs, stores or reads a title beyond the call.

Formats were observed on the installed app versions (see the revalidation log). Titles are free
text, so each parser is deliberately loose about the content and strict about the separators.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

__all__ = [
    "BUNDLE_CHROME",
    "BUNDLE_CMUX",
    "BUNDLE_DATAGRIP",
    "BUNDLE_OBSIDIAN",
    "BUNDLE_OUTLOOK",
    "BUNDLE_SLACK",
    "BUNDLE_TEAMS",
    "JETBRAINS_BUNDLES",
    "MAX_TITLE_CHARS",
    "PARSERS",
    "Confidence",
    "TitleContext",
    "TitleParser",
    "parse_title",
]

Confidence = Literal["high", "medium", "low"]

BUNDLE_CMUX: Final = "com.cmuxterm.app"
BUNDLE_CHROME: Final = "com.google.Chrome"
BUNDLE_TEAMS: Final = "com.microsoft.teams2"
BUNDLE_OBSIDIAN: Final = "md.obsidian"
BUNDLE_OUTLOOK: Final = "com.microsoft.Outlook"
BUNDLE_SLACK: Final = "com.tinyspeck.slackmacgap"
BUNDLE_DATAGRIP: Final = "com.jetbrains.datagrip"

# Observed: intellij, pycharm and datagrip. The rest share the JetBrains platform's title scheme.
JETBRAINS_BUNDLES: Final = (
    "com.jetbrains.intellij",
    "com.jetbrains.intellij.ce",
    "com.jetbrains.pycharm",
    "com.jetbrains.pycharm.ce",
    "com.jetbrains.WebStorm",
    "com.jetbrains.goland",
    "com.jetbrains.rider",
    "com.jetbrains.CLion",
    "com.jetbrains.PhpStorm",
    "com.jetbrains.rubymine",
    "com.jetbrains.rustrover",
    "com.jetbrains.dataspell",
    "com.jetbrains.AppCode",
    "com.google.android.studio",
)

# Titles longer than this are cut before matching, so a hostile title cannot make a regex slow.
MAX_TITLE_CHARS: Final = 2048

_EN_DASH_SEP: Final = " – "
_ELLIPSIS: Final = ("…", "...")


@dataclass(frozen=True, slots=True)
class TitleContext:
    """What one window title says.

    ``unread`` is a count, ``True`` for a marker without a count, or ``None`` (no marker, or an
    app whose marker is not confirmed). ``fields`` holds only the strings the title contained.
    """

    kind: str
    fields: Mapping[str, str] = field(default_factory=dict[str, str])
    unread: int | bool | None = None
    confidence: Confidence = "high"


TitleParser = Callable[[str], TitleContext | None]


def _clean(fields: Mapping[str, str | None]) -> dict[str, str]:
    return {key: value for key, value in fields.items() if value}


def _truncated(text: str) -> bool:
    return text.endswith(_ELLIPSIS)


# --- cmux -------------------------------------------------------------------------------------


def parse_cmux(title: str) -> TitleContext | None:
    """A cmux window title is one token that is consistent with the selected workspace's name.

    It is a weak hint (``docs/spikes/0003-cmux.md`` question 4): not unique per session and not
    proven to follow a tab switch, so the confidence is low. The selected workspace and tab come
    from the second tier (step 40).
    """
    return TitleContext(kind="terminal", fields={"workspace": title}, confidence="low")


# --- JetBrains --------------------------------------------------------------------------------

_PROJECT_PART = re.compile(r"^(?P<project>.+?)(?: \[(?P<path>(?:~|/)[^\]]*)\])?$")
_FILE_PART = re.compile(r"^(?P<file>.+?)(?: \[(?P<module>[^\]]+)\])?$")
_VIEW_PREFIX = re.compile(r"^(?P<view>[A-Z][^:/\[\]]*): (?P<rest>\S.*)$")
_WELCOME = re.compile(r"^Welcome to \S")


def _jetbrains_parts(title: str) -> tuple[dict[str, str], str | None] | None:
    """Split ``<project>[ [<path>]][ – <right>]`` into project fields and the right-hand part."""
    if title.startswith("\u2013"):
        return None  # no project name
    left, sep, right = title.removesuffix(" \u2013").partition(_EN_DASH_SEP)
    match = _PROJECT_PART.match(left)
    if match is None:
        return None
    fields = _clean({"project": match["project"], "path": match["path"]})
    return fields, (right.strip() or None) if sep else None


def parse_jetbrains(title: str) -> TitleContext | None:
    """``<project> – <file> [<module>]``, ``<project> [<path>] – <file>`` or just ``<project>``.

    The file part may be a path outside the project (``~/…``), may carry a view prefix such as
    ``Repository Diff: <file>``, and is ellipsised when long. The welcome screen has no project.
    """
    if _WELCOME.match(title):
        return TitleContext(kind="ide", fields={"screen": "welcome"}, confidence="low")
    parts = _jetbrains_parts(title)
    if parts is None:
        return None
    fields, right = parts
    confidence: Confidence = "high"
    if right is not None:
        file_part = _FILE_PART.match(right)
        if file_part is None:
            return None
        file_text = file_part["file"]
        view = _VIEW_PREFIX.match(file_text)
        if view is not None:
            fields["view"] = view["view"]
            file_text = view["rest"]
            confidence = "medium"
        if file_text.startswith(("~/", "/")):
            fields["file_path"] = file_text
            file_text = file_text.rsplit("/", 1)[-1] or file_text
        fields["file"] = file_text
        if file_part["module"]:
            fields["module"] = file_part["module"]
        if _truncated(right):
            confidence = "medium"
    elif "project" not in fields:
        return None
    return TitleContext(kind="ide", fields=fields, confidence=confidence)


def parse_datagrip(title: str) -> TitleContext | None:
    """``<project> – <console or object>``. The part after the dash is the open console or table."""
    if _WELCOME.match(title):
        return TitleContext(kind="database", fields={"screen": "welcome"}, confidence="low")
    parts = _jetbrains_parts(title)
    if parts is None:
        return None
    fields, right = parts
    if right is not None:
        fields["console"] = right
    return TitleContext(
        kind="database",
        fields=fields,
        confidence="medium" if right is not None and _truncated(right) else "high",
    )


# --- Chrome -----------------------------------------------------------------------------------

_CHROME = re.compile(r"^(?:(?P<page>.*) - )?Google Chrome(?: - (?P<profile>\S.*))?$")


def parse_chrome(title: str) -> TitleContext | None:
    """``<page title> - Google Chrome - <profile>`` (the profile is ``<name> (<label>)``).

    The page title is the page's own and may hold ``" - "`` and numbers such as ``(3)``; those
    are never read as an unread marker.
    """
    match = _CHROME.match(title)
    if match is None:
        return None
    fields = _clean({"page_title": match["page"], "profile": match["profile"]})
    return TitleContext(kind="browser", fields=fields, confidence="high" if fields else "low")


# --- Teams ------------------------------------------------------------------------------------

_TEAMS_SUFFIX: Final = " | Microsoft Teams"


def parse_teams(title: str) -> TitleContext | None:
    """``<Section> | <Context> | Microsoft Teams``, for example ``Chat | <Last>, <First> | …``."""
    if not title.endswith(_TEAMS_SUFFIX) and title != "Microsoft Teams":
        return None
    body = title.removesuffix(_TEAMS_SUFFIX)
    if body == "Microsoft Teams":
        return TitleContext(kind="chat", fields={}, confidence="low")
    section, sep, context = body.partition(" | ")
    fields = _clean({"section": section.strip(), "context": context.strip() if sep else None})
    if section.strip() == "Chat" and fields.get("context"):
        fields["chat_partner"] = fields["context"]
    return TitleContext(kind="chat", fields=fields)


# --- Obsidian ---------------------------------------------------------------------------------

_OBSIDIAN = re.compile(r"^(?P<rest>.+) - Obsidian(?: v?\d[\w.\-]*)?$")


def parse_obsidian(title: str) -> TitleContext | None:
    """``<note> - <vault> - Obsidian <version>``; with no note open, ``<vault> - Obsidian …``."""
    match = _OBSIDIAN.match(title)
    if match is None:
        return None
    note, sep, vault = match["rest"].rpartition(" - ")
    fields = _clean({"note": note if sep else None, "vault": vault if sep else match["rest"]})
    return TitleContext(kind="notes", fields=fields)


# --- Outlook ----------------------------------------------------------------------------------

_OUTLOOK_QUOTED = re.compile(r'^(?P<folder>.+?) "(?P<account>[^"]+)"$')
_OUTLOOK_BULLET = re.compile(r"^(?P<folder>.+?) • (?P<account>\S.*)$")


def parse_outlook(title: str) -> TitleContext | None:
    """``<Folder> "<account>"`` (installed version); older builds wrote ``<Folder> • <account>``."""
    for pattern in (_OUTLOOK_QUOTED, _OUTLOOK_BULLET):
        match = pattern.match(title)
        if match is not None:
            return TitleContext(
                kind="mail",
                fields={"folder": match["folder"], "account": match["account"]},
            )
    return None


# --- Slack ------------------------------------------------------------------------------------

_SLACK_SUFFIX: Final = " - Slack"
_SLACK_NEW_ITEMS = re.compile(r"^(?P<count>\d+) new items?$")
_SLACK_CHANNEL = re.compile(r"^(?P<marker>! )?(?P<name>.+?)(?: \((?P<type>[A-Za-z][\w ]*)\))?$")


def parse_slack(title: str) -> TitleContext | None:
    """``[! ]<name> (<Channel|DM|…>) - <workspace> - [<N> new items - ]Slack``.

    Confirmed unread markers (A15): the ``! `` prefix, and the ``N new items`` segment, which gives
    the count. The ``* `` prefix that older Slack builds use is not confirmed on the installed
    version, so it is not read as a marker.
    """
    if not title.endswith(_SLACK_SUFFIX):
        return None
    parts = title.removesuffix(_SLACK_SUFFIX).split(" - ")
    count: int | None = None
    if len(parts) >= 3:
        items = _SLACK_NEW_ITEMS.match(parts[-1])
        if items is not None:
            count = int(items["count"])
            parts.pop()
    name_part = parts[0]
    match = _SLACK_CHANNEL.match(name_part)
    if match is None or not match["name"].strip():
        return None
    fields = _clean(
        {
            "channel": match["name"].strip(),
            "channel_type": match["type"].lower() if match["type"] else None,
            "workspace": " - ".join(parts[1:]).strip() or None,
        }
    )
    unread: int | bool | None = count if count else None
    if unread is None and match["marker"]:
        unread = True
    return TitleContext(kind="chat", fields=fields, unread=unread)


# --- The table --------------------------------------------------------------------------------

PARSERS: Final[Mapping[str, TitleParser]] = {
    BUNDLE_CMUX: parse_cmux,
    **dict.fromkeys(JETBRAINS_BUNDLES, parse_jetbrains),
    BUNDLE_DATAGRIP: parse_datagrip,
    BUNDLE_CHROME: parse_chrome,
    BUNDLE_TEAMS: parse_teams,
    BUNDLE_OBSIDIAN: parse_obsidian,
    BUNDLE_OUTLOOK: parse_outlook,
    BUNDLE_SLACK: parse_slack,
}


def parse_title(bundle_id: str, title: str) -> TitleContext | None:
    """Parse ``title`` with the parser registered for ``bundle_id``; ``None`` when there is none."""
    parser = PARSERS.get(bundle_id)
    if parser is None:
        return None
    text = title.strip()[:MAX_TITLE_CHARS]
    if not text:
        return None
    return parser(text)
