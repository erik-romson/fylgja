# 0024: Lidskjalv keeps two data sources per module for now

- Status: accepted
- Amendment: A24
- Amends: PLAN.md §2, §9
- Answers: Q3, Q12
- Relates to: A18, A21
- Date: 2026-10-05

## Context

Fylgja will eventually replace these parts of Lidskjalv, but both must work for the time being.

## Decision

**Lidskjalv keeps two data sources per module for now** (Q3). Lidskjalv's builds, costs, quota and ARC/NOW stories keep running on their own code paths. As Fylgja gains equivalents (builds, costs and quota from WP5.4, stories from WP8.1), Lidskjalv gets one switch per module, `LIDSKJALV_SOURCE_<MODULE>=lidskjalv|fylgja`, defaulting to `lidskjalv`. A switch flips only after a parity check. Session data follows the same pattern (WP2.6). Its default flips to `fylgja` after the dual-run gate (WP2.8), and the `lidskjalv` session source keeps working until Lidskjalv retires it. Reminders may follow the same pattern later (Q12): Lidskjalv would store them as Fylgja session-scoped todos through the todo API and keep injecting them from its own synchronous hook. That is not scheduled here. If it happens, the fields it needs (an inject flag and a delivered timestamp) are designed at that point as a Fylgja todo-API change, without adding any Fylgja hook. Retiring Lidskjalv's own implementations is a later decision and is not scheduled here.

## Consequences

- Lidskjalv gets one switch per module, `LIDSKJALV_SOURCE_<MODULE>=lidskjalv|fylgja`, defaulting to `lidskjalv`, and a switch flips only after a parity check.
- WP5.4, WP5.5, WP8.1 and WP8.3 provide the Fylgja equivalents; the Lidskjalv-side work lives in the Lidskjalv repository.
- Retiring Lidskjalv's own implementations, and storing its reminders as Fylgja todos, are later decisions that are not scheduled.
