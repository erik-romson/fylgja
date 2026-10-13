"""``GET /now``: the whole picture in one call (``fylgja.now/1``).

``?rank=`` names the ranker that orders ``workspaces[]``; without it the order follows
``[items] default_ranker`` (``docs/api/items.md``). That order is the only thing the parameter
changes: the schema is untouched, and a workspace the ranker does not name keeps its natural place
after those it does. A ``rank`` that names no loaded ranker is a 422.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, Request
from fastapi.responses import Response

from fylgja.api.deps import current_now, daemon_of, json_response, ranked_or_422
from fylgja.api.now_models import Workspace, dump_json

__all__ = ["reorder", "router"]

router = APIRouter()


def reorder(workspaces: Sequence[Workspace], order: Sequence[str]) -> list[Workspace]:
    """``workspaces`` in ``order``; those it does not name follow in their natural order."""
    by_name = {workspace.name: workspace for workspace in workspaces}
    first = [by_name.pop(name) for name in dict.fromkeys(order) if name in by_name]
    named = {workspace.name for workspace in first}
    return first + [workspace for workspace in workspaces if workspace.name not in named]


@router.get("/now")
async def get_now(request: Request, rank: str | None = None) -> Response:
    now = current_now(request)
    name = rank or daemon_of(request).effective_default_ranker
    result = ranked_or_422(request, now, name)
    if now.workspaces is None:
        return json_response(dump_json(now))
    ordered = reorder(now.workspaces, result.workspace_order)
    return json_response(dump_json(now.model_copy(update={"workspaces": ordered})))
