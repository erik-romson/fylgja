"""The CLI's HTTP client of the daemon (``PLAN-final-plan.md`` A7).

Stdlib ``urllib`` only. Short timeouts, JSON decoding, and two failures the commands tell apart:
:class:`DaemonDown` (connection refused, unreachable or timed out: exit code 3) and
:class:`ApiError` (the daemon answered, but not with usable JSON or a success status: exit code 1).

:func:`open_events` follows the ``/events`` change stream (``http.client``), so a command can wait
for an event such as ``summary.ready`` (``fylgja story``).
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any, Final

from fylgja import paths

__all__ = [
    "DEFAULT_PORT",
    "EXIT_API_ERROR",
    "EXIT_DAEMON_DOWN",
    "TIMEOUT",
    "ApiError",
    "DaemonDown",
    "EventStream",
    "base_url",
    "get_json",
    "open_events",
    "read_secret",
    "send_json",
]

DEFAULT_PORT: Final = 7319
EXIT_API_ERROR: Final = 1
EXIT_DAEMON_DOWN: Final = 3
TIMEOUT: Final = 3.0
"""Seconds before a daemon that does not answer counts as down."""

LOOPBACK: Final = "127.0.0.1"


class DaemonDown(Exception):
    """No daemon answered at ``url``."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"daemon not running at {url} ({reason})")
        self.url = url
        self.reason = reason


class ApiError(Exception):
    """The daemon answered with a failure or with something that is not JSON."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def base_url() -> str:
    """``http://127.0.0.1:<[api] port>`` from the user's configuration."""
    # Imported here: only this call needs Pydantic, and a test of the client alone never does.
    from fylgja.config import load_config

    return f"http://{LOOPBACK}:{load_config().api.port}"


def _reason(exc: BaseException) -> str:
    inner = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(inner, (TimeoutError, socket.timeout)):
        return "timed out"
    if isinstance(inner, ConnectionRefusedError):
        return "connection refused"
    return type(inner).__name__


