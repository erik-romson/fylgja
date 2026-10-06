"""One ``AF_UNIX`` connect and write within a deadline, never retried (``PLAN.md`` §4.3.1, WP3.6).

* **Text.** Plain text only, at most ``[actions.message] max_chars`` (8 000) characters, with no
  control characters except newline and tab (:func:`check_text`). Too long is refused, never cut:
  a shortened instruction can mean something else. :func:`compose` puts the Fylgja provenance label
  and the generated message id on the first line, so the target sees who sent it and the id can be
  matched later.
* **Frame.** The encoder (:mod:`~fylgja.messaging.claude.encoders`) turns that into exactly one
  newline-terminated line. There is **no auth line**: Fylgja never reads ``*.key`` files or
  ``CLAUDE_CODE_MESSAGING_TOKEN``, so its message is ordinary unverified peer input that the
  target's inbound policy judges.
* **Transport.** :func:`send_once` makes one non-blocking connect and writes the line with
  ``send`` until every byte is out, all inside one deadline (:data:`DEADLINE`, 2 s). It counts the
  bytes the kernel accepted in a :class:`Progress` the caller owns, so a timeout, an error or a
  cancellation half-way still yields a truthful receipt. It reads no reply (the reply format is
  undocumented) and closes the socket.
* **One attempt.** Nothing here loops over attempts. A peer that closes mid-line, a timeout and a
  cancellation all end the send; the caller records the receipt and never resends.
"""

from __future__ import annotations

import asyncio
import os
import socket
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from fylgja.messaging.claude.receipts import Delivery, ReceiptState

__all__ = [
    "DEADLINE",
    "LABEL",
    "MAX_SOCKET_PATH",
    "Connector",
    "Progress",
    "check_frame",
    "check_text",
    "classify_cancel",
    "compose",
    "send_once",
]

DEADLINE: Final = 2.0
"""Seconds for the connect and the whole write together."""
LABEL: Final = (
    "[Fylgja message {message_id}: sent from this Mac by Fylgja, not by a Claude session]"
)
"""The provenance label, the first line of every message body."""
MAX_SOCKET_PATH: Final = 103
"""The longest ``AF_UNIX`` path macOS takes (``sun_path`` is 104 bytes with the NUL)."""

_ALLOWED_CONTROLS: Final = frozenset({"\n", "\t"})

Connector = Callable[[socket.socket, str], Awaitable[None]]
"""Connects a non-blocking socket to a path (default: the event loop's ``sock_connect``)."""


def check_text(text: str, max_chars: int) -> str | None:
    """``None`` when ``text`` may be sent, else the ``error_kind`` that refuses it."""
    if not text.strip():
        return "empty_text"
    if len(text) > max_chars:
        return "text_too_long"
    for char in text:
        if char not in _ALLOWED_CONTROLS and unicodedata.category(char) in ("Cc", "Cs"):
            return "control_characters"
    return None


def compose(text: str, message_id: str) -> str:
    """The plain-text body: the provenance label with the message id, then the text."""
    return LABEL.format(message_id=message_id) + "\n" + text


def check_frame(frame: bytes) -> None:
    """An encoder's output must be one complete line: a single ``\\n``, at the end."""
    if not frame.endswith(b"\n") or b"\n" in frame[:-1]:
        raise ValueError("a frame must be exactly one newline-terminated line")


@dataclass
class Progress:
    """How far one send got; the caller reads it after a timeout, an error or a cancellation."""

    connected: bool = False
    sent: int = 0
    total: int = 0

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.sent >= self.total


def classify_cancel(progress: Progress) -> Delivery:
    """The truthful result of a send cancelled at ``progress``."""
    if progress.complete:
        return Delivery(ReceiptState.SUBMITTED, progress.sent)
    if progress.sent == 0:
        return Delivery(ReceiptState.REFUSED_LOCALLY, 0, "cancelled_before_send")
    return Delivery(ReceiptState.UNKNOWN, progress.sent, "cancelled_mid_write")


def _errno_kind(error: OSError) -> str:
    if error.errno is None:
        return type(error).__name__
    return os.strerror(error.errno).lower().replace(" ", "_")[:40]


async def _writable(loop: asyncio.AbstractEventLoop, sock: socket.socket) -> None:
    ready: asyncio.Future[None] = loop.create_future()

    def wake() -> None:
        if not ready.done():
            ready.set_result(None)

    descriptor = sock.fileno()
    loop.add_writer(descriptor, wake)
    try:
        await ready
    finally:
        loop.remove_writer(descriptor)


async def _write_all(
    loop: asyncio.AbstractEventLoop, sock: socket.socket, frame: bytes, progress: Progress
) -> None:
    view = memoryview(frame)
    while progress.sent < len(view):
        try:
            written = sock.send(view[progress.sent :])
        except (BlockingIOError, InterruptedError):
            await _writable(loop, sock)
            continue
        progress.sent += written


async def send_once(
    path: str,
    frame: bytes,
    *,
    deadline: float = DEADLINE,
    progress: Progress | None = None,
    connector: Connector | None = None,
) -> Delivery:
    """Connect to ``path`` once and write ``frame`` within ``deadline`` seconds.

    Returns ``submitted`` when every byte was written, ``timed-out`` when the deadline passed,
    ``unreachable`` when nothing could be written (no socket, refused, closed before the first
    byte) and ``unknown`` when the peer failed after part of the line was written. A cancellation
    propagates; ``progress`` then tells the caller how far it got (:func:`classify_cancel`).
    """
    check_frame(frame)
    state = Progress() if progress is None else progress
    state.total = len(frame)
    if len(os.fsencode(path)) > MAX_SOCKET_PATH:
        return Delivery(ReceiptState.REFUSED_LOCALLY, 0, "socket_path_too_long")
    loop = asyncio.get_running_loop()
    connect = loop.sock_connect if connector is None else connector
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        async with asyncio.timeout(deadline):
            await connect(sock, path)
            state.connected = True
            await _write_all(loop, sock, frame, state)
    except TimeoutError:
        if state.complete:  # pragma: no cover - the timeout fired after the last byte
            return Delivery(ReceiptState.SUBMITTED, state.sent)
        kind = "write_timeout" if state.connected else "connect_timeout"
        return Delivery(ReceiptState.TIMED_OUT, state.sent, kind)
    except OSError as error:
        if state.sent == 0:
            missing = isinstance(error, FileNotFoundError)
            reason = "socket_missing" if missing else _errno_kind(error)
            return Delivery(ReceiptState.UNREACHABLE, 0, reason)
        return Delivery(ReceiptState.UNKNOWN, state.sent, _errno_kind(error))
    finally:
        sock.close()
    return Delivery(ReceiptState.SUBMITTED, state.sent)
