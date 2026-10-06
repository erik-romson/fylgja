"""Version-gated encoders of the Claude Code session-inbox message frame (``PLAN.md`` §4.3.1).

The official documentation defines the socket, the optional auth line and the inbound behaviour,
but not the user-message frame, so each frame Fylgja can write is a compatibility adapter here,
tested against every Claude Code version it claims (A9). An encoder turns one
:class:`OutboundMessage` into exactly one newline-terminated line and never adds an auth line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ["Encoder", "OutboundMessage", "UnsupportedFrame", "Version", "default_encoder"]

Version = tuple[int, int, int]
"""A Claude Code version as ``(major, minor, patch)``."""


class UnsupportedFrame(Exception):
    """The encoder has no frame for this target."""


@dataclass(frozen=True)
class OutboundMessage:
    """What is sent: the message id and the plain-text body (provenance label plus the text)."""

    message_id: str
    body: str


class Encoder(Protocol):
    """A frame encoder for a set of Claude Code versions and peer protocols."""

    @property
    def name(self) -> str: ...

    def supports(self, version: Version, peer_protocol: int) -> bool:
        """Whether this encoder has a tested frame for a target of ``version`` and protocol."""
        ...

    def encode(self, message: OutboundMessage) -> bytes:
        """One frame line ending in ``\\n``; :class:`UnsupportedFrame` when there is none."""
        ...


def default_encoder() -> Encoder:
    """The encoder the daemon uses: ``v1``."""
    from fylgja.messaging.claude.encoders.v1 import V1Encoder

    return V1Encoder()
