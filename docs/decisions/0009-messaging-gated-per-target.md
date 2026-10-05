# 0009: Messaging is gated per target session

- Status: accepted
- Amendment: A9
- Amends: PLAN.md §4.3.1
- Answers: —
- Relates to: A14
- Date: 2026-10-05

## Context

R5: running sessions use different versions.

## Decision

**Messaging is gated per target session**, using that session's registry `version` (≥ 2.1.224) and `peerProtocol`, not the installed CLI version. An unknown `peerProtocol` disables sending to that target.

## Consequences

- WP3.6 reads each target's registry `version` and `peerProtocol` before offering a send action.
- An unknown `peerProtocol` disables sending to that target, with a focus or copy fallback.
- The installed CLI version is never the gate.
