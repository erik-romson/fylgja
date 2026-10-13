# 0049: Focus spans: sleep and window folders

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A12, A17
- Date: 2026-10-13

## Context

A focus span stays open until a focus change, idle, a lock or shutdown. No sleep signal exists: the presence
collector reports a lock only after the machine wakes, stamped at wake time. A suspended Mac therefore keeps the open
span running, and the sleep counts as focus time.

## Decision

- **Sleep.** The daemon's tick loop compares the wall clock between two ticks. A jump of more than
  `tick + idle_threshold` (330 s by default) means the machine slept. The open span is closed at the last tick with
  `end_reason = idle`, and a new span for the still-focused window opens at the first tick after the jump, unless idle
  or lock still hold. A shorter jump is not worth a split.

## Consequences

- No migration: `idle` is an existing `end_reason`, and it now also covers a suspended machine.
- History totals exclude the time the machine slept.
- Tests move the wall clock without moving monotonic time with `FakeClock.suspend`.
