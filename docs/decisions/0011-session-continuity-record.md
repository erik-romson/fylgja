# 0011: Session continuity record

- Status: accepted
- Amendment: A11
- Amends: PLAN.md §3 Session
- Answers: —
- Relates to: A1
- Date: 2026-10-05

## Context

Lidskjalv's lanes survive `/clear`; Fylgja's model has to as well.

## Decision

**Session continuity record.** Each session carries a `process_key` (pid + process start time) and a `predecessor` link, set when `/clear` rotates the session id. `/now` exposes `lane_key` = `process_key` for Lidskjalv.

## Consequences

- The store keeps `process_key` and a `predecessor` link on sessions, set when `/clear` rotates the session id.
- `/now` exposes `lane_key` equal to `process_key` for Lidskjalv.
- Fixtures from the ratatosk `clear-model-session` scenario back the regression tests.
