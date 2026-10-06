"""Message receipts: what Fylgja knows about one message, and never more (``PLAN.md`` §4.3.1).

A receipt is one ``message_receipts`` row (``PLAN-final-plan.md`` §4.1): the message id, the
session key, when it was created, its state and when that state was reached, the number of bytes
written to the target's socket, and a short ``error_kind`` code. **No text body is ever stored**:
neither the row nor this module's log lines hold the message text.

The states, each reached only by its own condition:

``submitted``
    Every byte of the one-line frame was written to the target's socket within the deadline. It
    is *not* "delivered": the target's inbound policy may still deliver, hold or drop it, and
    Fylgja, not being a Claude session, has no sender inbox to hear which.
``observed-enqueued``
    Reserved. It would mean the Claude collector saw the message id in a target
    ``queue-operation`` enqueue record. Spike 0004 found that no enqueue record carries a sender
    id, so nothing produces this state (:data:`OBSERVED_ENQUEUED_SUPPORTED` is ``False``) until a
    by-hand capture shows how a socket message's marker appears there.
``unsupported``
    Fylgja cannot message this target: its kind has no adapter, its Claude version is older than
    2.1.224 or unknown, its peer protocol is unknown (A9), or no encoder supports it. With the
    ``v1`` encoder empty (spike 0004 ``no-go``) every live Claude target ends here.
``unreachable``
    Nothing was written and nothing can be: no live registry row, a dead process (a stale row), no
    inbox socket, or a refused connection.
``refused-locally``
    Fylgja itself did not send: a local check failed (the A14 socket rules, a reused pid, text that
    is empty, too long or holds control characters) or the send was cancelled before any byte left.
``timed-out``
    The 2 s connect-and-write deadline passed. ``bytes`` says how much of the line was written; an
    incomplete line is never a message, because Claude Code acts on complete lines only.
``unknown``
    Part of the line may have reached the target and the result is ambiguous (the peer closed
    mid-write, or the send was cancelled mid-write). It is never retried: a duplicate instruction
    is more dangerous than a dropped one.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Protocol

from fylgja.store import db

__all__ = [
    "OBSERVED_ENQUEUED_SUPPORTED",
    "Delivery",
    "MemoryReceipts",
    "Receipt",
    "ReceiptLog",
    "ReceiptState",
    "WriterReceipts",
    "describe",
    "insert_receipt",
    "load_receipts",
    "new_message_id",
    "observe_enqueued",
]


class ReceiptState(StrEnum):
    SUBMITTED = "submitted"
    OBSERVED_ENQUEUED = "observed-enqueued"
    UNSUPPORTED = "unsupported"
    UNREACHABLE = "unreachable"
    REFUSED_LOCALLY = "refused-locally"
    TIMED_OUT = "timed-out"
    UNKNOWN = "unknown"


OBSERVED_ENQUEUED_SUPPORTED: Final = False
"""Spike 0004 (2026-10-06): no ``queue-operation`` enqueue record has an id or sender field, so a
sender-supplied message id cannot be observed there. ``observed-enqueued`` stays unreachable."""


def new_message_id() -> str:
    """A fresh message id (a random UUID); it goes into the provenance label and the receipt."""
    return str(uuid.uuid4())


@dataclass(frozen=True)
class Delivery:
    """What one send attempt came to, before it becomes a receipt."""

    state: ReceiptState
    bytes_sent: int = 0
    error_kind: str | None = None


@dataclass(frozen=True)
class Receipt:
    """One ``message_receipts`` row. It has no text field on purpose."""

    message_id: str
    session_key: str
    created_at: datetime
    state: ReceiptState
    state_at: datetime
    bytes: int
    error_kind: str | None = None

    def as_json(self) -> dict[str, Any]:
        """The API answer: the row plus a plain-language ``detail``."""
        return {
            "message_id": self.message_id,
            "session_key": self.session_key,
            "state": self.state.value,
            "created_at": db.to_db_time(self.created_at),
            "state_at": db.to_db_time(self.state_at),
            "bytes": self.bytes,
            "error_kind": self.error_kind,
            "detail": describe(self.state, self.error_kind),
        }


_FALLBACK: Final = "Focus the session and paste the text instead (focus-and-copy)."

_DETAILS: Final[dict[ReceiptState, str]] = {
    ReceiptState.SUBMITTED: (
        "Written to the session's socket. That is not delivery: the session's inbound policy "
        "decides whether it is delivered, held for approval or dropped, and Fylgja is not told."
    ),
    ReceiptState.OBSERVED_ENQUEUED: (
        "The session queued the message. That does not mean Claude read or acted on it."
    ),
    ReceiptState.UNSUPPORTED: "Fylgja cannot message this session. " + _FALLBACK,
    ReceiptState.UNREACHABLE: "Not sent: the session has no live, reachable inbox. " + _FALLBACK,
    ReceiptState.REFUSED_LOCALLY: "Not sent: Fylgja refused it before anything left. " + _FALLBACK,
    ReceiptState.TIMED_OUT: (
        "Not sent: the 2 s connect-and-write deadline passed; an incomplete line is never acted "
        "on. Not retried. " + _FALLBACK
    ),
    ReceiptState.UNKNOWN: (
        "The result is ambiguous: part of the message may have reached the session. It was not "
        "retried, because a duplicate is worse than a drop. Check the session before resending."
    ),
}


def describe(state: ReceiptState, error_kind: str | None = None) -> str:
    """A plain-language sentence for a receipt; ``error_kind`` is a short code, never content."""
    text = _DETAILS[state]
    return text if error_kind is None else f"{text} ({error_kind})"


def observe_enqueued(receipt: Receipt, at: datetime) -> Receipt:
    """Advance a ``submitted`` receipt to ``observed-enqueued``.

    Unreachable while :data:`OBSERVED_ENQUEUED_SUPPORTED` is ``False``: it raises rather than claim
    more than Fylgja can see.
    """
    if not OBSERVED_ENQUEUED_SUPPORTED:
        raise NotImplementedError("no enqueue record carries a sender message id (spike 0004)")
    if receipt.state is not ReceiptState.SUBMITTED:  # pragma: no cover - unreachable for now
        raise ValueError("only a submitted message can be observed enqueued")
    return Receipt(  # pragma: no cover - unreachable for now
        receipt.message_id,
        receipt.session_key,
        receipt.created_at,
        ReceiptState.OBSERVED_ENQUEUED,
        at,
        receipt.bytes,
    )


# --------------------------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------------------------

_INSERT: Final = (
    "INSERT INTO message_receipts "
    "(message_id, session_key, created_at, state, state_at, bytes, error_kind) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_SELECT: Final = (
    "SELECT message_id, session_key, created_at, state, state_at, bytes, error_kind "
    "FROM message_receipts"
)


def insert_receipt(connection: sqlite3.Connection, receipt: Receipt) -> None:
    """Insert ``receipt`` (a writer job: run it through the single writer)."""
    connection.execute(
        _INSERT,
        (
            receipt.message_id,
            receipt.session_key,
            db.to_db_time(receipt.created_at),
            receipt.state.value,
            db.to_db_time(receipt.state_at),
            receipt.bytes,
            receipt.error_kind,
        ),
    )


def load_receipts(connection: sqlite3.Connection, session_key: str | None = None) -> list[Receipt]:
    """The stored receipts, oldest first; only those of ``session_key`` when given."""
    if session_key is None:
        rows = connection.execute(_SELECT + " ORDER BY created_at, message_id").fetchall()
    else:
        rows = connection.execute(
            _SELECT + " WHERE session_key = ? ORDER BY created_at, message_id", (session_key,)
        ).fetchall()
    return [
        Receipt(
            message_id=str(row[0]),
            session_key=str(row[1]),
            created_at=db.from_db_time(str(row[2])),
            state=ReceiptState(str(row[3])),
            state_at=db.from_db_time(str(row[4])),
            bytes=int(row[5]),
            error_kind=None if row[6] is None else str(row[6]),
        )
        for row in rows
    ]


class ReceiptLog(Protocol):
    """Where receipts go."""

    async def record(self, receipt: Receipt) -> None: ...


class _Executor(Protocol):
    async def execute(self, job: Any, *, transaction: bool = True) -> Any: ...


class WriterReceipts:
    """Receipts written through the daemon's single writer (:class:`~fylgja.core.writer.Writer`)."""

    def __init__(self, writer: _Executor) -> None:
        self._writer = writer

    async def record(self, receipt: Receipt) -> None:
        await self._writer.execute(lambda tx: insert_receipt(tx.connection, receipt))


class MemoryReceipts:
    """Receipts kept in a list (tests and tools without a database)."""

    def __init__(self) -> None:
        self.receipts: list[Receipt] = []

    async def record(self, receipt: Receipt) -> None:
        self.receipts.append(receipt)
