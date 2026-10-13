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
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fylgja.api.now_models import Now, Project, Session, Window
from fylgja.config import ItemsConfig
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
from fylgja.store.rows import SpanRow

__all__ = ["DEFAULT_DAYS", "SUMMARY_SEPARATOR", "build_items"]

DEFAULT_DAYS = 14
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
