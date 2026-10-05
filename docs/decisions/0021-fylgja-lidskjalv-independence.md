# 0021: Fylgja and Lidskjalv stay independent permanently

- Status: accepted
- Amendment: A21
- Amends: PLAN.md §2, §4.3
- Answers: Q2, Q9, Q11
- Relates to: A10, A18, A22, A24
- Date: 2026-10-05

## Context

Erik wants to be able to remove Lidskjalv without leaving any dependency behind. The round-1 version of A21, which removed Fylgja's hooks and made Fylgja rely on Lidskjalv's forwarder, contradicts that and is withdrawn.

## Decision

**Fylgja and Lidskjalv stay independent permanently** (Q9, which refines Q2). Fylgja never depends on Lidskjalv, at runtime or in its tests: not on its hook, its cache, its repository or its installation. Removing Lidskjalv (its hook entries, its `dotfiles.conf` line, the SketchyBar plugins, the panel and the cache) leaves Fylgja fully working. Fylgja's own direct async hook registrations are therefore permanent, not just for the dual run. Lidskjalv's hook never forwards events to Fylgja, because Fylgja's own hooks already receive every event and forwarding would duplicate them. "Thin" (Q2) therefore applies only to Lidskjalv's side. After the dual-run gate (WP2.7), Lidskjalv reads its session data from Fylgja by default (WP2.8). Lidskjalv's hook shrinks to what Lidskjalv itself still needs, such as reminder injection (A18), once Lidskjalv retires its own session source. That retirement is Lidskjalv's decision and is not scheduled here. Erik confirmed this reading of "thin forwarder" (Q11).

## Consequences

- No Fylgja code or test reads Lidskjalv's repository, cache, hook or installation; parity goldens come from one-off `spikes/` scripts and are committed.
- Fylgja's own async hook registrations are permanent, and Lidskjalv's hook never forwards events.
- After the WP2.7 dual-run gate, Lidskjalv reads sessions from Fylgja by default (WP2.8), and doctor and installer tests check the independence.
