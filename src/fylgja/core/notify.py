"""Attention alerts: the pure decision core (``docs/decisions/0055``).

Given what ``/now`` shows as waiting, what is on screen, whether the person is away, the time and
the previous state, :func:`decide` says whether to send an alert now, with which title and
message, and when to look again. Nothing here reads a clock, does I/O or runs a task: the caller
passes monotonic seconds (``Snapshot.now``) and the aware wall time (``Snapshot.wall_now``), and
carries the returned :class:`NotifyState` to the next evaluation.

**Input** (R1). :func:`candidates` turns the ``waiting[]`` entries of
:func:`fylgja.core.shown.shown_waiting` into :class:`Candidate` values; :func:`decide` ignores
every entry whose ``kind`` is not in ``[notify] kinds``.

**Episodes** (R2). A session entry is one episode per ``(target_kind, target, kind, since)``: a new
``since`` is a new episode. A project or window entry is one episode per ``(target_kind, target,
kind)`` while it is listed, because its ``since`` can move within one occurrence; the episode keeps
the latest ``since`` it saw as ``last_since``.

**Grace** (R3) counts from the entry's ``since``. At the first sighting the age
``wall_now - since`` is clamped to ``[0, grace_seconds]`` (an unknown or future ``since`` is age
0), and the episode is due at ``first_seen + max(grace_seconds - age, floor)`` on the monotonic
clock, where ``floor = min(grace_seconds, LATE_SIGHT_FLOOR)``. Sleep therefore never makes an
episode due early, and an entry first seen long after its ``since`` (a restart, a collector that
came back) is due :data:`LATE_SIGHT_FLOOR` seconds after that sighting.

**Families and the screen** (R4, R5, R6). A session's family is the top of its ``parent_key``
chain among the shown sessions; a project or window is its own family. While the person is not
away, every family with a shown live session in the focused window is on screen, and its alerts
(first alerts and reminders) are held until it leaves the screen.

**Reminders** (R7): at most one per episode, ``reminder_seconds`` after its first alert; ``0``
turns them off. **Channel** (R8): ``away_command`` while away when it is set, else
``desk_command`` when it is set, else none, and due episodes wait. **Coalescing** (R9): every
episode due in one evaluation goes into one :class:`Alert`, ordered by ``since`` (unknown last),
then target.

**End** (R10). An episode whose entry leaves closes silently and is remembered for
:data:`TOMBSTONE_SECONDS`. A return within that time continues the old state when it is the same
occurrence: the same key for a session, a ``since`` that is unknown or not later than
``last_since`` for a project or window. Anything else is a new episode.

**Text** (R11) never names a collector: the source label is the session ``kind`` with its first
letter upper-cased. Every value is sanitised (control characters become spaces, whitespace runs
collapse), titles are clipped to 64 characters and messages to 200. **Substitution** (R12):
:func:`render_argv` makes one pass over each argv element; ``{key}`` is empty unless the alert
names one session family whose key is safe inside a shell string.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from fylgja.config import LOOPBACK_HOST
from fylgja.core.waiting import WaitingEntry
from fylgja.store.rows import SessionRow

if TYPE_CHECKING:
    from fylgja.config import NotifyConfig

__all__ = [
    "LATE_SIGHT_FLOOR",
    "MAX_MESSAGE",
    "MAX_TITLE",
    "TOMBSTONE_SECONDS",
    "Alert",
    "Candidate",
    "Channel",
    "Decision",
    "Episode",
    "EpisodeKey",
    "NotifyState",
    "Snapshot",
    "candidates",
    "decide",
    "on_screen",
    "render_argv",
    "url_for",
]

LATE_SIGHT_FLOOR: Final = 5.0
"""Seconds an entry first seen past its grace still waits (R3): time for the collectors' first
snapshots to end rows that ended while the daemon was down."""

TOMBSTONE_SECONDS: Final = 86_400.0
"""How long a closed episode is remembered (R10), in monotonic seconds."""

MAX_TITLE: Final = 64
MAX_MESSAGE: Final = 200
_LABELS_SHOWN: Final = 3
"""How many labels a coalesced message names before ``+k more``."""
_MAX_FAMILY_STEPS: Final = 8

TITLE: Final = "Fylgja"
REMINDER_TITLE: Final = "Fylgja: still waiting"

_VERBS: Final[Mapping[str, str]] = {
    "permission": "needs permission",
    "input": "waits for input",
    "turn_done": "finished a turn",
    "build_failed": "has a failed build",
    "review_requested": "has a review request",
    "unread": "has unread messages",
}
_TARGET_LABELS: Final[Mapping[str, str]] = {"project": "A project", "window": "A window"}
_AGENT: Final = "Agent"

_SESSION: Final = "session"
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_PLACEHOLDER = re.compile(r"\{(title|message|url|key)\}")
_SAFE_KEY = re.compile(r"[A-Za-z0-9:@._-]+")
_NO_SINCE: Final = datetime.min.replace(tzinfo=UTC)

Channel = Literal["desk", "away"]


# --------------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One shown waiting entry with what the alert text and the hold need."""

    entry: WaitingEntry
    family: str
    """The family root (R4): a session key, or the target itself for a project or window."""
    source: str
    """The source label (R11): ``Claude``, ``Norn``, ``Agent``, ``A project`` or ``A window``."""
    folder: str | None
    """The last component of the session's or project's path; ``None`` when unknown."""


