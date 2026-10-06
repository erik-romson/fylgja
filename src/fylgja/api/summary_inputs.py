"""The inputs of a summary, taken from the ``/now`` picture, and the cached ``story`` of ``/now``
(WP8.1; ``PLAN-final-plan.md`` §4.2, §4.6).

A summary is made from what ``/now`` shows, so it never says more than the picture does (titles and
prompt text arrive redacted) and its digest changes exactly when the picture it summarizes does.

* **A session** (:func:`session_inputs`): the agent, project, title, branch, state and §4.6
  one-liner, a norn stage, and from the facts block what ``_story_one`` shows the model: the turns,
  the six most used tools, the edited files (by name, at most ten), the open tasks (at most four),
  the last prompt and reply, every prompt typed so far, and ``last_at``. The transcript's last line
  (``facts.last_at``) is the session's ``activity_at``: a story as of that instant or later is
  never regenerated.
* **A workspace** (:func:`workspace_inputs`): its name, effective label and one-liner, its
  projects, each session's agent, state and one-liner, the top context items of its windows (the
  focused window first, at most five) and its open todos (at most eight).
* **``/now``** (:func:`with_session_story`, :func:`with_workspace_story`): an entry gets a
  ``story`` only while summaries are enabled and a summary with text is cached for it; its state
  is judged against the entry's current inputs.

Pure functions; nothing here reads the store or calls a model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from fylgja.api.now_models import (
    Facts,
    Now,
    Session,
    SessionStory,
    SummaryState,
    Workspace,
    WorkspaceStory,
)
from fylgja.core.summaries import ScopeKind, SummaryAnswer, SummaryInputs, SummaryService

__all__ = [
    "find_inputs",
    "find_session",
    "session_inputs",
    "with_session_story",
    "with_workspace_story",
    "workspace_inputs",
]

TOP_TOOLS: Final = 6
TOP_FILES: Final = 10
TOP_TASKS: Final = 4
TOP_CONTEXT: Final = 5
TOP_TODOS: Final = 8


def _facts_data(facts: Facts) -> dict[str, Any]:
    tools = sorted((facts.tools or {}).items(), key=lambda item: (-item[1], item[0]))
    files = sorted({path.rstrip("/").rsplit("/", 1)[-1] for path in facts.files or () if path})
    tasks = [t.content for t in facts.tasks or () if t.status != "completed" and t.content]
    return {
        "title": facts.title,
        "branch": facts.branch,
        "turns": facts.turns or None,
        "tools": [f"{name}×{count}" for name, count in tools[:TOP_TOOLS]],
        "files": files[:TOP_FILES],
        "open_tasks": tasks[:TOP_TASKS],
        "prompt": facts.prompt,
        "said": facts.said,
        "asked": list(facts.prompts or ()),
        "last_at": facts.last_at,
    }


def _stage(session: Session) -> str | None:
    stage = session.stage
    if stage is None or not stage.name:
        return None
    where = f" ({stage.index}/{stage.total})" if stage.index and stage.total else ""
    return f"{session.pipeline or 'pipeline'} · {stage.name}{where}"


def session_inputs(session: Session) -> SummaryInputs:
    """The inputs of a session's ARC and NOW (see the module docstring)."""
    data: dict[str, Any] = {
        "agent": session.kind,
        "project_path": session.project_path or session.cwd,
        "title": session.title,
        "branch": session.branch,
        "state": None if session.state is None else session.state.value,
        "one_liner": session.one_liner,
        "stage": _stage(session),
    }
    activity_at: datetime | None = None
    facts = session.facts
    if facts is not None:
        found = _facts_data(facts)
        data.update({k: v for k, v in found.items() if k not in ("title", "branch") or v})
        if facts.last_at:
            activity_at = datetime.fromtimestamp(facts.last_at, UTC)
    return SummaryInputs.create("session", session.key, data, activity_at=activity_at)


def _context(workspace: Workspace) -> list[str]:
    """``<kind>: <title>`` of the context items of the workspace's windows, the focused window
    first; a window without items gives ``<app>: <title>``."""
    windows = sorted(workspace.windows, key=lambda w: not w.focused)
    found: list[str] = []
    for window in windows:
        if window.context:
            found.extend(f"{item.kind}: {item.title}" for item in window.context if item.title)
        elif window.title:
            found.append(f"{window.app or 'window'}: {window.title}")
    return found[:TOP_CONTEXT]


def workspace_inputs(workspace: Workspace, sessions: Mapping[str, Session]) -> SummaryInputs:
    """The inputs of a workspace's paragraph (see the module docstring). ``sessions`` holds every
    shown session by key."""
    members = [sessions[key] for key in workspace.sessions if key in sessions]
    data: dict[str, Any] = {
        "name": workspace.name,
        "label": workspace.label,
        "one_liner": workspace.one_liner,
        "projects": list(workspace.projects),
        "sessions": [
            {
                "agent": s.kind,
                "state": None if s.state is None else s.state.value,
                "one_liner": s.one_liner,
                "title": s.title,
            }
            for s in members
        ],
        "context": _context(workspace),
        "todos": [{"status": t.status, "text": t.text} for t in (workspace.todos or ())][
            :TOP_TODOS
        ],
    }
    return SummaryInputs.create("workspace", workspace.name, data)


def _walk(sessions: Iterable[Session]) -> Iterable[Session]:
    for session in sessions:
        yield session
        yield from _walk(session.children or ())


def find_session(sessions: Sequence[Session] | None, key: str) -> Session | None:
    """The shown session ``key``, also among the children of another."""
    return next((s for s in _walk(sessions or ()) if s.key == key), None)


def find_inputs(now: Now, scope: ScopeKind, ref: str) -> SummaryInputs | None:
    """The current inputs of ``(scope, ref)`` in ``now``; ``None`` when ``/now`` does not show it
    (and for ``day``, which WP8.2 adds)."""
    if scope == "session":
        session = find_session(now.sessions, ref)
        return None if session is None else session_inputs(session)
    if scope == "workspace":
        workspace = next((w for w in now.workspaces or () if w.name == ref), None)
        if workspace is None:
            return None
        return workspace_inputs(workspace, {s.key: s for s in _walk(now.sessions or ())})
    return None


def _shown(service: SummaryService, inputs: SummaryInputs) -> SummaryAnswer | None:
    if not service.enabled or service.cached(inputs.scope, inputs.ref) is None:
        return None
    answer = service.answer(inputs.scope, inputs.ref, inputs)
    return answer if answer is not None and answer.has_text else None


def with_session_story(service: SummaryService | None, session: Session) -> Session:
    """``session`` with its cached ``story``, when there is one to show."""
    if service is None or service.cached("session", session.key) is None:
        return session
    answer = _shown(service, session_inputs(session))
    if answer is None:
        return session
    story = SessionStory(
        arc=answer.arc,
        now=answer.now,
        generated_at=answer.generated_at,
        state=SummaryState(answer.state.value),
    )
    return session.model_copy(update={"story": story})


def with_workspace_story(
    service: SummaryService | None, workspace: Workspace, sessions: Mapping[str, Session]
) -> Workspace:
    """``workspace`` with its cached ``story``, when there is one to show."""
    if service is None or service.cached("workspace", workspace.name) is None:
        return workspace
    answer = _shown(service, workspace_inputs(workspace, sessions))
    if answer is None:
        return workspace
    story = WorkspaceStory(
        text=answer.text, generated_at=answer.generated_at, state=SummaryState(answer.state.value)
    )
    return workspace.model_copy(update={"story": story})
