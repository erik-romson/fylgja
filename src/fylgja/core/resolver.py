"""The resolver: places every session in a project, a host, a window and a workspace.

``PLAN-final-plan.md`` WP1.8 and A1. The joins are pure functions over live-view records:

* **session → project:** the git root of the cwd, else the cwd itself. Clones of a draupnir
  workspace carry the workspace as their group. The git root and the group arrive as
  :class:`~fylgja.core.project_roots.ProjectFact` values; the resolver does no I/O.
* **session → host → window:** the first ancestor process that owns an AeroSpace window is the
  host app; its adapter (:mod:`fylgja.core.hosts`) picks the window, and the exact cmux workspace
  and surface where it can.
* **window → workspace:** the AeroSpace workspace of the window.
* **project → workspace:** the AeroSpace workspace that holds the most focus time of the project
  over the last day (``PLAN.md`` §3 rule 4; the focus spans of :mod:`fylgja.core.spans`), and where
  no span says anything about the project, the workspace where most of its windows are.
* **workspace → inferred label** (:mod:`fylgja.core.labels`).

Every join carries ``via`` and ``confidence``, and one that failed carries ``unresolved: <reason>``.
:func:`resolve` computes the :class:`Resolution`; :func:`apply_resolution` writes it into the
``sessions``, ``projects`` and ``workspaces`` columns inside a writer transaction (so the live view
follows); :class:`Resolver` ties the two to the live view and the writer.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from fylgja.collectors.base import Clock
from fylgja.core.hosts import (
    CmuxIds,
    HostAdapter,
    HostData,
    HostSession,
    Join,
    adapter_for,
    cmux_ids_from_env,
    default_adapters,
)
from fylgja.core.labels import InferredLabel, LabelInput, WindowFacts, infer_label, project_name
from fylgja.core.live_view import Change, Collection, LiveView
from fylgja.core.project_roots import ProjectFact, ProjectLocator
from fylgja.core.writer import Tx, Writer
from fylgja.models import (
    HookEvent,
    ProcessRef,
    Record,
    SessionEvent,
    SessionPayload,
    SessionSnapshot,
)
from fylgja.store import db, queries
from fylgja.store.rows import SessionRow, WindowRow

__all__ = [
    "FocusTime",
    "FocusTimeLoader",
    "Placement",
    "ProjectPlacement",
    "Resolution",
    "Resolver",
    "SessionInput",
    "apply_resolution",
    "resolve",
    "session_input",
]

logger = logging.getLogger("fylgja.resolver")

VIA_GIT_ROOT: Final = "git-root"
VIA_CWD: Final = "cwd"
VIA_WINDOW_COUNT: Final = "window-count"
VIA_FOCUS_TIME: Final = "focus-time"
HIGH: Final = "high"
MEDIUM: Final = "medium"
LOW: Final = "low"
NONE: Final = "none"


# --------------------------------------------------------------------------------------------
# Inputs and outputs
# --------------------------------------------------------------------------------------------

FocusTime = Mapping[str, Mapping[str, float]]
"""Seconds of focus per project path and workspace over the last day."""


@dataclass(frozen=True)
class SessionInput:
    """What the resolver needs to know about one live session."""

    session_key: str
    native_id: str
    pid: int | None
    cwd: str | None
    name: str | None = None
    ancestry: Sequence[ProcessRef] = ()
    cmux_ids: CmuxIds | None = None
    """The hook-time ``CMUX_*`` ids (A3), once the hook delivers them."""


def session_input(
    row: SessionRow,
    ancestry: Sequence[ProcessRef] = (),
    cmux_ids: CmuxIds | None = None,
) -> SessionInput:
    """The resolver's view of a live-view session row."""
    return SessionInput(
        session_key=row.session_key,
        native_id=row.native_id,
        pid=row.pid,
        cwd=row.cwd,
        name=row.title or row.name,
        ancestry=ancestry,
        cmux_ids=cmux_ids,
    )


@dataclass(frozen=True)
class Placement:
    """Where one session is. ``unresolved`` says why the window join failed."""

    session_key: str
    project_path: str | None
    project_via: str
    project_confidence: str
    group_name: str | None
    host_kind: str | None
    host_ref: str | None
    window_id: str | None
    workspace: str | None
    """The AeroSpace workspace."""
    via: str
    confidence: str
    unresolved: str | None = None
    note: str = ""
    cmux_workspace_id: str | None = None
    cmux_surface_id: str | None = None
    repo_root: str | None = None


@dataclass(frozen=True)
class ProjectPlacement:
    """One project and the AeroSpace workspace it is mostly in."""

    path: str
    name: str
    repo_root: str | None
    group_name: str | None
    sessions: int
    windows: int
    workspace: str | None
    via: str
    confidence: str
    unresolved: str | None = None


