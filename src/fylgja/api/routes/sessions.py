"""``GET /sessions`` and ``GET /sessions/{key}``: ``/now``'s ``sessions[]``.

``key`` is ``kind:native_id``. A session nested under another (``children[]``) is found too. The
list is empty while no collector that reports sessions is available.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from fastapi import APIRouter, Request
from fastapi.responses import Response

from fylgja.api.deps import current_now, json_response, not_found
from fylgja.api.now_models import Session, dump_json

__all__ = ["router"]

router = APIRouter()


def _walk(sessions: Iterable[Session]) -> Iterator[Session]:
    for session in sessions:
        yield session
        yield from _walk(session.children or ())


@router.get("/sessions")
async def list_sessions(request: Request) -> Response:
    return json_response(dump_json(current_now(request).sessions or [], kind=Session))


@router.get("/sessions/{key}")
async def get_session(request: Request, key: str) -> Response:
    for session in _walk(current_now(request).sessions or ()):
        if session.key == key:
            return json_response(dump_json(session))
    raise not_found("session")