def _source_label(kind: str) -> str:
    if not kind or not kind[0].isalnum():
        return _AGENT
    return kind[0].upper() + kind[1:]


def _folder(path: str | None) -> str | None:
    if not path:
        return None
    return path.rstrip("/").rpartition("/")[2] or None


def _family(key: str, by_key: Mapping[str, SessionRow]) -> str:
    """The top of ``key``'s ``parent_key`` chain among ``by_key`` (R4)."""
    current = key
    seen = {key}
    for _ in range(_MAX_FAMILY_STEPS):
        row = by_key.get(current)
        parent = None if row is None else row.parent_key
        if parent is None or parent not in by_key or parent in seen:
            break
        seen.add(parent)
        current = parent
    return current


def candidates(
    entries: Iterable[WaitingEntry], sessions: Iterable[SessionRow]
) -> tuple[Candidate, ...]:
    """The entries with their family root, source label and folder (R4, R11), in input order.

    ``sessions`` are the shown sessions (:func:`fylgja.core.shown.shown_sessions`).
    """
    by_key = {row.session_key: row for row in sessions}
    found: list[Candidate] = []
    for entry in entries:
        if entry.target_kind == _SESSION:
            row = by_key.get(entry.target)
            found.append(
                Candidate(
                    entry=entry,
                    family=_family(entry.target, by_key),
                    source=_AGENT if row is None else _source_label(row.kind),
                    folder=None if row is None else _folder(row.project_path),
                )
            )
        else:
            found.append(
                Candidate(
                    entry=entry,
                    family=entry.target,
                    source=_TARGET_LABELS.get(entry.target_kind, _AGENT),
                    folder=_folder(entry.target) if entry.target_kind == "project" else None,
                )
            )
    return tuple(found)


def on_screen(window_id: str | None, sessions: Iterable[SessionRow]) -> frozenset[str]:
    """The families with a shown live session in the focused window (R5).

    The caller passes ``None`` while the person is away, no window has focus or the focused window
    is private; nothing is on screen then.
    """
    if window_id is None:
        return frozenset()
    shown = list(sessions)
    by_key = {row.session_key: row for row in shown}
    return frozenset(
        _family(row.session_key, by_key)
        for row in shown
        if row.window_id == window_id and row.ended_at is None
    )


@dataclass(frozen=True)
class Snapshot:
    """What one evaluation sees."""

    now: float
    """Monotonic seconds: every deadline is on this clock."""
    wall_now: datetime
    """The aware wall time, used only for the age of a first sighting (R3)."""
    candidates: tuple[Candidate, ...]
    on_screen: frozenset[str]
    """The family roots on screen (:func:`on_screen`)."""
    away: bool
    """The person is idle or the screen is locked."""


# --------------------------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EpisodeKey:
    """The identity of an episode (R2). ``since`` is ``None`` for a project or window target."""

    target_kind: str
    target: str
    kind: str
    since: datetime | None


@dataclass(frozen=True)
class Episode:
    """One occurrence of an entry, in monotonic seconds."""

    first_seen: float
    due_at: float
    """When the first alert is due; fixed at the first sighting (R3)."""
    last_since: datetime | None
    """The latest ``since`` seen (R2, R10)."""
    sent_at: float | None = None
    """When the first alert went out (or was attempted); ``None`` while unannounced."""
    reminded: bool = False
    closed_at: float | None = None
    """When the entry left the list; a closed episode is a tombstone (R10)."""


@dataclass(frozen=True)
class NotifyState:
    """The episodes carried from one evaluation to the next. :func:`decide` never mutates it."""

    episodes: Mapping[EpisodeKey, Episode] = field(default_factory=dict)


