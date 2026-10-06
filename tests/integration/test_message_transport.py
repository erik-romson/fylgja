"""The one-shot ``AF_UNIX`` transport over fake sockets (WP3.6, ``PLAN.md`` §4.3.1, §4.9).

Partial writes, the 2 s deadline, cancellation, size limits, a single attempt even when the result
is ambiguous, and the provenance label and message id in what is sent. Sockets live under a short
``/tmp/fy-XXXX`` directory; ``/tmp/cc-socks*`` is never touched.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import socket
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from message_support import (
    SESSION,
    FakeInbox,
    LineEncoder,
    World,
    dead_socket,
    short_tmp,
    socket_dir,
)

from fylgja.messaging.claude import transport
from fylgja.messaging.claude.encoders import OutboundMessage, UnsupportedFrame
from fylgja.messaging.claude.encoders.v1 import NAME, SPIKE_VERDICT, SUPPORTED, V1Encoder
from fylgja.messaging.claude.messenger import ClaudeMessenger
from fylgja.messaging.claude.receipts import Delivery, MemoryReceipts, ReceiptState
from fylgja.messaging.claude.transport import (
    DEADLINE,
    LABEL,
    MAX_SOCKET_PATH,
    Progress,
    check_frame,
    check_text,
    classify_cancel,
    compose,
    send_once,
)
from fylgja.messaging.service import MessageService
from fylgja.testing.fakes import FakeClock

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def socks() -> Iterator[Path]:
    with short_tmp() as root:
        yield socket_dir(root)


@pytest.fixture
def world(isolated_env: Path, socks: Path) -> World:
    return World(isolated_env / "registry", socks)


def _frame(size: int) -> bytes:
    return b"x" * (size - 1) + b"\n"


def _service(world: World, encoder: Any | None = None, **options: Any) -> MessageService:
    return MessageService(
        enabled=True,
        max_chars=options.pop("max_chars", 8000),
        messengers={"claude": world.messenger(encoder or LineEncoder(), **options)},
        receipts=MemoryReceipts(),
        clock=FakeClock(),
    )


# --- a whole line ------------------------------------------------------------------------------


async def test_a_line_is_written_whole_and_reported_submitted(socks: Path) -> None:
    frame = b'{"type":"test"}\n'
    with FakeInbox(socks / "a.sock") as inbox:
        delivery = await send_once(str(socks / "a.sock"), frame)
        inbox.wait()
    assert delivery == Delivery(ReceiptState.SUBMITTED, len(frame))
    assert inbox.received == [frame]
    assert inbox.connections == 1


async def test_partial_writes_are_continued_until_every_byte_is_out(socks: Path) -> None:
    frame = _frame(400_000)
    with FakeInbox(socks / "a.sock", mode="slow", chunk=4096, delay=0.0005) as inbox:
        progress = Progress()
        delivery = await send_once(str(socks / "a.sock"), frame, deadline=20, progress=progress)
        inbox.wait()
    assert delivery == Delivery(ReceiptState.SUBMITTED, len(frame))
    assert (progress.sent, progress.total, progress.connected) == (len(frame), len(frame), True)
    assert inbox.received == [frame]


class _Dribble:
    """A real socket whose ``send`` takes at most ``step`` bytes and sometimes would block."""

    def __init__(self, sock: socket.socket, step: int) -> None:
        self._sock = sock
        self._step = step
        self.calls = 0

    def fileno(self) -> int:
        return self._sock.fileno()

    def send(self, data: Any) -> int:
        self.calls += 1
        if self.calls % 3 == 0:
            raise BlockingIOError
        return self._sock.send(bytes(data[: self._step]))


async def test_short_sends_and_would_block_are_handled_byte_exactly() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    left.setblocking(False)
    frame = b"0123456789" * 7 + b"\n"
    dribble = _Dribble(left, step=7)
    progress = Progress(total=len(frame))
    try:
        await transport._write_all(asyncio.get_running_loop(), dribble, frame, progress)  # type: ignore[arg-type]
        right.settimeout(2)
        got = b""
        while len(got) < len(frame):
            got += right.recv(1024)
    finally:
        left.close()
        right.close()
    assert got == frame
    assert progress.sent == len(frame)
    assert dribble.calls > len(frame) // 7  # many short sends, some of them would-block


# --- the deadline ---------------------------------------------------------------------------------


def test_the_deadline_is_two_seconds_everywhere() -> None:
    assert DEADLINE == 2.0
    assert inspect.signature(send_once).parameters["deadline"].default == DEADLINE
    assert inspect.signature(ClaudeMessenger).parameters["deadline"].default == DEADLINE


async def test_a_peer_that_never_reads_times_out_at_the_default_two_seconds(socks: Path) -> None:
    frame = _frame(8_000_000)
    with FakeInbox(socks / "a.sock", mode="hold") as inbox:
        started = time.monotonic()
        delivery = await send_once(str(socks / "a.sock"), frame)
        elapsed = time.monotonic() - started
    assert delivery.state is ReceiptState.TIMED_OUT
    assert delivery.error_kind == "write_timeout"
    assert 0 < delivery.bytes_sent < len(frame)
    assert 1.9 <= elapsed < 3.5
    assert inbox.connections == 1


async def test_a_connect_that_hangs_times_out_with_nothing_written(socks: Path) -> None:
    async def hang(sock: socket.socket, path: str) -> None:
        await asyncio.Event().wait()

    delivery = await send_once(str(socks / "a.sock"), b"x\n", deadline=0.05, connector=hang)
    assert delivery == Delivery(ReceiptState.TIMED_OUT, 0, "connect_timeout")


# --- cancellation ---------------------------------------------------------------------------------


async def test_a_send_cancelled_mid_write_is_unknown_and_not_resent(socks: Path) -> None:
    frame = _frame(8_000_000)
    with FakeInbox(socks / "a.sock", mode="hold") as inbox:
        progress = Progress()
        task = asyncio.create_task(
            send_once(str(socks / "a.sock"), frame, deadline=30, progress=progress)
        )
        while progress.sent == 0:  # noqa: ASYNC110 - polling the progress the caller owns
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)
        assert inbox.connections == 1
    assert classify_cancel(progress) == Delivery(
        ReceiptState.UNKNOWN, progress.sent, "cancelled_mid_write"
    )


async def test_a_send_cancelled_before_connecting_sent_nothing(socks: Path) -> None:
    async def hang(sock: socket.socket, path: str) -> None:
        await asyncio.Event().wait()

    progress = Progress()
    task = asyncio.create_task(
        send_once(str(socks / "a.sock"), b"x\n", progress=progress, connector=hang)
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert classify_cancel(progress) == Delivery(
        ReceiptState.REFUSED_LOCALLY, 0, "cancelled_before_send"
    )
    assert classify_cancel(Progress(sent=5, total=5)) == Delivery(ReceiptState.SUBMITTED, 5)


async def test_a_service_send_cancelled_mid_write_records_an_unknown_receipt(
    world: World,
) -> None:
    world.add()
    service = _service(world, LineEncoder(pad=8_000_000), deadline=30)
    with FakeInbox(world.sock_path(), mode="hold") as inbox:
        task = asyncio.create_task(service.send(f"claude:{SESSION}", "hello"))
        while inbox.connections == 0:  # noqa: ASYNC110
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await service.drain()
        assert inbox.connections == 1
    receipts = service._receipts.receipts  # type: ignore[attr-defined]
    assert [(r.state, r.error_kind) for r in receipts] == [
        (ReceiptState.UNKNOWN, "cancelled_mid_write")
    ]
    assert receipts[0].bytes > 0


# --- size limits ----------------------------------------------------------------------------------


def test_text_checks() -> None:
    assert check_text("x" * 8000, 8000) is None
    assert check_text("x" * 8001, 8000) == "text_too_long"
    assert check_text("", 8000) == "empty_text"
    assert check_text(" \n\t ", 8000) == "empty_text"
    assert check_text("line one\nline two\tok", 8000) is None
    assert check_text("æøå 日本 🙂", 8000) is None
    for bad in ("a\x1b[31mred", "a\x00b", "a\rb", "a\x7fb", "a\ud800b"):
        assert check_text(bad, 8000) == "control_characters", repr(bad)


async def test_text_over_max_chars_is_refused_never_cut(world: World) -> None:
    world.add()
    encoder = LineEncoder()
    service = _service(world, encoder, max_chars=10)
    with FakeInbox(world.sock_path()) as inbox:
        too_long = await service.send(f"claude:{SESSION}", "x" * 11)
        fits = await service.send(f"claude:{SESSION}", "x" * 10)
        inbox.wait()
    assert (too_long.state, too_long.error_kind, too_long.bytes) == (
        ReceiptState.REFUSED_LOCALLY,
        "text_too_long",
        0,
    )
    assert fits.state is ReceiptState.SUBMITTED
    assert len(encoder.encoded) == 1  # the refused one was never encoded
    assert inbox.connections == 1


async def test_the_default_max_chars_is_8000(world: World) -> None:
    from api_support import config

    assert config().actions.message.max_chars == 8000
    assert config().actions.message.enabled is False


async def test_a_socket_path_too_long_for_af_unix_is_refused(socks: Path) -> None:
    long_path = "/tmp/" + "p" * (MAX_SOCKET_PATH - 4)  # noqa: S108 - never created
    assert len(long_path) > MAX_SOCKET_PATH
    delivery = await send_once(long_path, b"x\n")
    assert delivery == Delivery(ReceiptState.REFUSED_LOCALLY, 0, "socket_path_too_long")


def test_a_frame_must_be_exactly_one_line() -> None:
    check_frame(b"one line\n")
    for bad in (b"", b"no newline", b"two\nlines\n", b"\n\n"):
        with pytest.raises(ValueError, match="one newline-terminated line"):
            check_frame(bad)


async def test_an_encoder_that_writes_more_than_one_line_is_refused(world: World) -> None:
    class TwoLines(LineEncoder):
        def encode(self, message: OutboundMessage) -> bytes:
            return b'{"type":"auth"}\n' + super().encode(message)

    world.add()
    with FakeInbox(world.sock_path()) as inbox:
        receipt = await _service(world, TwoLines()).send(f"claude:{SESSION}", "hello")
    assert (receipt.state, receipt.error_kind) == (ReceiptState.UNSUPPORTED, "encoder_refused")
    assert inbox.connections == 0


# --- one attempt, even when ambiguous -------------------------------------------------------------


async def test_a_peer_closing_mid_line_is_unknown_and_never_retried(world: World) -> None:
    world.add()
    service = _service(world, LineEncoder(pad=4_000_000))
    with FakeInbox(world.sock_path(), mode="close_after", limit=2048, rcvbuf=4096) as inbox:
        receipt = await service.send(f"claude:{SESSION}", "hello")
        await asyncio.sleep(0.2)
        inbox.wait()
        assert inbox.connections == 1
    assert receipt.state is ReceiptState.UNKNOWN
    assert 0 < receipt.bytes < 4_000_000
    # Usually broken_pipe or connection_reset_by_peer; macOS can also report EPROTOTYPE when a
    # send races the peer's close. Any errno name will do: the state is what must be right.
    assert re.fullmatch(r"[a-z0-9_]+", receipt.error_kind or "")


async def test_a_timed_out_send_is_not_retried(world: World) -> None:
    world.add()
    service = _service(world, LineEncoder(pad=8_000_000), deadline=0.2)
    with FakeInbox(world.sock_path(), mode="hold") as inbox:
        receipt = await service.send(f"claude:{SESSION}", "hello")
        await asyncio.sleep(0.2)
        assert inbox.connections == 1
    assert (receipt.state, receipt.error_kind) == (ReceiptState.TIMED_OUT, "write_timeout")


async def test_a_refused_or_missing_socket_is_unreachable_after_one_try(socks: Path) -> None:
    dead = dead_socket(socks / "dead.sock")
    refused = await send_once(str(dead), b"x\n")
    assert (refused.state, refused.bytes_sent) == (ReceiptState.UNREACHABLE, 0)
    assert refused.error_kind == "connection_refused"
    missing = await send_once(str(socks / "none.sock"), b"x\n")
    assert missing == Delivery(ReceiptState.UNREACHABLE, 0, "socket_missing")


async def test_nothing_in_the_transport_loops_over_attempts() -> None:
    source = inspect.getsource(transport)
    assert "retry" not in source.replace("never retried", "").replace("no retry", "").lower()
    assert source.count("connect(sock, path)") == 1


# --- the provenance label and the message id ------------------------------------------------------


async def test_the_label_and_a_generated_message_id_are_sent(world: World) -> None:
    world.add()
    service = _service(world)
    with FakeInbox(world.sock_path()) as inbox:
        first = await service.send(f"claude:{SESSION}", "please look at the build")
        second = await service.send(f"claude:{SESSION}", "please look at the build")
        inbox.wait(2)
    assert first.message_id != second.message_id
    for receipt, raw in zip((first, second), inbox.received, strict=True):
        assert uuid.UUID(receipt.message_id).version == 4
        assert raw.endswith(b"\n")
        assert raw.count(b"\n") == 1
        frame = json.loads(raw)
        assert frame["id"] == receipt.message_id
        label, _, text = frame["text"].partition("\n")
        assert label == LABEL.format(message_id=receipt.message_id)
        assert "Fylgja" in label
        assert receipt.message_id in label
        assert text == "please look at the build"
        assert receipt.bytes == len(raw)
    assert b'"auth"' not in b"".join(inbox.received)  # never an auth line


def test_compose_puts_the_label_first() -> None:
    body = compose("hi\nthere", "abc")
    assert body.splitlines()[0] == LABEL.format(message_id="abc")
    assert body.endswith("\nhi\nthere")


# --- the shipped encoder follows the spike's verdict ----------------------------------------------


def test_the_v1_encoder_matches_the_spike_verdict() -> None:
    note = (REPO / "docs" / "spikes" / "0004-message-frame.md").read_text(encoding="utf-8")
    verdicts = re.findall(r"^Verdict: (go|no-go)\s*$", note, flags=re.MULTILINE)
    assert verdicts == [SPIKE_VERDICT]
    encoder = V1Encoder()
    assert encoder.name == NAME
    if SPIKE_VERDICT == "no-go":
        assert SUPPORTED == ()
        for version in ((2, 1, 224), (2, 1, 284), (2, 1, 289), (2, 1, 291)):
            assert not encoder.supports(version, 1)
        with pytest.raises(UnsupportedFrame):
            encoder.encode(OutboundMessage("id", "body"))
