"""The message action: disabled by default, one attempt, an honest receipt (WP3.6).

:class:`MessageService` is what the API calls. It knows no agent by name in its dispatch: it splits
the session key (``kind:native_id``) and hands the message to the adapter registered for that kind
(:meth:`MessageService.from_config` registers the built-in ones). In order it

1. refuses everything while ``[actions.message] enabled = false`` (:class:`MessagingDisabled`; the
   API answers 409 and records nothing);
2. checks the text: not empty, at most ``max_chars`` characters, no control characters
   (``refused-locally``);
3. finds the adapter for the kind (none: ``unsupported``);
4. composes the body (the provenance label and a generated message id, then the text) and lets the
   adapter send it **once**;
5. records the receipt through the single writer, with no text body, and returns it.

A cancellation is recorded truthfully too (nothing sent: ``refused-locally``; part of the line
sent: ``unknown``) before it propagates. Log lines carry the message id, the session key and the
state, never the text.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Protocol

from fylgja.collectors.base import Clock
from fylgja.collectors.seams import FileSystem, ProcessRunner
from fylgja.config import Config
from fylgja.messaging.claude import messenger as claude_messenger
from fylgja.messaging.claude.encoders import OutboundMessage
from fylgja.messaging.claude.receipts import (
    Delivery,
    Receipt,
    ReceiptLog,
    ReceiptState,
    WriterReceipts,
    new_message_id,
)
from fylgja.messaging.claude.transport import Progress, check_text, classify_cancel, compose

__all__ = ["MessageService", "Messenger", "MessagingDisabled", "WriterReceipts"]

logger = logging.getLogger("fylgja.messaging")


class MessagingDisabled(Exception):
    """``[actions.message] enabled = false``: no message is sent or recorded."""


class Messenger(Protocol):
    """A message adapter for one session kind."""

    async def send(self, native_id: str, message: OutboundMessage, progress: Progress) -> Delivery:
        """Send ``message`` once; report what happened. ``progress`` counts the bytes written."""
        ...


class MessageService:
    """See the module docstring."""

    def __init__(
        self,
        *,
        enabled: bool,
        max_chars: int,
        messengers: Mapping[str, Messenger],
        receipts: ReceiptLog,
        clock: Clock,
        new_id: Callable[[], str] = new_message_id,
    ) -> None:
        self.enabled = enabled
        self.max_chars = max_chars
        self.messengers = dict(messengers)
        self._receipts = receipts
        self._clock = clock
        self._new_id = new_id
        self._pending: set[asyncio.Task[None]] = set()

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        receipts: ReceiptLog,
        clock: Clock,
        runner: ProcessRunner,
        fs: FileSystem,
    ) -> MessageService:
        """The service as ``[actions.message]`` configures it, with the built-in adapters."""
        settings = config.actions.message
        adapter = claude_messenger.build_messenger(config, runner=runner, fs=fs)
        return cls(
            enabled=settings.enabled,
            max_chars=settings.max_chars,
            messengers={claude_messenger.KIND: adapter},
            receipts=receipts,
            clock=clock,
        )

    async def send(self, session_key: str, text: str) -> Receipt:
        """Send ``text`` to the session once and return the recorded receipt.

        Raises :class:`MessagingDisabled` while messaging is off.
        """
        if not self.enabled:
            raise MessagingDisabled
        message_id = self._new_id()
        created_at = self._clock.now()
        kind, _, native_id = session_key.partition(":")
        problem = check_text(text, self.max_chars)
        adapter = self.messengers.get(kind)
        if not kind or not native_id:
            delivery = Delivery(ReceiptState.REFUSED_LOCALLY, 0, "bad_session_key")
        elif problem is not None:
            delivery = Delivery(ReceiptState.REFUSED_LOCALLY, 0, problem)
        elif adapter is None:
            delivery = Delivery(ReceiptState.UNSUPPORTED, 0, "no_message_adapter")
        else:
            message = OutboundMessage(message_id, compose(text, message_id))
            progress = Progress()
            try:
                delivery = await adapter.send(native_id, message, progress)
            except asyncio.CancelledError:
                receipt = self._receipt(
                    message_id, session_key, created_at, classify_cancel(progress)
                )
                self._record_later(receipt)
                raise
            except Exception as error:
                logger.error("message %s failed: %s", message_id, type(error).__name__)
                delivery = (
                    Delivery(ReceiptState.UNKNOWN, progress.sent, "internal_error")
                    if progress.sent
                    else Delivery(ReceiptState.REFUSED_LOCALLY, 0, "internal_error")
                )
        receipt = self._receipt(message_id, session_key, created_at, delivery)
        await self._record(receipt)
        return receipt

    def _receipt(
        self, message_id: str, session_key: str, created_at: datetime, delivery: Delivery
    ) -> Receipt:
        return Receipt(
            message_id=message_id,
            session_key=session_key,
            created_at=created_at,
            state=delivery.state,
            state_at=self._clock.now(),
            bytes=delivery.bytes_sent,
            error_kind=delivery.error_kind,
        )

    async def _record(self, receipt: Receipt) -> None:
        suffix = "" if receipt.error_kind is None else f" ({receipt.error_kind})"
        logger.info(
            "message %s to %s: %s%s",
            receipt.message_id,
            receipt.session_key,
            receipt.state.value,
            suffix,
        )
        try:
            await self._receipts.record(receipt)
        except Exception as error:
            logger.error("receipt %s not stored: %s", receipt.message_id, type(error).__name__)

    def _record_later(self, receipt: Receipt) -> None:
        task = asyncio.get_running_loop().create_task(self._record(receipt))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        """Wait for receipts of cancelled sends to be recorded."""
        while self._pending:
            batch = list(self._pending)
            # gather completes eagerly when every task is already done, before their done
            # callbacks run, so the batch is taken out here rather than by those callbacks.
            await asyncio.gather(*batch, return_exceptions=True)
            self._pending.difference_update(batch)
