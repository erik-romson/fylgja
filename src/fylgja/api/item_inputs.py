"""The navigation items, built from the ``/now`` picture (``docs/decisions/0051``).

Like :mod:`fylgja.api.summary_inputs`, the builder starts from ``/now``, so every string it uses
is already redacted and limited to the collectors that are available, and an item cannot drift
from what the page shows. Pure: no FastAPI, no I/O and no clock (``now.generated_at`` is the time).

**Grouping.**

* A live session with a ``project_path`` is a place of ``folder:<project_path>``.
* An open window with a folder (``folders``, from ``WindowFolders.snapshot``) is a place of
  ``folder:<git root>``; one without a folder is a place of ``app:<bundle id>`` (else the app
  name).
* A span of a window that is no longer open makes a closed place of ``folder:<span.project_path>``,
  else ``app:<span.app_bundle_id>``. A span of an open window joins that place.

An item is listed when it has an open place or focus in the last ``days``. Worktrees are separate
keys and never merged. The role of a place comes from its kind (a session is an ``agent``) or from
the kinds of a window's context items, never from a collector name.

**The answers** (``docs/api/items.md``). :func:`build_items_answer` turns a ranking
(:class:`~fylgja.core.daemon.RankResult`) into ``fylgja.items/1``: ``q`` and ``limit`` apply after
ranking (``total`` counts before both, ``shown`` after), the day groups, the Go targets, the
``while_away`` block and the last work session are computed here, so a client only renders. The
cached folder summaries (``story``, ``docs/decisions/0053``) are given to it; it never asks for
one.
:func:`build_rankers_answer` gives ``fylgja.rankers/1`` with the choices a client offers. Both are
pure and log nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from fylgja.api.items_models import (
    ActivityInfo,
    AwayChange,
    DayGroup,
    ItemPlace,
    Items,
    ItemStory,
    LastSession,
    NavItem,
    RankerChoice,
    RankerInfo,
    Rankers,
    ScorePart,
)
from fylgja.api.items_models import GoTarget as GoModel
from fylgja.api.now_models import Now, Project, Session, Window
from fylgja.config import ItemsConfig
from fylgja.core.activity import Activity
from fylgja.core.daemon import RankResult
from fylgja.core.items import (
    GoTarget,
    Item,
    ItemKind,
    Place,
    PlaceKind,
    Role,
    choose_go,
    folder_name,
    item_id,
    order_places,
    role_of_kinds,
    ticket_keys,
)
from fylgja.core.waiting import start_of_day
from fylgja.rankers.base import Ranked
from fylgja.rankers.registry import RegisteredRanker
from fylgja.store.rows import SpanRow

__all__ = [
    "DEFAULT_DAYS",
    "MAX_DAYS",
    "MAX_LIMIT",
    "MAX_QUERY",
    "SUMMARY_SEPARATOR",
    "build_items",
    "build_items_answer",
    "build_rankers_answer",
    "item_matches",
]

DEFAULT_DAYS = 14
MAX_DAYS: Final = 14
"""The most days ``?days`` may look back (the span window holds 14)."""
MAX_LIMIT: Final = 500
"""The largest ``?limit``."""
MAX_QUERY: Final = 200
"""The longest ``?q`` (and ``?rank``) in characters."""
SUMMARY_SEPARATOR = " · "
FOLDER_PREFIX = "folder:"
APP_PREFIX = "app:"
_GO_ROLES = ("ide", "agent", "terminal")
_NEXT = "next"


@dataclass
class _Draft:
    """A place while it is being gathered."""

    kind: PlaceKind
    ref: str
    role: Role
    label: str
    title: str | None
    workspace: str | None
    open: bool
    waiting: bool = False
    last: datetime | None = None
    one_liner: str | None = None
    branch: str | None = None
    file: str | None = None

    def place(self) -> Place:
        return Place(
            kind=self.kind,
            ref=self.ref,
            role=self.role,
            label=self.label,
            title=self.title,
            workspace=self.workspace,
            open=self.open,
            waiting=self.waiting,
            last_focus_at=self.last,
        )


@dataclass
class _Acc:
    """An item while it is being gathered."""

    key: str
    kind: ItemKind
    path: str | None
    name: str | None = None
    drafts: dict[tuple[str, str], _Draft] = field(default_factory=dict)
    last: datetime | None = None
    seconds: float = 0.0
    state: set[str] = field(default_factory=set)


def _newest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None or b is None:
        return a or b
    return max(a, b)


def _walk(sessions: Iterable[Session]) -> Iterable[Session]:
    for session in sessions:
        yield session
        yield from _walk(session.children or ())


def _file_of(window: Window) -> str | None:
    """The file an IDE window shows, when its context items say so."""
    for item in window.context or ():
        value = (item.data or {}).get("file")
        if isinstance(value, str) and value:
            return value
    return None


class _Builder:
    def __init__(self, now: Now, folders: Mapping[str, str], config: ItemsConfig, days: int):
        self.now = now
        self.moment = now.generated_at
        self.since = now.generated_at - timedelta(days=days)
        self.day_start = start_of_day(now.generated_at)
        self.folders = folders
        self.config = config
        self.accs: dict[str, _Acc] = {}
        self.window_keys: dict[str, str] = {}
        self.session_keys: dict[str, str] = {}
        self.workspace_of_session: dict[str, str] = {}
        self.waiting_by_target: dict[str, set[str]] = {}

    # -- accumulators -----------------------------------------------------------------------

    def acc(self, key: str) -> _Acc:
        found = self.accs.get(key)
        if found is None:
            if key.startswith(FOLDER_PREFIX):
                path = key[len(FOLDER_PREFIX) :]
                found = _Acc(key, "folder", path, name=folder_name(path))
            else:
                found = _Acc(key, "app", None)
            self.accs[key] = found
        return found

    def draft(self, acc: _Acc, draft: _Draft) -> None:
        acc.drafts.setdefault((draft.kind, draft.ref), draft)

    # -- gathering --------------------------------------------------------------------------

    def gather(self, spans: Sequence[SpanRow]) -> None:
        for waiting in self.now.waiting or ():
            self.waiting_by_target.setdefault(waiting.target, set()).add(waiting.kind)
        for workspace in self.now.workspaces or ():
            for ref in workspace.sessions:
                self.workspace_of_session.setdefault(ref, workspace.name)
        self._sessions()
        self._windows()
        for span in sorted(
            spans,
            key=lambda s: (s.started_at, s.window_id or "", s.session_key or "", s.title or ""),
        ):
            self._span(span)
        self._states()

    def _sessions(self) -> None:
        sessions = sorted(_walk(self.now.sessions or ()), key=lambda s: s.key)
        for session in sessions:
            path = session.project_path
            if not path:
                continue
            key = FOLDER_PREFIX + path
            kinds = self.waiting_by_target.get(session.key, set())
            self.session_keys[session.key] = key
            self.draft(
                self.acc(key),
                _Draft(
                    kind="session",
                    ref=session.key,
                    role="agent",
                    label=session.kind,
                    title=session.one_liner or session.title,
                    workspace=self.workspace_of_session.get(session.key),
                    open=True,
                    waiting=bool(kinds),
                    one_liner=session.one_liner,
                    branch=session.branch,
                ),
            )

    def _windows(self) -> None:
        pairs = [(w.name, window) for w in self.now.workspaces or () for window in w.windows]
        for workspace, window in sorted(pairs, key=lambda pair: (pair[0], pair[1].id)):
            folder = self.folders.get(window.id)
            if folder:
                key = FOLDER_PREFIX + folder
            elif window.bundle_id or window.app:
                key = APP_PREFIX + (window.bundle_id or window.app or "")
            else:
                continue
            acc = self.acc(key)
            label = window.app or window.bundle_id or "window"
            if acc.kind == "app" and acc.name is None:
                acc.name = label
            role = role_of_kinds(item.kind for item in window.context or ())
            self.window_keys[window.id] = key
            self.draft(
                acc,
                _Draft(
                    kind="window",
                    ref=window.id,
                    role=role,
                    label=label,
                    title=window.title,
                    workspace=workspace,
                    open=True,
                    waiting=window.id in self.waiting_by_target,
                    file=_file_of(window) if role == "ide" else None,
                ),
            )

    def _span_key(self, span: SpanRow) -> str | None:
        if span.window_id and span.window_id in self.window_keys:
            return self.window_keys[span.window_id]
        if span.project_path:
            return FOLDER_PREFIX + span.project_path
        if span.app_bundle_id:
            return APP_PREFIX + span.app_bundle_id
        return None

    def _span(self, span: SpanRow) -> None:
        end = span.ended_at or self.moment
        if end < self.since or span.started_at > self.moment:
            return
        key = self._span_key(span)
        if key is None:
            return
        acc = self.acc(key)
        acc.last = _newest(acc.last, end)
        begin = max(span.started_at, self.day_start)
        clipped_end = min(end, self.moment)
        if clipped_end > begin:
            acc.seconds += (clipped_end - begin).total_seconds()
        if span.window_id:
            open_key = self.window_keys.get(span.window_id)
            if open_key is not None:
                draft = self.accs[open_key].drafts.get(("window", span.window_id))
                if draft is not None:
                    draft.last = _newest(draft.last, end)
            else:
                self._closed(acc, span, end)
        session_key = span.session_key
        if session_key and session_key in self.session_keys:
            draft = self.accs[self.session_keys[session_key]].drafts.get(("session", session_key))
            if draft is not None:
                draft.last = _newest(draft.last, end)

    def _closed(self, acc: _Acc, span: SpanRow, end: datetime) -> None:
        assert span.window_id is not None
        draft = acc.drafts.get(("window", span.window_id))
        if draft is None:
            draft = _Draft(
                kind="window",
                ref=span.window_id,
                role="app",
                label=span.app_bundle_id or "window",
                title=span.title,
                workspace=span.workspace,
                open=False,
                last=end,
            )
            acc.drafts[("window", span.window_id)] = draft
        elif not draft.open and (draft.last is None or end >= draft.last):
            draft.last = end
            draft.title = span.title
            draft.workspace = span.workspace

    def _states(self) -> None:
        projects: dict[str, Project] = {p.path: p for p in self.now.projects or ()}
        workspace_next = {
            w.name for w in self.now.workspaces or () for t in w.todos or () if t.status == _NEXT
        }
        for acc in self.accs.values():
            for draft in acc.drafts.values():
                if draft.open and draft.waiting:
                    acc.state.update(self.waiting_by_target.get(draft.ref, ()))
                if draft.open and draft.workspace in workspace_next:
                    acc.state.add("todo_next")
            if acc.path is None:
                continue
            acc.state.update(self.waiting_by_target.get(acc.path, ()))
            project = projects.get(acc.path)
            if project is None:
                continue
            if any(t.status == _NEXT for t in project.todos or ()):
                acc.state.add("todo_next")
            if project.dirty:
                acc.state.add("dirty")
            if project.pr is not None and (project.pr.state or "").lower() == "open":
                acc.state.add("pr_open")

    # -- building ---------------------------------------------------------------------------

    def build(self) -> tuple[Item, ...]:
        projects: dict[str, Project] = {p.path: p for p in self.now.projects or ()}
        items: list[Item] = []
        for key in sorted(self.accs):
            acc = self.accs[key]
            by_place = acc.drafts
            places = order_places(d.place() for d in by_place.values())
            if not any(p.open for p in places) and acc.last is None:
                continue
            ordered = [by_place[(p.kind, p.ref)] for p in places]
            last = acc.last
            for place in places:
                last = _newest(last, place.last_focus_at)
            project = projects.get(acc.path) if acc.path is not None else None
            name = acc.name or key[len(APP_PREFIX) :]
            tickets = ticket_keys(
                [
                    name,
                    *([project.branch] if project and project.branch else []),
                    *(d.branch for d in ordered if d.branch),
                    *(d.title for d in ordered if d.title),
                ],
                self.config.ticket_prefixes,
            )
            go_by_role: dict[str, GoTarget] = {}
            for role in _GO_ROLES:
                target = choose_go([p for p in places if p.role == role])
                if target is not None:
                    go_by_role[role] = target
            items.append(
                Item(
                    id=item_id(key),
                    key=key,
                    kind=acc.kind,
                    name=name,
                    path=acc.path,
                    group=project.group if project else None,
                    tickets=tickets,
                    places=places,
                    state=frozenset(acc.state),
                    last_focus_at=last,
                    seconds_today=acc.seconds,
                    summary=_summary(tickets, project, ordered),
                    go=choose_go(places),
                    go_by_role=go_by_role,
                )
            )
        return tuple(items)


def _summary(tickets: Sequence[str], project: Project | None, ordered: Sequence[_Draft]) -> str:
    """Tickets, branch, ``PR #n <state>``, ``dirty``, the newest IDE file and the first session
    one-liner, in that order, joined with ``·``. A part with nothing to say is left out."""
    parts: list[str] = []
    if tickets:
        parts.append(", ".join(tickets))
    branch = project.branch if project and project.branch else None
    if branch is None:
        branch = next((d.branch for d in ordered if d.branch), None)
    if branch:
        parts.append(branch)
    if project is not None and project.pr is not None:
        state = (project.pr.state or "").lower()
        parts.append(f"PR #{project.pr.number} {state}".rstrip())
    if project is not None and project.dirty:
        parts.append("dirty")
    file = next((d.file for d in ordered if d.file), None)
    if file:
        parts.append(file)
    one_liner = next((d.one_liner for d in ordered if d.kind == "session" and d.one_liner), None)
    if one_liner:
        parts.append(f'"{one_liner}"')
    return SUMMARY_SEPARATOR.join(parts)


def build_items(
    now: Now,
    folders: Mapping[str, str],
    window: Sequence[SpanRow],
    items: ItemsConfig,
    *,
    days: int = DEFAULT_DAYS,
) -> tuple[Item, ...]:
    """The navigation items of ``now``, sorted by key (a ranker orders them).

    ``folders`` maps a window id to the git root of its working folder; ``window`` is the span
    window (an open span has ``ended_at`` ``None``); ``items`` supplies the ticket prefixes.
    Identical for any order of the inputs."""
    builder = _Builder(now, folders, items, days)
    builder.gather(window)
    return builder.build()


# --------------------------------------------------------------------------------------------
# The answers: fylgja.items/1 and fylgja.rankers/1
# --------------------------------------------------------------------------------------------

_GO_FIELDS: Final = (("ide", "go_ide"), ("agent", "go_agent"), ("terminal", "go_terminal"))
"""The role of a Go target and the ``NavItem`` field that carries it."""


def _round(value: float) -> float:
    return round(value, 3)


def _go(target: GoTarget | None) -> GoModel | None:
    return None if target is None else GoModel(kind=target.kind, ref=target.ref)


def _place(place: Place) -> ItemPlace:
    return ItemPlace(
        kind=place.kind,
        ref=place.ref,
        role=place.role,
        label=place.label,
        title=place.title,
        workspace=place.workspace,
        open=place.open,
        waiting=place.waiting,
        last_focus_at=place.last_focus_at,
    )


def _nav_item(item: Item, ranked: Ranked, story: ItemStory | None = None) -> NavItem:
    by_role = {field: _go(item.go_by_role.get(role)) for role, field in _GO_FIELDS}
    return NavItem(
        id=item.id,
        key=item.key,
        kind=item.kind,
        name=item.name,
        path=item.path,
        group=item.group,
        tickets=list(item.tickets),
        score=_round(ranked.score),
        explanation=ranked.explanation,
        parts=[ScorePart(name=part.name, value=_round(part.value)) for part in ranked.parts],
        places=[_place(place) for place in item.places],
        state=sorted(item.state),
        last_focus_at=item.last_focus_at,
        seconds_today=_round(item.seconds_today),
        summary=item.summary,
        go=_go(item.go),
        go_ide=by_role["go_ide"],
        go_agent=by_role["go_agent"],
        go_terminal=by_role["go_terminal"],
        story=story,
    )


def item_matches(item: Item, needle: str, story: str | None = None) -> bool:
    """``needle`` (case-folded) is in the item's name, a ticket, its path, its summary, its
    ``story`` text or a place title, case-insensitively."""
    texts = [
        item.name,
        *item.tickets,
        item.path or "",
        item.summary,
        story or "",
        *(place.title for place in item.places if place.title),
    ]
    return any(needle in text.casefold() for text in texts)


def _local_day(moment: datetime) -> str:
    return moment.astimezone().date().isoformat()


def _days(items: Sequence[Item], today: str) -> list[DayGroup]:
    """The items by the local day of their last focus (never focused: ``today``), newest day
    first, each day in the order of ``items``."""
    groups: dict[str, list[str]] = {}
    for item in items:
        day = today if item.last_focus_at is None else _local_day(item.last_focus_at)
        groups.setdefault(day, []).append(item.id)
    return [DayGroup(day=day, items=groups[day]) for day in sorted(groups, reverse=True)]


def _activity_info(activity: Activity) -> ActivityInfo:
    away = activity.away
    return ActivityInfo(
        working=activity.working,
        away_since=None if away is None else away.since,
        away_until=None if away is None else away.until,
        session_started_at=None if activity.current is None else activity.current.start,
        active_seconds_session=_round(activity.active_in_current),
    )


def _owner_of_target(items: Sequence[Item]) -> dict[str, Item]:
    """The item of each waiting target: a place's session key or window id, or a folder path."""
    owners: dict[str, Item] = {}
    for item in sorted(items, key=lambda i: i.key):
        for place in item.places:
            owners.setdefault(place.ref, item)
        if item.path is not None:
            owners.setdefault(item.path, item)
    return owners


