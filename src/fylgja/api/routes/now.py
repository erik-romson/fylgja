"""``GET /now``: the whole picture in one call (``fylgja.now/1``)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from fylgja.api.deps import current_now, json_response
from fylgja.api.now_models import dump_json

__all__ = ["router"]

router = APIRouter()


@router.get("/now")
async def get_now(request: Request) -> Response:
    return json_response(dump_json(current_now(request)))
