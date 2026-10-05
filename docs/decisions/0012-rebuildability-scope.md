# 0012: Rebuildability is scoped to the raw-event retention window

- Status: accepted
- Amendment: A12
- Amends: PLAN.md §5
- Answers: —
- Relates to: A20
- Date: 2026-10-05

## Context

§5 says both "everything derived is rebuildable" and "raw events 30 days".

## Decision

**Rebuildability is scoped to the raw-event retention window** (30 days). Derived rows older than that (spans, snapshots) are retained archives and are not rebuildable.

## Consequences

- Retention (WP1.x) keeps raw events for 30 days; rebuild tests use data inside that window only.
- Spans and snapshots older than the window are retained archives and are never pruned by a rebuild.
- Documentation states the scope wherever "rebuildable" appears.