def _while_away(result: RankResult, now: Now) -> list[AwayChange]:
    """The waiting entries that began during the latest absence, on items not focused since it
    began, in ``/now`` order (most urgent first)."""
    away = result.activity.away
    if away is None:
        return []
    end = result.now if away.until is None else away.until
    owners = _owner_of_target(result.items)
    changes: list[AwayChange] = []
    for waiting in now.waiting or ():
        since = waiting.since
        if since is None or not away.since <= since <= end:
            continue
        item = owners.get(waiting.target)
        if item is None:
            continue
        if item.last_focus_at is not None and item.last_focus_at > away.since:
            continue
        changes.append(
            AwayChange(
                item=item.id,
                name=item.name,
                kind=waiting.kind,
                reason=waiting.reason,
                since=since,
                go=_go(item.go),
            )
        )
    return changes


def _span_owner(span: SpanRow, by_window: Mapping[str, str], keys: set[str]) -> str | None:
    """The item key a span belongs to: the item holding its window as a place, else its folder,
    else its app (the rule of the builder and of the rankers)."""
    key = by_window.get(span.window_id) if span.window_id else None
    if key is None and span.project_path and FOLDER_PREFIX + span.project_path in keys:
        key = FOLDER_PREFIX + span.project_path
    if key is None and span.app_bundle_id and APP_PREFIX + span.app_bundle_id in keys:
        key = APP_PREFIX + span.app_bundle_id
    return key


