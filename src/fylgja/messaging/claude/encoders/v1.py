"""The ``v1`` session-inbox frame encoder: present, and empty (spike 0004, verdict ``no-go``).

Spike 0004 (``docs/spikes/0004-message-frame.md``, 2026-10-06) found that the official
documentation specifies the socket, the optional auth line and the "complete line within 30 s"
rule, but not the JSON object that carries a user message, and the protocol survey does not either.
By the authority order a frame Fylgja guessed would be undocumented behaviour written into another
process's input, so this adapter supports **no** Claude Code version: :meth:`V1Encoder.supports`
is always false, every send reports ``unsupported`` and focus-and-copy is the action.

When a by-hand capture (spike 0004 §4) establishes the frame, a later change fills in
:data:`SUPPORTED`, implements :meth:`V1Encoder.encode` and adds protocol fixtures for the minimum
(2.1.224) and current supported versions, and sets the spike's verdict to ``go``.
"""

from __future__ import annotations

from typing import Final

from fylgja.messaging.claude.encoders import OutboundMessage, UnsupportedFrame, Version

__all__ = ["NAME", "SPIKE_VERDICT", "SUPPORTED", "V1Encoder"]

NAME: Final = "claude-inbox-v1"
SPIKE_VERDICT: Final = "no-go"
"""The ``Verdict:`` line of ``docs/spikes/0004-message-frame.md``; a test keeps the two in step."""

SUPPORTED: Final[tuple[tuple[int, Version, Version], ...]] = ()
"""``(peer_protocol, first_version, last_version)`` ranges with a tested frame. Empty: no-go."""


class V1Encoder:
    """Supports nothing until the frame is documented or captured (see the module docstring)."""

    @property
    def name(self) -> str:
        return NAME

    def supports(self, version: Version, peer_protocol: int) -> bool:
        return any(
            protocol == peer_protocol and first <= version <= last
            for protocol, first, last in SUPPORTED
        )

    def encode(self, message: OutboundMessage) -> bytes:
        raise UnsupportedFrame(
            "the session-inbox message frame is not documented (spike 0004: no-go)"
        )
