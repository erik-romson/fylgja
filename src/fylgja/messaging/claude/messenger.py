"""The Claude message adapter: resolve, gate, encode, confirm, send once (WP3.6).

:meth:`ClaudeMessenger.send` runs the steps in this order and stops at the first that refuses:

1. :meth:`~fylgja.messaging.claude.resolve.Resolver.resolve`: the live registry row, liveness, the
   A9 gates and the A14 socket rules;
2. the encoder must support the target's own version and peer protocol, and produce exactly one
   line (``v1`` supports none: spike 0004 ``no-go``, so this step ends every real send as
   ``unsupported``);
3. :meth:`~fylgja.messaging.claude.resolve.Resolver.confirm`: the row, the process start time and
   the socket again, immediately before connecting;
4. :func:`~fylgja.messaging.claude.transport.send_once`: one connect and write within the deadline.

It never loops: whatever the transport returns is the result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fylgja.collectors.seams import FileSystem, ProcessRunner
from fylgja.config import Config
from fylgja.messaging.claude import transport
from fylgja.messaging.claude.encoders import (
    Encoder,
    OutboundMessage,
    UnsupportedFrame,
    default_encoder,
)
from fylgja.messaging.claude.receipts import Delivery, ReceiptState
from fylgja.messaging.claude.resolve import Refusal, Resolver, ps_start_time

__all__ = ["KIND", "ClaudeMessenger", "build_messenger", "registry_dir"]

KIND: Final = "claude"


def registry_dir(config: Config) -> Path:
    """The registry the Claude collector reads: ``[collectors.claude] registry_dir`` or
    ``~/.claude/sessions`` (``HOME`` read at call time)."""
    configured = config.collector_settings(KIND).get("registry_dir")
    if isinstance(configured, str) and configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude" / "sessions"


def build_messenger(config: Config, *, runner: ProcessRunner, fs: FileSystem) -> ClaudeMessenger:
    """The production adapter: the registry, ``ps`` for liveness, the A14 roots of this uid and
    the default (``v1``) encoder."""
    resolver = Resolver(registry_dir(config), fs=fs, start_time=ps_start_time(runner))
    return ClaudeMessenger(resolver, default_encoder())


class ClaudeMessenger:
    """Sends one message to a local Claude session (see the module docstring)."""

    def __init__(
        self,
        resolver: Resolver,
        encoder: Encoder,
        *,
        deadline: float = transport.DEADLINE,
        connector: transport.Connector | None = None,
    ) -> None:
        self.resolver = resolver
        self.encoder = encoder
        self.deadline = deadline
        self._connector = connector

    async def send(
        self, native_id: str, message: OutboundMessage, progress: transport.Progress
    ) -> Delivery:
        try:
            target = await self.resolver.resolve(native_id)
            if not self.encoder.supports(target.version, target.peer_protocol):
                raise Refusal(ReceiptState.UNSUPPORTED, "no_supported_frame")
            try:
                frame = self.encoder.encode(message)
                transport.check_frame(frame)
            except (UnsupportedFrame, ValueError):
                raise Refusal(ReceiptState.UNSUPPORTED, "encoder_refused") from None
            await self.resolver.confirm(target)
        except Refusal as refusal:
            return Delivery(refusal.state, 0, refusal.kind)
        return await transport.send_once(
            target.socket_path,
            frame,
            deadline=self.deadline,
            progress=progress,
            connector=self._connector,
        )