@dataclass(frozen=True)
class Resolution:
    sessions: Mapping[str, Placement]
    projects: Mapping[str, ProjectPlacement]
    labels: Mapping[str, InferredLabel]
    """The inferred label of each AeroSpace workspace."""


# --------------------------------------------------------------------------------------------
# The joins
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Project:
    path: str | None
    via: str
    confidence: str
    group: str | None
    repo_root: str | None


def _project_of(cwd: str | None, fact: ProjectFact | None) -> _Project:
    """Session → project: the git root, else the cwd itself."""
    path = (cwd or "").rstrip("/")
    if not path:
        return _Project(None, NONE, NONE, None, None)
    if fact is None:
        return _Project(path, VIA_CWD, LOW, None, None)
    if fact.git_root:
        return _Project(fact.git_root, VIA_GIT_ROOT, HIGH, fact.group, fact.git_root)
    return _Project(path, VIA_CWD, MEDIUM, fact.group, None)


def _owners(windows: Iterable[WindowRow]) -> dict[int, list[WindowRow]]:
    """The windows of each process that owns one, in a stable order."""
    owners: dict[int, list[WindowRow]] = {}
    for window in windows:
        if window.app_pid is not None:
            owners.setdefault(window.app_pid, []).append(window)
    for group in owners.values():
        group.sort(key=lambda w: (len(w.window_id), w.window_id))
    return owners


def _host_join(
    session: SessionInput,
    owners: Mapping[int, Sequence[WindowRow]],
    adapters: Sequence[HostAdapter],
) -> Join | None:
    """Session → host → window: walk the ancestor chain to the first process that owns a window.

    ``None`` means no host was found; the caller says why.
    """
    for ref in session.ancestry:
        windows = owners.get(ref.pid)
        if windows:
            adapter = adapter_for(windows[0].app_bundle_id or "", adapters)
            host_session = HostSession(
                session_id=session.native_id,
                pid=session.pid,
                cwd=session.cwd or "",
                name=session.name,
                cmux_ids=session.cmux_ids,
            )
            return adapter.join(host_session, windows)
    return None


def _no_host_reason(session: SessionInput) -> str:
    if session.pid is None:
        return "session has no pid"
    if not session.ancestry:
        return "no process chain recorded for the session"
    return "no ancestor process owns an AeroSpace window"


def _place(
    session: SessionInput,
    owners: Mapping[int, Sequence[WindowRow]],
    adapters: Sequence[HostAdapter],
    fact: ProjectFact | None,
) -> Placement:
    project = _project_of(session.cwd, fact)
    join = _host_join(session, owners, adapters)
    if join is None:
        return Placement(
            session_key=session.session_key,
            project_path=project.path,
            project_via=project.via,
            project_confidence=project.confidence,
            group_name=project.group,
            host_kind=None,
            host_ref=None,
            window_id=None,
            workspace=None,
            via=NONE,
            confidence=NONE,
            unresolved=_no_host_reason(session),
            repo_root=project.repo_root,
        )
    return Placement(
        session_key=session.session_key,
        project_path=project.path,
        project_via=project.via,
        project_confidence=project.confidence,
        group_name=project.group,
        host_kind=join.host,
        host_ref=join.host_ref,
        window_id=join.window_id,
        workspace=join.workspace,
        via=join.via,
        confidence=join.confidence,
        unresolved=join.unresolved,
        note=join.note,
        cmux_workspace_id=join.cmux_workspace_id,
        cmux_surface_id=join.cmux_surface_id,
        repo_root=project.repo_root,
    )


def _project_placement(
    path: str,
    members: Sequence[Placement],
    windows: int,
    join: tuple[str | None, str, str],
    *,
    unresolved: str | None = None,
) -> ProjectPlacement:
    first = members[0]
    workspace, via, confidence = join
    return ProjectPlacement(
        path=path,
        name=project_name(path, group_name=first.group_name),
        repo_root=first.repo_root,
        group_name=first.group_name,
        sessions=len(members),
        windows=windows,
        workspace=workspace,
        via=via,
        confidence=confidence,
        unresolved=unresolved,
    )


def _ranked_by_focus(seconds: Mapping[str, float], weight: Counter[str]) -> tuple[str, str] | None:
    """The workspace with the most focus time and the confidence of that pick; ``None`` when no
    workspace has any. Equal time is broken by window count, then by name; a tie that survives
    both is ``low``."""
    ranked = sorted(
        ((ws, secs) for ws, secs in seconds.items() if secs > 0),
        key=lambda item: (-item[1], -weight.get(item[0], 0), item[0]),
    )
    if not ranked:
        return None
    if len(ranked) == 1:
        return ranked[0][0], HIGH
    first, second = ranked[0], ranked[1]
    tie = first[1] == second[1] and weight.get(first[0], 0) == weight.get(second[0], 0)
    return first[0], LOW if tie else MEDIUM


