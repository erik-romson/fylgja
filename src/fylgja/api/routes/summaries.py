"""``GET`` and ``POST /summaries/{scope}/{ref}``: cached summaries and asking for one (WP8.1,
``PLAN-final-plan.md`` §4.2 "Summary responses").

``scope`` is ``session`` (``ref`` is the session key), ``workspace`` (``ref`` is the workspace
name) or ``day`` (``ref`` is ``YYYY-MM-DD``; its inputs arrive with WP8.2, so until then only a
cached day summary can be read). Any other scope is **404**. Both methods answer the same model,
:class:`SummaryResponse`: ``{scope, ref, state, arc?, now?, text?, generated_at?, input_digest?,
error_kind?}``, with ``state`` one of ``fresh``, ``stale``, ``pending``, ``missing``, ``failed`` and
``disabled``.

* ``GET`` (a read, no authentication) answers the cached state judged against the scope's current
  inputs and never starts a job: **200**, or **404** when nothing is cached, nothing is pending and
  ``/now`` does not show the scope. While ``[summaries] enabled = false`` it answers ``disabled``.
* ``POST`` (a mutating route: bearer, Origin, JSON, body limit; the body is ignored) asks for a
  summary: **200** with the cached result when it is fresh, or when it is stale or failed and the
  TTL holds the next attempt back; **202** with ``pending`` when a job was queued for it or is
  already running (the requests share that job); **409** with ``disabled`` while summaries are off;
  **404** when ``/now`` does not show the scope (or the scope cannot be summarized yet).

A finished job publishes ``summary.ready`` (``{scope, ref, state}``) on ``/events``; the app wires
that in :func:`fylgja.api.app.create_app`.
"""

from __future__ import annotations

from typing import Final, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import AwareDatetime

from fylgja.api.deps import current_now, daemon_of, json_response
from fylgja.api.now_models import NowModel, SummaryState, dump_json
from fylgja.api.summary_inputs import find_inputs
from fylgja.core.summaries import SCOPES, ScopeKind, SummariesDisabled, SummaryAnswer

__all__ = ["SummaryResponse", "response_of", "router"]

router = APIRouter()

_UNKNOWN_SCOPE: Final = "unknown summary scope (session, workspace or day)"
_NOT_SHOWN: Final = {
    "session": "no live session has that key",
    "workspace": "no workspace has that name",
    "day": "day summaries are not available yet",
}


class SummaryResponse(NowModel):
    """The answer of ``GET`` and ``POST /summaries/{scope}/{ref}``."""

    scope: Literal["session", "workspace", "day"]
    ref: str
    state: SummaryState
    arc: str | None = None
    """A session: what the whole session has been about."""
    now: str | None = None
    """A session: what it is working on at this moment."""
    text: str | None = None
    """A workspace or a day: one short paragraph."""
    generated_at: AwareDatetime | None = None
    """The instant the summary describes (when its job took its inputs)."""
    input_digest: str | None = None
    error_kind: str | None = None
    """Why the latest attempt failed."""


def response_of(answer: SummaryAnswer) -> SummaryResponse:
    return SummaryResponse(
        scope=answer.scope,
        ref=answer.ref,
        state=SummaryState(answer.state.value),
        arc=answer.arc,
        now=answer.now,
        text=answer.text,
        generated_at=answer.generated_at,
        input_digest=answer.input_digest,
        error_kind=answer.error_kind,
    )


def _scope(value: str) -> ScopeKind:
    for scope in SCOPES:
        if scope == value:
            return scope
    raise HTTPException(status_code=404, detail=_UNKNOWN_SCOPE)


@router.get("/summaries/{scope}/{ref}")
async def get_summary(request: Request, scope: str, ref: str) -> Response:
    kind = _scope(scope)
    service = daemon_of(request).summaries
    inputs = find_inputs(current_now(request), kind, ref) if service.enabled else None
    answer = service.answer(kind, ref, inputs)
    if answer is None:
        raise HTTPException(status_code=404, detail=_NOT_SHOWN[kind])
    return json_response(dump_json(response_of(answer)))


@router.post("/summaries/{scope}/{ref}")
async def post_summary(request: Request, scope: str, ref: str) -> Response:
    kind = _scope(scope)
    service = daemon_of(request).summaries
    disabled = SummaryResponse(scope=kind, ref=ref, state=SummaryState.DISABLED)
    if not service.enabled:
        return json_response(dump_json(disabled), status_code=409)
    inputs = find_inputs(current_now(request), kind, ref)
    if inputs is None:
        raise HTTPException(status_code=404, detail=_NOT_SHOWN[kind])
    try:
        answer = service.request(inputs)
    except SummariesDisabled:  # pragma: no cover - the daemon stopped while the body was read
        return json_response(dump_json(disabled), status_code=409)
    status = 202 if answer.state.value == SummaryState.PENDING.value else 200
    return json_response(dump_json(response_of(answer)), status_code=status)
