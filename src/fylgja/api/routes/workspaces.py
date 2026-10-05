"""``GET /workspaces`` and ``GET /workspaces/{name}``: ``/now``'s ``workspaces[]``.

The list is empty while no collector that reports windows is available.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from fylgja.api.deps import current_now, json_response, not_found
from fylgja.api.now_models import Workspace, dump_json

__all__ = ["router"]

router = APIRouter()


@router.get("/workspaces")
async def list_workspaces(request: Request) -> Response:
    return json_response(dump_json(current_now(request).workspaces or [], kind=Workspace))


@router.get("/workspaces/{name}")
async def get_workspace(request: Request, name: str) -> Response:
    for workspace in current_now(request).workspaces or []:
        if workspace.name == name:
            return json_response(dump_json(workspace))
    raise not_found("workspace")