def get_json(path: str, *, url: str | None = None, timeout: float | None = None) -> Any:
    """``GET <url><path>`` decoded as JSON.

    Raises :class:`DaemonDown` when nothing answers and :class:`ApiError` otherwise.
    """
    root = base_url() if url is None else url
    request = urllib.request.Request(
        root + path, headers={"Accept": "application/json"}, method="GET"
    )
    wait = TIMEOUT if timeout is None else timeout
    try:
        with urllib.request.urlopen(request, timeout=wait) as response:  # noqa: S310 - loopback
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise ApiError(f"{path}: the daemon answered {exc.code} {exc.reason}", exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:  # URLError is an OSError; refused, timeout
        raise DaemonDown(root, _reason(exc)) from exc
    except http.client.HTTPException as exc:
        raise ApiError(f"{path}: malformed answer from the daemon ({type(exc).__name__})") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ApiError(f"{path}: the daemon's answer is not JSON") from exc


def _detail(error: urllib.error.HTTPError) -> str:
    """`` : <detail>`` from the daemon's JSON error body, or nothing (at most 300 characters)."""
    try:
        detail = json.loads(error.read(4096)).get("detail")
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        return ""
    return f": {detail[:300]}" if isinstance(detail, str) and detail else ""


def read_secret() -> str:
    """The API secret that mutating calls send as the bearer; :class:`ApiError` if there is none."""
    try:
        secret = paths.api_secret_file().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        secret = ""
    if not secret:
        raise ApiError("no API secret yet: run `fylgja install secret`, then restart the daemon")
    return secret


def send_json(
    method: str,
    path: str,
    body: Any = None,
    *,
    url: str | None = None,
    timeout: float | None = None,
) -> Any:
    """``POST``, ``PUT``, ``PATCH`` or ``DELETE`` ``<url><path>`` with a JSON body.

    Sends the bearer secret and ``Content-Type: application/json`` and no ``Origin`` (the CLI is not
    a browser). Raises :class:`DaemonDown` when nothing answers and :class:`ApiError` otherwise.
    """
    root = base_url() if url is None else url
    request = urllib.request.Request(
        root + path,
        data=json.dumps({} if body is None else body).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {read_secret()}",
        },
        method=method.upper(),
    )
    wait = TIMEOUT if timeout is None else timeout
    try:
        with urllib.request.urlopen(request, timeout=wait) as response:  # noqa: S310 - loopback
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise ApiError(
            f"{path}: the daemon answered {exc.code} {exc.reason}{_detail(exc)}", exc.code
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DaemonDown(root, _reason(exc)) from exc
    except http.client.HTTPException as exc:
        raise ApiError(f"{path}: malformed answer from the daemon ({type(exc).__name__})") from exc
    if not payload.strip():
        return None
    try:
        return json.loads(payload)
    except ValueError as exc:
        raise ApiError(f"{path}: the daemon's answer is not JSON") from exc


def quote(segment: str) -> str:
    """A path segment, percent-encoded."""
    return urllib.parse.quote(segment, safe="")


# --------------------------------------------------------------------------------------------
# The change stream
# --------------------------------------------------------------------------------------------


def _event_data(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


class EventStream:
    """An open ``GET /events`` stream. Close it when done (:meth:`close` is idempotent)."""

    def __init__(
        self, connection: http.client.HTTPConnection, response: http.client.HTTPResponse
    ) -> None:
        self._connection = connection
        self._response = response
        self._closed = False

    def events(self) -> Iterator[tuple[str, Any]]:
        """``(event name, data)`` of each frame as it arrives; comments are skipped and the data
        is decoded from JSON when it is JSON. Ends with the stream."""
        name = "message"
        data: list[str] = []
        while True:
            raw = self._response.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                if data:
                    yield name, _event_data("\n".join(data))
                name, data = "message", []
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                name = value
            elif field == "data":
                data.append(value)

    def wait_for(self, match: Callable[[str, Any], bool], timeout: float) -> Any | None:
        """The data of the first event ``match`` accepts within ``timeout`` seconds, else ``None``
        (also when the stream ends first). The stream is read in a background thread and is
        closed afterwards."""
        found: list[Any] = []
        done = threading.Event()

        def read() -> None:
            try:
                for name, data in self.events():
                    if match(name, data):
                        found.append(data)
                        return
            except (OSError, ValueError, AttributeError, http.client.HTTPException):
                return  # the stream was closed under the reader
            finally:
                done.set()

        reader = threading.Thread(target=read, name="fylgja-events", daemon=True)
        reader.start()
        done.wait(timeout)
        self._shutdown()
        reader.join(2.0)
        self.close()
        return found[0] if found else None

    def _shutdown(self) -> None:
        """Wake a reader blocked on the socket: the stream ends for it."""
        sock = self._connection.sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._shutdown()
        with contextlib.suppress(OSError, http.client.HTTPException):
            self._response.close()
        self._connection.close()


def open_events(*, url: str | None = None, timeout: float | None = None) -> EventStream:
    """Open ``GET <url>/events``. Raises :class:`DaemonDown` when nothing answers and
    :class:`ApiError` when the daemon refuses the stream. Reads then block until a frame arrives
    (the daemon sends a heartbeat every 15 s)."""
    root = base_url() if url is None else url
    parts = urllib.parse.urlsplit(root)
    connection = http.client.HTTPConnection(
        parts.hostname or LOOPBACK,
        parts.port or DEFAULT_PORT,
        timeout=TIMEOUT if timeout is None else timeout,
    )
    try:
        connection.request("GET", "/events", headers={"Accept": "text/event-stream"})
        response = connection.getresponse()
    except OSError as exc:
        connection.close()
        raise DaemonDown(root, _reason(exc)) from exc
    except http.client.HTTPException as exc:
        connection.close()
        raise ApiError(f"/events: malformed answer from the daemon ({type(exc).__name__})") from exc
    if response.status != 200:
        connection.close()
        raise ApiError(f"/events: the daemon answered {response.status} {response.reason}")
    if connection.sock is not None:
        connection.sock.settimeout(None)
    return EventStream(connection, response)
