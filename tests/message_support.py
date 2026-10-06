"""Fake Unix sockets, a test encoder and a registry world for the messaging tests (WP3.6).

Sockets live under a short ``/tmp/fy-XXXX`` directory, because macOS limits ``AF_UNIX`` paths to
104 bytes; nothing here touches ``/tmp/cc-socks*``. The allowed A14 roots are injected, so the
resolver checks the fake directory exactly as it would check Claude's.

:class:`LineEncoder` is test-only: the shipped ``v1`` encoder supports nothing (spike 0004
``no-go``), so the transport and receipt paths past the encoder are exercised with this one.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_support import T0, row_data, write_row

from fylgja.core.system import LocalFileSystem
from fylgja.messaging.claude.encoders import OutboundMessage, Version
from fylgja.messaging.claude.messenger import ClaudeMessenger
from fylgja.messaging.claude.resolve import Resolver

SESSION = "00000000-0000-4000-8000-00000000c0de"
PID = 4242
VERSION = "2.1.289"


@contextlib.contextmanager
def short_tmp() -> Iterator[Path]:
    """A fresh ``/tmp/fy-XXXX`` directory (short enough for ``AF_UNIX``), removed afterwards."""
    root = Path(tempfile.mkdtemp(prefix="fy-", dir="/tmp"))  # noqa: S108 - must be short
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def socket_dir(root: Path, name: str = "cc", mode: int = 0o700) -> Path:
    """A Claude-like socket directory under ``root`` with ``mode``."""
    directory = root / name
    directory.mkdir()
    directory.chmod(mode)
    return directory


def dead_socket(path: Path) -> Path:
    """A socket file nobody listens on: connecting is refused."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.close()
    return path


class FakeInbox:
    """A Unix socket server in a thread that records every connection and the bytes it got.

    ``mode``:

    * ``read``: read until the peer closes;
    * ``slow``: read ``chunk`` bytes at a time with ``delay`` between reads (forces partial writes);
    * ``close_after``: read ``limit`` bytes, then close (the peer fails mid-write);
    * ``hold``: accept and never read (the peer's write blocks until its deadline).
    """

    def __init__(
        self,
        path: Path,
        *,
        mode: str = "read",
        chunk: int = 65536,
        delay: float = 0.0,
        limit: int = 0,
        rcvbuf: int | None = None,
    ) -> None:
        self.path = path
        self.mode = mode
        self.chunk = chunk
        self.delay = delay
        self.limit = limit
        self.connections = 0
        self.received: list[bytes] = []
        self._stop = threading.Event()
        self._held: list[socket.socket] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if rcvbuf is not None:
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self._server.bind(str(path))
        path.chmod(0o600)
        self._server.listen(8)
        self._server.settimeout(0.05)
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FakeInbox:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(5)
        for held in self._held:
            held.close()
        self._server.close()

    @property
    def text(self) -> str:
        return b"".join(self.received).decode("utf-8", errors="replace")

    def wait(self, connections: int = 1, timeout: float = 5.0) -> None:
        """Wait until ``connections`` connections were fully handled."""
        deadline = time.monotonic() + timeout
        while len(self.received) < connections and time.monotonic() < deadline:
            time.sleep(0.01)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            if self.mode == "hold":
                self._held.append(conn)
                continue
            with conn:
                self.received.append(self._read(conn))

    def _read(self, conn: socket.socket) -> bytes:
        conn.settimeout(5.0)
        data = bytearray()
        while not self._stop.is_set():
            want = self.chunk
            if self.mode == "close_after":
                want = min(want, self.limit - len(data))
                if want <= 0:
                    break
            try:
                piece = conn.recv(want)
            except OSError:
                break
            if not piece:
                break
            data += piece
            if self.mode == "slow" and self.delay:
                time.sleep(self.delay)
        if self.mode == "close_after":
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
        return bytes(data)


@dataclass
class LineEncoder:
    """Test-only: one JSON line ``{"type", "id", "text"}`` for the versions it is given."""

    ranges: tuple[tuple[int, Version, Version], ...] = ((1, (2, 1, 224), (2, 1, 999)),)
    encoded: list[OutboundMessage] = field(default_factory=list)
    pad: int = 0
    """Extra bytes in the frame (to fill socket buffers)."""

    @property
    def name(self) -> str:
        return "test-line"

    def supports(self, version: Version, peer_protocol: int) -> bool:
        return any(p == peer_protocol and lo <= version <= hi for p, lo, hi in self.ranges)

    def encode(self, message: OutboundMessage) -> bytes:
        self.encoded.append(message)
        frame: dict[str, Any] = {"type": "test", "id": message.message_id, "text": message.body}
        if self.pad:
            frame["pad"] = "p" * self.pad
        return json.dumps(frame).encode("utf-8") + b"\n"


class Starts:
    """A :data:`~fylgja.messaging.claude.resolve.StartTime` over a table of start times."""

    def __init__(self) -> None:
        self.table: dict[int, float] = {}
        self.calls: list[int] = []

    async def __call__(self, pid: int) -> float | None:
        self.calls.append(pid)
        return self.table.get(pid)


class World:
    """A registry directory, a socket directory under ``/tmp/fy-XXXX`` and a resolver over both."""

    def __init__(self, registry: Path, socks: Path) -> None:
        registry.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.socks = socks
        self.starts = Starts()
        self.resolver = Resolver(
            registry,
            fs=LocalFileSystem(),
            start_time=self.starts,
            roots=(str(socks),),
            uid=os.getuid(),
        )

    def sock_path(self, pid: int = PID) -> Path:
        return self.socks / f"{pid}.sock"

    def add(
        self,
        pid: int = PID,
        session_id: str = SESSION,
        *,
        start: float | None = T0,
        alive: bool = True,
        socket_path: str | None = None,
        **fields: Any,
    ) -> Path:
        """Write a row whose socket is in the fake directory; its process is alive at ``start``."""
        if alive:
            self.starts.table[pid] = T0 if start is None else start
        path = str(self.sock_path(pid)) if socket_path is None else socket_path
        fields.setdefault("version", VERSION)
        fields.setdefault("messagingSocketPath", path)
        return write_row(self.registry, row_data(pid, session_id, start=start, **fields))

    def messenger(self, encoder: Any | None = None, **options: Any) -> ClaudeMessenger:
        return ClaudeMessenger(self.resolver, encoder or LineEncoder(), **options)
