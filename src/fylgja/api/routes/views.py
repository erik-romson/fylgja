"""``GET /view/overview``: the Overview tab of the navigator page in one call
(``docs/api/views.md``, ``docs/decisions/0047``).

A read: no authentication, Host-checked like every route. The answer is
``fylgja.view.overview/1`` (:class:`~fylgja.api.view_models.Overview`), built by
:func:`fylgja.api.views.build_overview` from the same three answers the page once fetched itself:
``/now``, the open and next todos (``GET /todos?status=open,next``) and ``GET /next``
(:func:`fylgja.api.routes.next.next_list`, so ``next_list_id`` is the one ``GET /next`` gives for
the same state, and no extra suggestion list is logged).

``?rank=`` names the ranker that orders ``cards[]`` (by its ``workspace_order``); without it the
order follows ``[items] default_ranker``. A ``rank`` that names no loaded ranker is a 422.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import Response

from fylgja.api.deps import current_now, daemon_of, json_response, ranked_or_422
from fylgja.api.now import todo_model
from fylgja.api.now_models import dump_json
from fylgja.api.routes.next import next_list
from fylgja.api.views import build_overview

__all__ = ["router"]

router = APIRouter()

_OPEN_STATUSES: Final = ("open", "next")
"""The statuses the Overview lists (``GET /todos?status=open,next``)."""


@router.get("/view/overview")
async def get_overview(request: Request, rank: str | None = None) -> Response:
    daemon = daemon_of(request)
    now = current_now(request)
    result = ranked_or_422(request, now, rank or daemon.effective_default_ranker)
    rows = await daemon.todos.list_todos(statuses=_OPEN_STATUSES)
    overview = build_overview(
        now,
        [todo_model(row) for row in rows],
        next_list(daemon, now),
        generated_at=now.generated_at,
        workspace_order=result.workspace_order,
    )
    return json_response(dump_json(overview))
