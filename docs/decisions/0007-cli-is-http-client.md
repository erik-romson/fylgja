# 0007: The CLI is an HTTP client of the daemon

- Status: accepted
- Amendment: A7
- Amends: PLAN.md §6, §10.1 "sharing application services"
- Answers: —
- Relates to: A8
- Date: 2026-10-05

## Context

Keeps a single writer and avoids running collectors in two processes.

## Decision

**The CLI is an HTTP client of the daemon** (stdlib `urllib`, no new runtime dependency). The exceptions are `hook`, `serve`, `doctor` and `install`. The daemon is the only SQLite writer. Read commands exit with code 3 and a clear message when the daemon is down.

## Consequences

- The CLI uses stdlib `urllib`, so no new runtime dependency is needed.
- Only `hook`, `serve`, `doctor` and `install` run without the daemon; the others exit 3 with a clear message when it is down.
- The daemon's writer task stays the only SQLite writer.