def _project_workspaces(
    placements: Iterable[Placement], focus_time: FocusTime | None = None
) -> dict[str, ProjectPlacement]:
    """Project → workspace: the workspace with the most of the project's focus time over the last
    day (``via`` is ``focus-time``) and, for a project no span covers, the workspace with the most
    of its windows (``window-count``).

    A session whose window is known counts that window once however many sessions share it. A
    session placed only at workspace level counts as one window of that workspace. A tie goes to
    the workspace that sorts first, at ``low`` confidence. Focus time decides even when the project
    has no live window in that workspace; windows only break a tie in time.
    """
    by_project: dict[str, list[Placement]] = {}
    for placement in placements:
        if placement.project_path is not None:
            by_project.setdefault(placement.project_path, []).append(placement)
    projects: dict[str, ProjectPlacement] = {}
    for path, members in sorted(by_project.items()):
        windows: dict[str, set[str]] = {}
        loose: Counter[str] = Counter()
        for member in members:
            if member.workspace is None:
                continue
            if member.window_id is not None:
                windows.setdefault(member.workspace, set()).add(member.window_id)
            else:
                loose[member.workspace] += 1
        weight = Counter({ws: len(ids) for ws, ids in windows.items()}) + loose
        windows_total = sum(len(ids) for ids in windows.values()) + sum(loose.values())

        focused = _ranked_by_focus((focus_time or {}).get(path, {}), weight)
        if focused is not None:
            projects[path] = _project_placement(
                path, members, windows_total, (focused[0], VIA_FOCUS_TIME, focused[1])
            )
            continue
        if not weight:
            projects[path] = _project_placement(
                path,
                members,
                windows_total,
                (None, NONE, NONE),
                unresolved="no session of the project is placed in a workspace",
            )
            continue
        ranked = sorted(weight.items(), key=lambda item: (-item[1], item[0]))
        tie = len(ranked) > 1 and ranked[0][1] == ranked[1][1]
        confidence = LOW if tie else (HIGH if len(ranked) == 1 else MEDIUM)
        projects[path] = _project_placement(
            path, members, windows_total, (ranked[0][0], VIA_WINDOW_COUNT, confidence)
        )
    return projects


def _labels(
    placements: Iterable[Placement],
    windows: Iterable[WindowRow],
    workspaces: Iterable[str],
) -> dict[str, InferredLabel]:
    projects: dict[str, list[str]] = {}
    for placement in placements:
        if placement.workspace is not None and placement.project_path is not None:
            name = project_name(placement.project_path, group_name=placement.group_name)
            if name:
                projects.setdefault(placement.workspace, []).append(name)
    facts: dict[str, list[WindowFacts]] = {}
    for window in windows:
        if window.workspace is not None:
            facts.setdefault(window.workspace, []).append(
                WindowFacts(window.app_name or "", window.title or "")
            )
    names = {*workspaces, *projects, *facts}
    return {
        name: infer_label(LabelInput(projects=projects.get(name, ()), windows=facts.get(name, ())))
        for name in sorted(names)
    }


def resolve(
    sessions: Iterable[SessionInput],
    windows: Iterable[WindowRow],
    workspaces: Iterable[str] = (),
    *,
    adapters: Sequence[HostAdapter],
    facts: Mapping[str, ProjectFact] | None = None,
    focus_time: FocusTime | None = None,
) -> Resolution:
    """Place every session and project and label every workspace. Pure and deterministic.

    ``facts`` maps a cwd to what the filesystem says about it (see
    :class:`~fylgja.core.project_roots.ProjectLocator`); a cwd without an entry is its own project.
    ``focus_time`` is the seconds of focus per project and workspace over the last day; a project
    it names is placed by it, any other by its window count.
    """
    window_list = list(windows)
    owners = _owners(window_list)
    known = {} if facts is None else facts
    placements = {
        session.session_key: _place(session, owners, adapters, known.get(session.cwd or ""))
        for session in sessions
    }
    return Resolution(
        sessions=placements,
        projects=_project_workspaces(placements.values(), focus_time),
        labels=_labels(placements.values(), window_list, workspaces),
    )


# --------------------------------------------------------------------------------------------
# Writing the result
# --------------------------------------------------------------------------------------------


