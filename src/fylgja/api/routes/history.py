"""``GET /history?since=&until=``: where the attention went (WP5.2, ``PLAN-final-plan.md`` §4.2).

The answer is ``fylgja.history/1``: the focus spans that overlap ``[since, until)`` clipped to that
window, with per-workspace, per-project and per-app totals, and the projects Claude's prompt history
marked as "worked on" (the secondary signal). No authentication.

* ``since`` and ``until`` are ISO 8601 instants (``2026-10-05T08:00:00+02:00``, ``...Z``), a date
  (``2026-10-05``, local midnight) or a local date and time without an offset. ``until`` defaults
  to now and ``since`` to the start of the local day of ``until``. A window that is not positive or
  longer than a year is **422**; so is a time that cannot be read.
* A span that began before ``since`` or ends after ``until`` is clipped to it and says so
  (``clipped``). A span that is still open runs to now (``open``).
* ``spans`` are ordered by start. The list holds at most :data:`MAX_SPANS` spans
  (``spans_truncated`` says when more exist); the totals always cover every span.
* Seconds are rounded to milliseconds. A workspace, project or app that is not known is an entry
  with no name, listed last. A field without a value is absent, never ``null``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from fylgja.api.deps import daemon_of, json_response
from fylgja.core.labels import project_name
from fylgja.core.spans import History, SummarySpan, Total

__all__ = [
    "HISTORY_SCHEMA",
    "MAX_SPANS",
    "HistoryModel",
    "build_history",
    "parse_instant",
    "router",
]

HISTORY_SCHEMA: Final = "fylgja.history/1"
MAX_SPANS: Final = 5000
MAX_WINDOW: Final = timedelta(days=366)

router = APIRouter()


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", serialize_by_alias=True)


class HistorySpan(_Model):
    window_id: str | None = None
    workspace: str | None = None
    app_bundle_id: str | None = None
    title: str | None = None
    project_path: str | None = None
    session_key: str | None = None
    start: AwareDatetime
    end: AwareDatetime
    end_reason: str | None = None
    seconds: float
    open: bool = False
    clipped: bool = False


class WorkspaceTotal(_Model):
    workspace: str | None = None
    seconds: float


class ProjectTotal(_Model):
    project_path: str | None = None
    name: str | None = None
    seconds: float


class AppTotal(_Model):
    app_bundle_id: str | None = None
    seconds: float


class WorkedOn(_Model):
    project_path: str
    name: str
    markers: int
    last_at: AwareDatetime


class HistoryModel(_Model):
    schema_: Literal["fylgja.history/1"] = Field(default=HISTORY_SCHEMA, alias="schema")
    since: AwareDatetime
    until: AwareDatetime
    generated_at: AwareDatetime
    total_seconds: float
    workspaces: list[WorkspaceTotal]
    projects: list[ProjectTotal]
    apps: list[AppTotal]
    worked_on: list[WorkedOn]
    spans: list[HistorySpan]
    spans_truncated: bool = False


def _seconds(value: float) -> float:
    return round(value, 3)


def _span(span: SummarySpan) -> HistorySpan:
    return HistorySpan(
        window_id=span.window_id,
        workspace=span.workspace,
        app_bundle_id=span.app_bundle_id,
        title=span.title,
        project_path=span.project_path,
        session_key=span.session_key,
        start=span.started_at,
        end=span.ended_at,
        end_reason=span.end_reason,
        seconds=_seconds(span.seconds),
        open=span.open,
        clipped=span.clipped,
    )


def _project(total: Total) -> ProjectTotal:
    path = total.key
    return ProjectTotal(
        project_path=path,
        name=None if path is None else project_name(path) or None,
        seconds=_seconds(total.seconds),
    )


def build_history(history: History, *, generated_at: datetime) -> HistoryModel:
    """The ``/history`` model of the daemon's answer."""
    summary = history.summary
    return HistoryModel(
        since=summary.since,
        until=summary.until,
        generated_at=generated_at,
        total_seconds=_seconds(summary.total_seconds),
        workspaces=[
            WorkspaceTotal(workspace=t.key, seconds=_seconds(t.seconds)) for t in summary.workspaces
        ],
        projects=[_project(t) for t in summary.projects],
        apps=[AppTotal(app_bundle_id=t.key, seconds=_seconds(t.seconds)) for t in summary.apps],
        worked_on=[
            WorkedOn(
                project_path=row.project_path,
                name=project_name(row.project_path) or row.project_path,
                markers=row.markers,
                last_at=row.last_at,
            )
            for row in history.worked_on
        ],
        spans=[_span(s) for s in summary.spans[:MAX_SPANS]],
        spans_truncated=len(summary.spans) > MAX_SPANS,
    )


def parse_instant(text: str, *, name: str) -> datetime:
    """An ISO 8601 date or date and time as an aware UTC datetime. A date, or a time without an
    offset, is local time. Raises :class:`ValueError` naming ``name``."""
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"{name} is not an ISO 8601 date or time") from None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # naive: the machine's local time
    return parsed.astimezone(UTC)


def _window(since: str | None, until: str | None, now: datetime) -> tuple[datetime, datetime]:
    stop = now if until is None else parse_instant(until, name="until")
    if since is None:
        local = stop.astimezone()
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    else:
        start = parse_instant(since, name="since")
    if start >= stop:
        raise ValueError("since must be before until")
    if stop - start > MAX_WINDOW:
        raise ValueError("the window may not be longer than a year")
    return start, stop


@router.get("/history")
async def get_history(
    request: Request, since: str | None = None, until: str | None = None
) -> Response:
    daemon = daemon_of(request)
    now = daemon.clock.now()
    try:
        start, stop = _window(since, until, now)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    history = await daemon.history(start, stop)
    model = build_history(history, generated_at=now)
    return json_response(model.model_dump_json(exclude_none=True).encode("utf-8"))
