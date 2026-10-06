"""``POST /focus/window/{id}`` and ``POST /focus/session/{key}`` (WP3.5).

Both are mutating routes, so the mutation guard (bearer, Origin, JSON, body limit) covers them
without code here. Unknown ids give 404. A session with no window gives 409. The response is
``{"focused": "window" | "tab", "via": ...}``; a failing host step only lowers it to window level,
while a failing AeroSpace step is a 502 that names the failed step.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from fylgja.api.deps import current_now, daemon_of, json_response, not_found
from fylgja.api.now_models import Session
from fylgja.core.focus import FocusActions, FocusFailed, FocusResult

__all__ = ["router"]

router = APIRouter()


def _actions(request: Request) -> FocusActions:
    daemon = daemon_of(request)
    return FocusActions(daemon.runner, daemon.config, terminal_id=daemon.memory.terminal_id)


def _walk(sessions: Iterable[Session]) -> Iterator[Session]:
    for session in sessions:
        yield session
        yield from _walk(session.children or ())


def _response(result: FocusResult) -> Response:
    return json_response(json.dumps(result.as_json()).encode())


@router.post("/focus/window/{window_id}")
async def focus_window(request: Request, window_id: str) -> Response:
    known = any(
        window.id == window_id
        for workspace in current_now(request).workspaces or ()
        for window in workspace.windows
    )
    if not known:
        raise not_found("window")
    try:
        return _response(await _actions(request).focus_window(window_id))
    except FocusFailed as error:
        raise HTTPException(status_code=502, detail=str(error)) from None


@router.post("/focus/session/{key}")
async def focus_session(request: Request, key: str) -> Response:
    found = next((s for s in _walk(current_now(request).sessions or ()) if s.key == key), None)
    if found is None:
        raise not_found("session")
    host = found.host
    if host is None or host.window_id is None:
        raise HTTPException(status_code=409, detail="session has no window")
    try:
        result = await _actions(request).focus_session(
            key=found.key,
            window_id=host.window_id,
            workspace=host.workspace,
            host_kind=host.kind,
            cmux_surface=host.cmux_surface,
            cmux_workspace=host.cmux_workspace,
        )
    except FocusFailed as error:
        raise HTTPException(status_code=502, detail=str(error)) from None
    return _response(result)
