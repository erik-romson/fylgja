# 0020: Windows and session snapshots are stored as change intervals

- Status: accepted
- Amendment: A20
- Amends: PLAN.md §5 table semantics
- Answers: —
- Relates to: A12
- Date: 2026-10-05

## Context

40 windows × every 30 s comes to about 115k rows a day for no added information.

## Decision

**Windows and session snapshots are stored as change intervals** (`valid_from`/`valid_to`), not full copies per tick.

## Consequences

- The store schema uses `valid_from`/`valid_to` rows and the writer closes and opens intervals on change.
- Queries for "state at time T" use interval containment, and write volume stays near the number of real changes.
