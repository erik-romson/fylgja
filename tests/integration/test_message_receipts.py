"""Truthful receipts in ``message_receipts`` with no text body (WP3.6, ``PLAN.md`` §4.3.1).

Every scenario sends a message carrying a canary text through the real service, the real resolver
and the real transport over fake Unix sockets, and records the receipt through the single writer
into a real database under ``target/tests``. Each state must come from its own condition only, and
the canary must appear in no table, no database file and no log line.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, closing, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_support import T0
from message_support import (
    PID,
    SESSION,
    FakeInbox,
    LineEncoder,
    World,
    dead_socket,
    short_tmp,
    socket_dir,
)

from fylgja import paths
from fylgja.core.writer import Writer
from fylgja.messaging.claude.encoders import OutboundMessage
from fylgja.messaging.claude.encoders.v1 import V1Encoder
from fylgja.messaging.claude.receipts import (
    OBSERVED_ENQUEUED_SUPPORTED,
    Delivery,
    MemoryReceipts,
    Receipt,
    ReceiptState,
    WriterReceipts,
    describe,
    load_receipts,
    observe_enqueued,
)
from fylgja.messaging.claude.transport import Progress
from fylgja.messaging.service import MessageService, MessagingDisabled
from fylgja.store import db
from fylgja.testing.fakes import FakeClock

CANARY = "FYLGJA-TEXT-CANARY-5d1e7a90 please rebase onto main"
KEY = f"claude:{SESSION}"
T0_DT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def socks() -> Iterator[Path]:
    with short_tmp() as root:
        yield socket_dir(root)


@pytest.fixture
def world(isolated_env: Path, socks: Path) -> World:
    return World(isolated_env / "registry", socks)


@pytest.fixture
async def writer() -> AsyncIterator[Writer]:
    instance = Writer(paths.database_file(), clock=FakeClock(T0_DT))
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


def _service(
    world: World, writer: Writer, encoder: Any | None = None, **options: Any
) -> MessageService:
    clock = FakeClock(T0_DT)
    return MessageService(
        enabled=True,
        max_chars=options.pop("max_chars", 8000),
        messengers={"claude": world.messenger(encoder or LineEncoder(), **options)},
        receipts=WriterReceipts(writer),
        clock=clock,
    )


def _stored() -> list[Receipt]:
    with closing(db.connect_reader(paths.database_file())) as connection:
        return load_receipts(connection)


def _everything_stored() -> bytes:
    """Every row of every table, and the raw database, WAL and journal files."""
    parts: list[bytes] = []
    with closing(sqlite3.connect(f"file:{paths.database_file()}?mode=ro", uri=True)) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            for row in conn.execute(f'SELECT * FROM "{table}"'):  # noqa: S608 - own schema
                parts.append(json.dumps(row, default=str).encode())
    for path in paths.database_file().parent.glob(paths.database_file().name + "*"):
        parts.append(path.read_bytes())
    return b"\n".join(parts)


# --- the scenarios -------------------------------------------------------------------------------


@dataclass
class Scenario:
    """Arranges the world, then names the state it must produce and nothing else."""

    state: ReceiptState
    error_kind: str | None
    arrange: Callable[[World], AbstractContextManager[Any]]
    key: str = KEY
    text: str = CANARY
    encoder: Callable[[], Any] = LineEncoder
    options: dict[str, Any] | None = None


def _live(mode: str = "read", **inbox: Any) -> Callable[[World], AbstractContextManager[Any]]:
    def arrange(world: World) -> AbstractContextManager[Any]:
        world.add()
        return FakeInbox(world.sock_path(), mode=mode, **inbox)

    return arrange


def _row_only(**fields: Any) -> Callable[[World], AbstractContextManager[Any]]:
    def arrange(world: World) -> AbstractContextManager[Any]:
        world.add(**fields)
        return FakeInbox(world.sock_path())

    return arrange


def _nothing(world: World) -> AbstractContextManager[Any]:
    return nullcontext()


def _dead(world: World) -> AbstractContextManager[Any]:
    world.add()
    dead_socket(world.sock_path())
    return nullcontext()


def _missing(world: World) -> AbstractContextManager[Any]:
    world.add()
    return nullcontext()


def _symlink(world: World) -> AbstractContextManager[Any]:
    world.add()
    real = world.socks / "real.sock"
    inbox = FakeInbox(real)
    world.sock_path().symlink_to(real)
    return inbox


def _reused(world: World) -> AbstractContextManager[Any]:
    world.add()
    world.starts.table[PID] = T0 + 999
    return FakeInbox(world.sock_path())


class _Boom(LineEncoder):
    def encode(self, message: OutboundMessage) -> bytes:
        raise RuntimeError("an encoder bug")


BIG = 8_000_000

SCENARIOS: dict[str, Scenario] = {
    "submitted": Scenario(ReceiptState.SUBMITTED, None, _live()),
    "unsupported-no-frame": Scenario(
        ReceiptState.UNSUPPORTED, "no_supported_frame", _live(), encoder=V1Encoder
    ),
    "unsupported-old-version": Scenario(
        ReceiptState.UNSUPPORTED, "version_too_old", _row_only(version="2.1.100")
    ),
    "unsupported-peer-protocol": Scenario(
        ReceiptState.UNSUPPORTED, "peer_protocol_unknown", _row_only(peerProtocol=7)
    ),
    "unsupported-kind": Scenario(
        ReceiptState.UNSUPPORTED, "no_message_adapter", _live(), key="codex:thread-1"
    ),
    "unreachable-no-row": Scenario(ReceiptState.UNREACHABLE, "no_live_row", _nothing),
    "unreachable-stale-row": Scenario(
        ReceiptState.UNREACHABLE, "stale_row", _row_only(alive=False)
    ),
    "unreachable-no-socket": Scenario(ReceiptState.UNREACHABLE, "socket_missing", _missing),
    "unreachable-refused": Scenario(ReceiptState.UNREACHABLE, "connection_refused", _dead),
    "refused-symlink": Scenario(ReceiptState.REFUSED_LOCALLY, "socket_symlink", _symlink),
    "refused-pid-reuse": Scenario(ReceiptState.REFUSED_LOCALLY, "pid_reused", _reused),
    "refused-too-long": Scenario(
        ReceiptState.REFUSED_LOCALLY, "text_too_long", _live(), text=CANARY * 300
    ),
    "refused-empty": Scenario(ReceiptState.REFUSED_LOCALLY, "empty_text", _live(), text="  "),
    "refused-control": Scenario(
        ReceiptState.REFUSED_LOCALLY, "control_characters", _live(), text=CANARY + "\x1b[2J"
    ),
    "refused-bad-key": Scenario(
        ReceiptState.REFUSED_LOCALLY, "bad_session_key", _live(), key="claude:"
    ),
    "refused-internal-error": Scenario(
        ReceiptState.REFUSED_LOCALLY, "internal_error", _live(), encoder=_Boom
    ),
    "timed-out": Scenario(
        ReceiptState.TIMED_OUT,
        "write_timeout",
        _live("hold"),
        encoder=lambda: LineEncoder(pad=BIG),
        options={"deadline": 0.2},
    ),
    "unknown": Scenario(
        ReceiptState.UNKNOWN,
        "*",
        _live("close_after", limit=2048, rcvbuf=4096),
        encoder=lambda: LineEncoder(pad=4_000_000),
    ),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_each_state_comes_only_from_its_condition(
    name: str, world: World, writer: Writer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    scenario = SCENARIOS[name]
    encoder = scenario.encoder()
    service = _service(world, writer, encoder, **(scenario.options or {}))
    with scenario.arrange(world) as inbox:
        receipt = await service.send(scenario.key, scenario.text)
        await asyncio.sleep(0.05)
        if isinstance(inbox, FakeInbox) and receipt.state is ReceiptState.SUBMITTED:
            inbox.wait()
        connections = inbox.connections if isinstance(inbox, FakeInbox) else None
        received = list(inbox.received) if isinstance(inbox, FakeInbox) else []
    await writer.flush()

    assert receipt.state is scenario.state
    if scenario.error_kind != "*":
        assert receipt.error_kind == scenario.error_kind
    assert receipt.state is not ReceiptState.OBSERVED_ENQUEUED
    assert _stored() == [receipt]
    assert receipt.session_key == scenario.key

    # The condition behind each state, checked against what the fake inbox saw.
    if scenario.state is ReceiptState.SUBMITTED:
        assert len(received) == 1  # the whole line arrived, once
        assert json.loads(received[0])["id"] == receipt.message_id
        assert receipt.bytes == len(received[0]) > 0
    elif scenario.state in (ReceiptState.TIMED_OUT, ReceiptState.UNKNOWN):
        assert connections == 1  # one attempt, part of the line out, never resent
        assert 0 < receipt.bytes
        if scenario.state is ReceiptState.UNKNOWN:
            assert len(received) == 1
            assert 0 < len(received[0]) <= receipt.bytes < 4_000_000
    else:
        assert receipt.bytes == 0
        assert connections in (None, 0)

    # No text body anywhere.
    canary = CANARY.split()[0].encode()
    assert canary not in _everything_stored()
    assert canary not in caplog.text.encode()
    assert canary.decode() not in json.dumps(receipt.as_json())


def test_every_state_but_observed_enqueued_has_a_scenario() -> None:
    reached = {scenario.state for scenario in SCENARIOS.values()}
    assert reached == set(ReceiptState) - {ReceiptState.OBSERVED_ENQUEUED}


# --- observed-enqueued stays unreachable ----------------------------------------------------------


def test_observed_enqueued_is_documented_and_never_produced() -> None:
    assert OBSERVED_ENQUEUED_SUPPORTED is False
    receipt = Receipt("m", KEY, T0_DT, ReceiptState.SUBMITTED, T0_DT, 10)
    with pytest.raises(NotImplementedError, match="spike 0004"):
        observe_enqueued(receipt, T0_DT)
    text = describe(ReceiptState.OBSERVED_ENQUEUED)
    assert "does not mean Claude read or acted on it" in text


# --- the receipt text and the table ---------------------------------------------------------------


def test_every_state_has_a_plain_sentence_and_no_claim_of_delivery() -> None:
    for state in ReceiptState:
        sentence = describe(state)
        assert sentence.endswith((".", "resending."))
        assert "delivered to Claude" not in sentence
    assert "not delivery" in describe(ReceiptState.SUBMITTED)
    for state in (
        ReceiptState.UNSUPPORTED,
        ReceiptState.UNREACHABLE,
        ReceiptState.REFUSED_LOCALLY,
        ReceiptState.TIMED_OUT,
    ):
        assert "focus-and-copy" in describe(state)
    assert describe(ReceiptState.UNREACHABLE, "stale_row").endswith("(stale_row)")
    assert "not retried" in describe(ReceiptState.UNKNOWN)


def test_the_receipt_has_no_text_field_and_its_json_is_the_row_plus_detail() -> None:
    receipt = Receipt("m-1", KEY, T0_DT, ReceiptState.UNREACHABLE, T0_DT, 0, "stale_row")
    assert "text" not in receipt.__dataclass_fields__
    data = receipt.as_json()
    assert set(data) == {
        "message_id",
        "session_key",
        "state",
        "created_at",
        "state_at",
        "bytes",
        "error_kind",
        "detail",
    }
    assert data["state"] == "unreachable"


def test_the_table_has_no_text_column() -> None:
    connection = db.open_store(paths.database_file())
    try:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(message_receipts)")]
    finally:
        connection.close()
    assert columns == [
        "message_id",
        "session_key",
        "created_at",
        "state",
        "state_at",
        "bytes",
        "error_kind",
    ]


async def test_receipts_load_per_session_in_order(writer: Writer) -> None:
    log = WriterReceipts(writer)
    first = Receipt("a", KEY, T0_DT, ReceiptState.SUBMITTED, T0_DT, 12)
    other = Receipt("b", "claude:other", T0_DT, ReceiptState.UNREACHABLE, T0_DT, 0, "stale_row")
    for receipt in (first, other):
        await log.record(receipt)
    with closing(db.connect_reader(paths.database_file())) as connection:
        assert load_receipts(connection) == [first, other]
        assert load_receipts(connection, KEY) == [first]


# --- the service around the receipt ---------------------------------------------------------------


async def test_a_disabled_service_sends_and_records_nothing(world: World) -> None:
    receipts = MemoryReceipts()
    service = MessageService(
        enabled=False,
        max_chars=8000,
        messengers={"claude": world.messenger()},
        receipts=receipts,
        clock=FakeClock(),
    )
    world.add()
    with FakeInbox(world.sock_path()) as inbox:
        with pytest.raises(MessagingDisabled):
            await service.send(KEY, "hello")
        assert inbox.connections == 0
    assert receipts.receipts == []


async def test_a_receipt_that_cannot_be_stored_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Broken:
        async def record(self, receipt: Receipt) -> None:
            raise OSError("disk full")

    class Unreachable:
        async def send(self, native_id: str, message: OutboundMessage, progress: Progress) -> Any:
            return Delivery(ReceiptState.UNREACHABLE, 0, "no_live_row")

    service = MessageService(
        enabled=True,
        max_chars=8000,
        messengers={"claude": Unreachable()},
        receipts=Broken(),
        clock=FakeClock(),
    )
    receipt = await service.send(KEY, CANARY)
    assert receipt.state is ReceiptState.UNREACHABLE
    assert "not stored: OSError" in caplog.text
    assert CANARY not in caplog.text


async def test_an_adapter_failing_after_bytes_left_is_unknown() -> None:
    class HalfThenBug:
        async def send(self, native_id: str, message: OutboundMessage, progress: Progress) -> Any:
            progress.sent = 5
            raise RuntimeError("bug after the first bytes")

    receipts = MemoryReceipts()
    service = MessageService(
        enabled=True,
        max_chars=8000,
        messengers={"claude": HalfThenBug()},
        receipts=receipts,
        clock=FakeClock(),
        new_id=lambda: "fixed-id",
    )
    receipt = await service.send(KEY, "hello")
    assert (receipt.message_id, receipt.state, receipt.bytes, receipt.error_kind) == (
        "fixed-id",
        ReceiptState.UNKNOWN,
        5,
        "internal_error",
    )
    assert receipts.receipts == [receipt]
