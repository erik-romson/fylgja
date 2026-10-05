# 0005: Hook payloads are minimized before persistence

- Status: accepted
- Amendment: A5
- Amends: PLAN.md §5, §11
- Answers: —
- Relates to: A12
- Date: 2026-10-05

## Context

Privacy (§11). Derived state does not need those bodies.

## Decision

**Hook payloads are minimized before persistence.** The spool keeps the raw (size-capped) event only until commit. SQLite stores normalized, allowlisted fields: session id, event, tool name, notification type, reason, cwd, permission mode, edited file paths, and the prompt truncated to 300 characters. Tool input and response bodies are dropped.

## Consequences

- The ingest path normalizes events to the allowlisted fields before the SQLite commit.
- Tool input and response bodies never reach the store, and prompts are truncated to 300 characters.
- The spool keeps the size-capped raw event only until commit.