def _last_session(result: RankResult) -> LastSession | None:
    """The previous work session and the items focused in it, in order of first focus."""
    activity = result.activity
    previous = activity.previous
    if previous is None:
        return None
    by_window: dict[str, str] = {}
    for item in sorted(result.items, key=lambda i: i.key):
        for place in item.places:
            if place.kind == "window":
                by_window.setdefault(place.ref, item.key)
    ids = {item.key: item.id for item in result.items}
    keys = set(ids)
    seen: dict[str, None] = {}
    for span in activity.spans:
        if not previous.start <= span.started_at < previous.end:
            continue
        key = _span_owner(span, by_window, keys)
        if key is not None:
            seen.setdefault(ids[key])
    return LastSession(
        start=previous.start,
        end=previous.end,
        active_seconds=_round(previous.active_seconds),
        items=list(seen),
    )


def _story_text(story: ItemStory | None) -> str | None:
    return None if story is None else story.text


def build_items_answer(
    result: RankResult,
    now: Now,
    *,
    query: str | None = None,
    limit: int | None = None,
    summaries_enabled: bool = False,
    stories: Mapping[str, ItemStory] | None = None,
) -> Items:
    """``fylgja.items/1`` of a ranking.

    ``now`` is the ``/now`` picture of the request (its waiting entries feed ``while_away``).
    ``query`` keeps the items :func:`item_matches` accepts, in rank order; ``limit`` keeps the
    first ones after that. ``total`` counts the items before both and ``shown`` after. The day
    groups hold the shown items. ``while_away`` and ``last_session`` do not depend on the ranker,
    ``query`` or ``limit``. ``stories`` holds the cached folder summaries by item id
    (:func:`fylgja.api.summary_inputs.item_stories`)."""
    found = stories or {}
    pairs = list(zip(result.items, result.ranked, strict=True))
    needle = (query or "").strip().casefold()
    if needle:
        pairs = [
            pair
            for pair in pairs
            if item_matches(pair[0], needle, _story_text(found.get(pair[0].id)))
        ]
    if limit is not None:
        pairs = pairs[:limit]
    shown = [item for item, _ in pairs]
    return Items(
        generated_at=result.now,
        ranker=result.ranker,
        ranker_fallback=result.fallback,
        activity=_activity_info(result.activity),
        while_away=_while_away(result, now),
        last_session=_last_session(result),
        items=[_nav_item(item, ranked, found.get(item.id)) for item, ranked in pairs],
        days=_days(shown, _local_day(result.now)),
        summaries_enabled=summaries_enabled,
        query=query or None,
        total=len(result.items),
        shown=len(shown),
    )


