"""The Host guard of every endpoint and the mutation guard (A6; ``PLAN-final-plan.md`` §4.2).

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

:class:`MutationGuard` runs inside the Host guard and covers **every** HTTP request with a mutating
method (``POST``, ``PUT``, ``PATCH``, ``DELETE``), whatever the route: a route added later cannot
skip it, because it keys on the method and not on a router. In this order it requires

1. ``Authorization: Bearer <secret>``, compared in constant time (**401**; a daemon without a
   secret file refuses every mutation);
2. an ``Origin`` from ``[api] allowed_origins``, or no ``Origin`` at all (the CLI), which only
   counts together with the valid bearer of step 1 (**403**);
3. ``Content-Type: application/json``, which turns away ``text/plain`` form CSRF (**415**);
4. a body of at most 16 KiB, or 64 KiB for ``/sessions/{key}/message`` (**413**), enforced while the
   body is read and before any route or JSON parser sees it.

The answers are fixed JSON texts: they say what was missing, never what was sent, and the secret
and the rejected ``Origin`` are never echoed or logged.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from collections.abc import Collection, Iterable, Iterator
from typing import Any, Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fylgja import paths

__all__ = [
    "BODY_LIMIT",
    "CORS_PREFIX",
    "MESSAGE_BODY_LIMIT",
    "MUTATING_METHODS",
    "HostGuard",
    "MutationGuard",
    "allowed_hosts",
    "body_limit",
    "load_secret",
    "mutating_routes",
]

logger = logging.getLogger("fylgja.api.security")

CORS_PREFIX: Final = b"access-control-"
_GUARDED: Final = frozenset({"http", "websocket"})
_POLICY_VIOLATION: Final = 1008

MUTATING_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
BODY_LIMIT: Final = 16 * 1024
"""Largest request body of a mutating endpoint, in bytes."""
MESSAGE_BODY_LIMIT: Final = 64 * 1024
"""Largest body of ``POST /sessions/{key}/message``."""
_MESSAGE_PATH: Final = re.compile(r"/sessions/[^/]+/message/?")
_MEDIA_TYPE: Final = "application/json"


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


# --------------------------------------------------------------------------------------------
# The mutation guard
# --------------------------------------------------------------------------------------------


def load_secret() -> str | None:
    """The API secret from ``paths.api_secret_file()``; ``None`` when there is none or it is empty.

    The daemon reads it once at start-up (``fylgja install secret`` creates it). A restart picks up
    a rotated secret.
    """
    try:
        text = paths.api_secret_file().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def body_limit(path: str) -> int:
    """The request body limit of ``path``: 64 KiB for a session message, else 16 KiB."""
    return MESSAGE_BODY_LIMIT if _MESSAGE_PATH.fullmatch(path) else BODY_LIMIT


def mutating_routes(app: Any) -> list[tuple[str, str]]:
    """``(method, path)`` of every route registered on ``app`` with a mutating method.

    ``tests/integration/test_mutation_guards.py`` runs its guard assertions against each of them, so
    a mutating route added later is covered without touching the test.
    """
    found: list[tuple[str, str]] = []
    for path, methods in _route_table(getattr(app, "routes", [])):
        found.extend((method, path) for method in sorted(set(methods) & MUTATING_METHODS))
    return found


def _route_table(routes: Any) -> Iterator[tuple[str, Iterable[str]]]:
    """``(path, methods)`` of every route, looking inside the routers an app has included.

    Recent FastAPI keeps an included router as one entry that holds the real routes; its
    ``effective_route_contexts`` list them with the full path.
    """
    for route in routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is not None:
            for context in contexts() if callable(contexts) else contexts:
                yield str(context.path), tuple(context.methods or ())
            continue
        path = getattr(route, "path", None)
        if path is not None:
            yield str(path), tuple(getattr(route, "methods", None) or ())


def _headers(scope: Scope, name: bytes) -> list[bytes]:
    return [bytes(value) for key, value in scope.get("headers", []) if bytes(key).lower() == name]


def _bearer_ok(scope: Scope, secret: str | None) -> bool:
    values = _headers(scope, b"authorization")
    if secret is None or len(values) != 1:
        return False
    scheme, _, token = values[0].decode("latin-1").strip().partition(" ")
    presented = token.strip().encode("utf-8", "replace")
    expected = secret.encode("utf-8")
    return hmac.compare_digest(presented, expected) and scheme.lower() == "bearer"


def _origin_ok(scope: Scope, allowed: Collection[str]) -> bool:
    values = _headers(scope, b"origin")
    if not values:
        return True
    if len(values) != 1:
        return False
    return values[0].decode("latin-1").strip().rstrip("/").lower() in allowed


def _json_content_type(scope: Scope) -> bool:
    values = _headers(scope, b"content-type")
    if len(values) != 1:
        return False
    media = values[0].decode("latin-1").split(";", 1)[0].strip().lower()
    return media == _MEDIA_TYPE


_REJECTIONS: Final = {
    401: "a valid bearer secret is required",
    403: "Origin not allowed",
    413: "request body too large",
    415: "Content-Type must be application/json",
}


class _TooLarge(Exception):
    pass


class MutationGuard:
    """ASGI middleware that guards every mutating request (see the module docstring).

    ``secret`` is the bearer secret (``None``: no secret exists, so every mutation is refused with
    401) and ``allowed_origins`` the ``[api] allowed_origins`` list. It sits inside
    :class:`HostGuard`, which has already checked ``Host`` and strips CORS headers from the answer.
    """

    def __init__(self, app: ASGIApp, *, secret: str | None, allowed_origins: Iterable[str]) -> None:
        self.app = app
        self._secret = secret or None
        self._origins = frozenset(o.strip().rstrip("/").lower() for o in allowed_origins)

    def check(self, scope: Scope) -> int:
        """``0`` when the headers pass, else the HTTP status to reject with."""
        if not _bearer_ok(scope, self._secret):
            return 401
        if not _origin_ok(scope, self._origins):
            return 403
        if not _json_content_type(scope):
            return 415
        return 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or str(scope.get("method", "")).upper() not in MUTATING_METHODS:
            await self.app(scope, receive, send)
            return
        status = self.check(scope)
        if status:
            logger.debug("rejected a mutating request (%d)", status)
            await _reject_with(send, status)
            return
        limit = body_limit(str(scope.get("path", "")))
        declared = _headers(scope, b"content-length")
        if declared and (not declared[0].isdigit() or int(declared[0]) > limit):
            await _reject_with(send, 413)
            return
        try:
            body = await _read_body(receive, limit)
        except _TooLarge:
            await _reject_with(send, 413)
            return
        await self.app(scope, _replay(body, receive), send)


async def _read_body(receive: Receive, limit: int) -> list[Message]:
    """The request messages up to the end of the body; raises when the body passes ``limit``."""
    messages: list[Message] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            messages.append(message)  # a disconnect: replayed as it came
            return messages
        size += len(message.get("body", b""))
        if size > limit:
            raise _TooLarge
        messages.append(message)
        if not message.get("more_body", False):
            return messages


def _replay(messages: list[Message], receive: Receive) -> Receive:
    queue = list(messages)

    async def replay() -> Message:
        if queue:
            return queue.pop(0)
        return await receive()

    return replay


async def _reject_with(send: Send, status: int) -> None:
    body = json.dumps({"detail": _REJECTIONS[status]}).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