def apply_resolution(tx: Tx, resolution: Resolution, *, at: str) -> int:
    """Write the placements into the store, inside a writer transaction.

    ``sessions`` gets ``project_path``, ``host_kind``, ``host_ref``, ``window_id`` and
    ``workspace``; ``projects`` gets ``repo_root`` and ``group_name`` (a missing value keeps what is
    stored); an open workspace gets its ``inferred_label``. Every row that changed is reported to
    the live view. Returns how many rows changed.
    """
    connection = tx.connection
    changed = 0
    for placement in resolution.sessions.values():
        row = queries.set_session_placement(
            connection,
            session_key=placement.session_key,
            project_path=placement.project_path,
            host_kind=placement.host_kind,
            host_ref=placement.host_ref,
            window_id=placement.window_id,
            workspace=placement.workspace,
        )
        if row is not None:
            tx.changed(Change(Collection.SESSIONS, placement.session_key, row))
            changed += 1
    for project in resolution.projects.values():
        stored = queries.get_project(connection, project.path)
        if stored is not None and (
            (project.repo_root or stored.repo_root) == stored.repo_root
            and (project.group_name or stored.group_name) == stored.group_name
        ):
            continue
        updated = queries.upsert_project(
            connection,
            path=project.path,
            values={"repo_root": project.repo_root, "group_name": project.group_name},
            at=at,
        )
        tx.changed(Change(Collection.PROJECTS, project.path, updated))
        changed += 1
    for name, label in resolution.labels.items():
        workspace = queries.set_inferred_label(connection, name=name, label=label.text, at=at)
        if workspace is not None:
            tx.changed(Change(Collection.WORKSPACES, name, workspace))
            changed += 1
    return changed


# --------------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------------

HostLoader = Callable[[], Awaitable[HostData]]
FocusTimeLoader = Callable[[], Awaitable[FocusTime]]


class Resolver:
    """Keeps the placements of the live view current.

    The daemon passes every record that goes to the writer through :meth:`observe` (sessions carry
    their ancestor chains) and calls :meth:`refresh` when sessions or windows changed; a refresh
    resolves from the live view and commits the result through the writer, which updates the view.
    """

    def __init__(
        self,
        writer: Writer,
        view: LiveView,
        *,
        hosts: HostLoader,
        locator: ProjectLocator,
        clock: Clock,
        focus_time: FocusTimeLoader | None = None,
    ) -> None:
        self._writer = writer
        self._view = view
        self._hosts = hosts
        self._locator = locator
        self._clock = clock
        self._focus_time = focus_time
        self._ancestry: dict[str, tuple[ProcessRef, ...]] = {}
        self._cmux_ids: dict[str, CmuxIds] = {}
        self.on_ids_changed: Callable[[], None] | None = None
        """Called when a hook brought cmux ids a session did not have, so a refresh can use them."""

    def observe(self, record: Record) -> None:
        """Remember the ancestor chains of the sessions a record lists."""
        if isinstance(record, SessionSnapshot):
            for item in record.items:
                self._remember(item.payload)
            if record.complete:
                live = {f"{item.payload.kind}:{item.payload.native_id}" for item in record.items}
                self._ancestry = {k: v for k, v in self._ancestry.items() if k in live}
        elif isinstance(record, SessionEvent):
            self._remember(record.payload)
        elif isinstance(record, HookEvent):
            ids = cmux_ids_from_env(record.payload.ids)
            if ids is not None and self._cmux_ids.get(record.entity_id) != ids:
                self.observe_cmux_ids(record.entity_id, ids)
                if self.on_ids_changed is not None:
                    self.on_ids_changed()

    def _remember(self, payload: SessionPayload) -> None:
        if payload.ancestry:
            self._ancestry[f"{payload.kind}:{payload.native_id}"] = payload.ancestry

    def observe_cmux_ids(self, session_key: str, ids: CmuxIds) -> None:
        """Remember the hook-time cmux ids of a session (A3); they make the join exact."""
        self._cmux_ids[session_key] = ids

    def forget(self, session_key: str) -> None:
        self._ancestry.pop(session_key, None)
        self._cmux_ids.pop(session_key, None)

    @staticmethod
    async def _load_focus_time(loader: FocusTimeLoader) -> FocusTime:
        """Focus time over the last day; a store that cannot be read says nothing, and the join
        falls back to the window count."""
        try:
            return await loader()
        except Exception:
            logger.warning("focus time could not be read; placing projects by window count")
            return {}

    async def refresh(self) -> Resolution:
        """Resolve from the live view and write what changed. Returns the resolution."""
        snapshot = self._view.snapshot()
        inputs = [
            session_input(row, self._ancestry.get(key, ()), self._cmux_ids.get(key))
            for key, row in snapshot.sessions.items()
        ]
        facts = {
            item.cwd: self._locator.locate(item.cwd) for item in inputs if item.cwd is not None
        }
        data = await self._hosts()
        focus_time = (
            {} if self._focus_time is None else await self._load_focus_time(self._focus_time)
        )
        resolution = resolve(
            inputs,
            snapshot.windows.values(),
            snapshot.workspaces.keys(),
            adapters=default_adapters(data),
            facts=facts,
            focus_time=focus_time,
        )
        at = db.to_db_time(self._clock.now())
        await self._writer.execute(lambda tx: apply_resolution(tx, resolution, at=at))
        return resolution
