"""``POST /sessions/{key}/message``: the opt-in message action (WP3.6, ``PLAN.md`` §4.3.1).

A mutating route, so the mutation guard (bearer, Origin, JSON, a 64 KiB body limit) has passed
before this runs. Then:

* **409** while ``[actions.message] enabled = false`` (the default): nothing is sent or recorded;
* **400** for a body that is not JSON, and for any field that looks like a socket path or address:
  the target is always resolved from the live session registry, never taken from the caller;
* **422** for a body that is not exactly ``{"text": "<string>"}``;
* otherwise **200** with the receipt (:meth:`~fylgja.messaging.claude.receipts.Receipt.as_json`):
  ``submitted`` only when the whole line was written, and an honest state for everything else.
  The text is never echoed, logged or stored.
"""

from __future__ import annotations

import json
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from fylgja.api.deps import daemon_of, json_response
from fylgja.messaging.claude.resolve import supplied_socket_fields
from fylgja.messaging.service import MessagingDisabled

__all__ = ["DISABLED_DETAIL", "router"]

router = APIRouter()

DISABLED_DETAIL: Final = (
    "messaging is disabled ([actions.message] enabled = false); focus the session and paste "
    "the text instead"
)


@router.post("/sessions/{key}/message")
async def send_message(request: Request, key: str) -> Response:
    service = daemon_of(request).messages
    if not service.enabled:
        raise HTTPException(status_code=409, detail=DISABLED_DETAIL)
    try:
        body = json.loads(await request.body())
    except (ValueError, RecursionError):
        raise HTTPException(status_code=400, detail="the body is not JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail='the body must be {"text": "..."}')
    if supplied_socket_fields(body):
        raise HTTPException(
            status_code=400,
            detail="a socket path or address is never accepted; the target is resolved from "
            "the live session registry",
        )
    text = body.get("text")
    if set(body) != {"text"} or not isinstance(text, str):
        raise HTTPException(status_code=422, detail='the body must be {"text": "..."}')
    try:
        receipt = await service.send(key, text)
    except MessagingDisabled:  # the service was switched off while the body was read
        raise HTTPException(status_code=409, detail=DISABLED_DETAIL) from None
    return json_response(json.dumps(receipt.as_json()).encode("utf-8"))