def build_rankers_answer(
    rankers: Sequence[RegisteredRanker],
    *,
    default: str,
    configured_default: str,
    rank: str | None = None,
) -> Rankers:
    """``fylgja.rankers/1``: every discovered ranker, the loaded ones as ``choices``, and the
    choice to show for a client's stored ``rank``.

    ``selected`` is ``rank`` when it names a choice, else ``default``; ``stale_choice`` is true
    when ``rank`` was given and names no choice."""
    infos: list[RankerInfo] = []
    choices: list[RankerChoice] = []
    for ranker in rankers:
        description = None if ranker.plugin is None else ranker.plugin.description
        infos.append(
            RankerInfo(
                name=ranker.name,
                description=description,
                origin=ranker.origin.value,
                distribution=ranker.distribution,
                version=ranker.version,
                enabled=ranker.enabled,
                loaded=ranker.loaded,
                error=ranker.error,
            )
        )
        if ranker.loaded:
            choices.append(
                RankerChoice(
                    name=ranker.name, description=description, default=ranker.name == default
                )
            )
    names = {choice.name for choice in choices}
    stale = rank is not None and rank not in names
    return Rankers(
        default=default,
        configured_default=configured_default,
        rankers=infos,
        choices=choices,
        selected=rank if rank is not None and not stale else default,
        stale_choice=stale,
    )
