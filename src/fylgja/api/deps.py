"""What the routes share: the daemon of the app and the JSON response of the serialization rule."""

from __future__ import annotations

from typing import Final

from fastapi import HTTPException, Request
from fastapi.responses import Response

from fylgja.api.now import build_now
from fylgja.api.now_models import Now
from fylgja.core.daemon import Daemon

__all__ = ["JSON", "current_now", "daemon_of", "json_response", "not_found"]

JSON: Final = "application/json"
_NO_STORE: Final = {"cache-control": "no-store"}


def daemon_of(request: Request) -> Daemon:
    daemon = request.app.state.daemon
    if not isinstance(daemon, Daemon):  # pragma: no cover - create_app always sets it
        raise RuntimeError("the app has no daemon")
    return daemon


def current_now(request: Request) -> Now:
    """``/now`` as of this moment (built on the event loop thread)."""
    return build_now(daemon_of(request).state())


def json_response(body: bytes, status_code: int = 200) -> Response:
    """Live data: JSON, never cached."""
    return Response(content=body, status_code=status_code, media_type=JSON, headers=_NO_STORE)


def not_found(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{what} not found")
