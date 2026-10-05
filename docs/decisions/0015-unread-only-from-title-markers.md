# 0015: Unread chat suggestions come only from explicit title markers

- Status: accepted
- Amendment: A15
- Amends: PLAN.md §7
- Answers: —
- Relates to: —
- Date: 2026-10-05

## Context

§7 ranking item 6 versus §13.1 "no claim of an authoritative unread count".

## Decision

**Unread chat suggestions come only from explicit title markers** (for example `(3)` or `*` prefixes), confirmed per app during revalidation. Where no marker exists, nothing is inferred.

## Consequences

- WP5.3 confirms the markers per app during revalidation and records them.
- Apps without a marker contribute nothing to ranking item 6, and the UI never claims an authoritative unread count.
