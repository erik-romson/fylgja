"""``GET /projects``: ``/now``'s ``projects[]`` (empty while the section is absent)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from fylgja.api.deps import current_now, json_response
from fylgja.api.now_models import Project, dump_json

__all__ = ["router"]

router = APIRouter()


@router.get("/projects")
async def list_projects(request: Request) -> Response:
    return json_response(dump_json(current_now(request).projects or [], kind=Project))
