"""The Host guard of every endpoint (A6; ``PLAN-final-plan.md`` §4.2).

A loopback bind does not stop DNS rebinding: a page on ``evil.example`` whose name resolves to
127.0.0.1 can make the browser send requests to the daemon, and window titles hold customer names.
The browser always sends the attacker's name in ``Host``, so the guard accepts exactly two values,
``127.0.0.1:<port>`` and ``localhost:<port>``, and rejects everything else before any route runs:

* a foreign ``Host`` gets **403**; a missing or repeated ``Host`` gets **400**;
* a websocket with a foreign ``Host`` is closed with code 1008 before it is accepted;
* ``<port>`` is the configured API port, or, when that is ``0`` (a port the OS picks), the port the
  server socket is bound to (``scope["server"]``).

No response ever carries a CORS header: no CORS middleware is installed, and the guard also drops
any ``Access-Control-*`` header a route might add. It is a plain ASGI middleware, so it adds no
measurable cost to ``/now``. The rejected ``Host`` value is never logged.
"""

from __future__ import annotations

import json
import logging
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["CORS_PREFIX", "HostGuard", "allowed_hosts"]

logger = logging.getLogger("fylgja.api.security")

CORS_PREFIX: Final = b"access-control-"
_GUARDED: Final = frozenset({"http", "websocket"})
_POLICY_VIOLATION: Final = 1008


def allowed_hosts(port: int) -> frozenset[str]:
    """The ``Host`` values the API answers for ``port``."""
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})


def _strip_cors(send: Send) -> Send:
    async def guarded(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = [
                (name, value)
                for name, value in message.get("headers", [])
                if not bytes(name).lower().startswith(CORS_PREFIX)
            ]
            message = {**message, "headers": headers}
        await send(message)

    return guarded


class HostGuard:
    """ASGI middleware enforcing the A6 ``Host`` rule (see the module docstring)."""

    def __init__(self, app: ASGIApp, *, port: int) -> None:
        self.app = app
        self.port = port

    def _port(self, scope: Scope) -> int | None:
        if self.port:
            return self.port
        server = scope.get("server")
        if server is None or len(server) < 2 or server[1] is None:
            return None
        return int(server[1])

    def check(self, scope: Scope) -> int:
        """``0`` when the request may pass, else the HTTP status to reject it with."""
        values = [
            value for name, value in scope.get("headers", []) if bytes(name).lower() == b"host"
        ]
        if len(values) != 1:
            return 400
        port = self._port(scope)
        host = bytes(values[0]).decode("latin-1").strip().lower()
        if port is None or host not in allowed_hosts(port):
            return 403
        return 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in _GUARDED:
            await self.app(scope, receive, send)
            return
        status = self.check(scope)
        if status:
            logger.debug("rejected a %s request: Host not allowed (%d)", scope["type"], status)
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": _POLICY_VIOLATION})
                return
            await _reject(send, status)
            return
        await self.app(scope, receive, _strip_cors(send))


async def _reject(send: Send, status: int) -> None:
    detail = "missing or repeated Host header" if status == 400 else "Host not allowed"
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
