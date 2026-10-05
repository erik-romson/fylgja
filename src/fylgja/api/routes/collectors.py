"""``GET /collectors``: the ``collectors list`` rows (WP1.2).

Loaded plugins show their API version, capabilities and live health; a disabled external shows
only its metadata, with ``not_loaded`` for what needs its code. ``/now``'s ``collectors[]`` is the
health-only view of the same collectors.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import Response

from fylgja.api.deps import daemon_of, json_response

__all__ = ["router"]

router = APIRouter()


@router.get("/collectors")
async def list_collectors(request: Request) -> Response:
    rows = daemon_of(request).collector_rows()
    body = json.dumps([row.model_dump(mode="json") for row in rows], separators=(",", ":"))
    return json_response(body.encode("utf-8"))