@dataclass(frozen=True)
class Alert:
    """One alert to send (R9, R11)."""

    channel: Channel
    reminder_only: bool
    """Every included episode is a reminder."""
    title: str
    message: str
    key: str
    """The ``{key}`` value (R12): one session family's key, or empty."""
    episodes: tuple[EpisodeKey, ...]
    """The included episodes, in R9 order."""
    families: int
    """The number of distinct families."""
    kinds: tuple[str, ...]
    """The distinct waiting kinds, in R9 order."""
    session_keys: tuple[str, ...]
    """The session targets, in R9 order."""


@dataclass(frozen=True)
class Decision:
    """The result of one evaluation."""

    alert: Alert | None
    state: NotifyState
    next_check: float | None
    """The earliest future monotonic time at which something falls due; ``None`` for none."""


# --------------------------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------------------------


def _clean(text: str) -> str:
    """Control characters become spaces and whitespace runs collapse (R11)."""
    return " ".join(_CONTROL.sub(" ", text).split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _one_family(first: Candidate, detail: str) -> str:
    source = _clean(first.source)
    if detail == "kind":
        return f"{source} {_VERBS[first.entry.kind]}"
    reason = _clean(first.entry.reason)
    folder = _clean(first.folder or "")
    return f"{source} in {folder}: {reason}" if folder else f"{source}: {reason}"


def _many_families(included: Sequence[Candidate], families: int, detail: str) -> str:
    noun = "sessions" if all(c.entry.target_kind == _SESSION for c in included) else "items"
    text = f"{families} {noun} wait for you"
    if detail == "kind":
        return text
    labels: list[str] = []
    seen_families: set[str] = set()
    for candidate in included:
        if candidate.family in seen_families:
            continue
        seen_families.add(candidate.family)
        label = _clean(candidate.folder or "") or _clean(candidate.source)
        if label not in labels:
            labels.append(label)
    text += ": " + ", ".join(labels[:_LABELS_SHOWN])
    if len(labels) > _LABELS_SHOWN:
        text += f", +{len(labels) - _LABELS_SHOWN} more"
    return text


def _safe_key(first: Candidate, families: int) -> str:
    """The ``{key}`` value (R12): empty unless one session family with a shell-safe key."""
    key = first.entry.target
    if (
        families != 1
        or first.entry.target_kind != _SESSION
        or _SAFE_KEY.fullmatch(key) is None
        or key.startswith("-")
    ):
        return ""
    return key


def render_argv(command: Sequence[str], values: Mapping[str, str]) -> list[str]:
    """``command`` with ``{title}``, ``{message}``, ``{url}`` and ``{key}`` substituted (R12).

    One pass per element: substituted text is never expanded again, any other text (other braces
    included) stays as it is, and the element count never changes. A name missing from ``values``
    becomes empty.
    """
    return [_PLACEHOLDER.sub(lambda match: values.get(match[1], ""), part) for part in command]


def url_for(port: int) -> str:
    """The ``{url}`` value (R12): the navigator's recall tab, or empty for port 0."""
    return f"http://{LOOPBACK_HOST}:{port}/#recall" if port else ""


# --------------------------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------------------------


def _key(entry: WaitingEntry) -> EpisodeKey:
    since = entry.since if entry.target_kind == _SESSION else None
    return EpisodeKey(entry.target_kind, entry.target, entry.kind, since)


def _latest(old: datetime | None, new: datetime | None) -> datetime | None:
    if old is None:
        return new
    if new is None:
        return old
    return max(old, new)


def _first_sighting(entry: WaitingEntry, snapshot: Snapshot, grace: float) -> Episode:
    """A new episode, due ``grace`` after its ``since`` with the late-sighting floor (R3)."""
    age = 0.0
    if entry.since is not None:
        age = min(max((snapshot.wall_now - entry.since).total_seconds(), 0.0), grace)
    floor = min(grace, LATE_SIGHT_FLOOR)
    return Episode(
        first_seen=snapshot.now,
        due_at=snapshot.now + max(grace - age, floor),
        last_since=entry.since,
    )


def _same_occurrence(key: EpisodeKey, closed: Episode, since: datetime | None) -> bool:
    """Whether a return continues a closed episode (R10).

    A session's key holds its ``since``, so a matching key is the same occurrence. A project or
    window returns as the same occurrence unless its ``since`` is later than ``last_since``.
    """
    if key.target_kind == _SESSION:
        return True
    return since is None or closed.last_since is None or since <= closed.last_since


def _order(candidate: Candidate) -> tuple[bool, datetime, str]:
    since = candidate.entry.since
    return (since is None, _NO_SINCE if since is None else since, candidate.entry.target)


def _channel(snapshot: Snapshot, settings: NotifyConfig) -> Channel | None:
    if snapshot.away and settings.away_command:
        return "away"
    if settings.desk_command:
        return "desk"
    return None


def decide(snapshot: Snapshot, state: NotifyState, settings: NotifyConfig) -> Decision:
    """One evaluation (R1–R11): the alert to send now, the new state and the next deadline.

    Every included episode is marked announced (or reminded) in the returned state, whatever the
    send's outcome (R13).
    """
    now = snapshot.now
    kinds = set(settings.kinds)
    grace = settings.grace_seconds
    reminder = settings.reminder_seconds

    episodes: dict[EpisodeKey, Episode] = {
        key: episode
        for key, episode in state.episodes.items()
        if episode.closed_at is None or now - episode.closed_at < TOMBSTONE_SECONDS
    }

    present: dict[EpisodeKey, Candidate] = {}
    for candidate in snapshot.candidates:
        entry = candidate.entry
        if entry.kind not in kinds:
            continue
        key = _key(entry)
        present[key] = candidate
        episode = episodes.get(key)
        if episode is None:
            episodes[key] = _first_sighting(entry, snapshot, grace)
        elif episode.closed_at is None:
            episodes[key] = replace(episode, last_since=_latest(episode.last_since, entry.since))
        elif _same_occurrence(key, episode, entry.since):
            episodes[key] = replace(
                episode, closed_at=None, last_since=_latest(episode.last_since, entry.since)
            )
        else:
            episodes[key] = _first_sighting(entry, snapshot, grace)

    for key, episode in list(episodes.items()):
        if episode.closed_at is None and key not in present:
            episodes[key] = replace(episode, closed_at=now)

    def held(candidate: Candidate) -> bool:
        return not snapshot.away and candidate.family in snapshot.on_screen

    channel = _channel(snapshot, settings)
    due: list[tuple[Candidate, EpisodeKey, bool]] = []
    if channel is not None:
        for key, candidate in present.items():
            episode = episodes[key]
            if held(candidate):
                continue
            if episode.sent_at is None:
                if episode.due_at <= now:
                    due.append((candidate, key, False))
            elif not episode.reminded and reminder > 0 and episode.sent_at + reminder <= now:
                due.append((candidate, key, True))

    alert: Alert | None = None
    if channel is not None and due:
        due.sort(key=lambda item: _order(item[0]))
        for _, key, is_reminder in due:
            episode = episodes[key]
            episodes[key] = (
                replace(episode, reminded=True) if is_reminder else replace(episode, sent_at=now)
            )
        alert = _alert(channel, due, settings)

    next_check: float | None = None
    for key, candidate in present.items():
        episode = episodes[key]
        if held(candidate):
            continue
        if episode.sent_at is None:
            moment: float | None = episode.due_at
        elif not episode.reminded and reminder > 0:
            moment = episode.sent_at + reminder
        else:
            moment = None
        if moment is not None and moment > now and (next_check is None or moment < next_check):
            next_check = moment

    return Decision(alert=alert, state=NotifyState(episodes=episodes), next_check=next_check)


def _alert(
    channel: Channel, due: Sequence[tuple[Candidate, EpisodeKey, bool]], settings: NotifyConfig
) -> Alert:
    included = [candidate for candidate, _, _ in due]
    first = included[0]
    families = len({candidate.family for candidate in included})
    reminder_only = all(is_reminder for _, _, is_reminder in due)
    detail = settings.away_detail if channel == "away" else settings.desk_detail
    message = (
        _one_family(first, detail) if families == 1 else _many_families(included, families, detail)
    )
    return Alert(
        channel=channel,
        reminder_only=reminder_only,
        title=_clip(_clean(REMINDER_TITLE if reminder_only else TITLE), MAX_TITLE),
        message=_clip(_clean(message), MAX_MESSAGE),
        key=_safe_key(first, families),
        episodes=tuple(key for _, key, _ in due),
        families=families,
        kinds=tuple(dict.fromkeys(candidate.entry.kind for candidate in included)),
        session_keys=tuple(
            dict.fromkeys(
                candidate.entry.target
                for candidate in included
                if candidate.entry.target_kind == _SESSION
            )
        ),
    )
