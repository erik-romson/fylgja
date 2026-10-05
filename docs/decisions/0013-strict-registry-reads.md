# 0013: Strict registry reads

- Status: accepted
- Amendment: A13
- Amends: PLAN.md §4.3
- Answers: —
- Relates to: A14
- Date: 2026-10-05

## Context

R5, plus the rule in §4.3.1 that undocumented key files are never read.

## Decision

**Strict registry reads.** Glob exactly `^[0-9]+\.json$` in `~/.claude/sessions/`, never open `*.key`, and parse unknown fields tolerantly.

## Consequences

- The Claude collector globs exactly `^[0-9]+\.json$` and never opens `*.key` files.
- Parsers tolerate unknown fields, and tests include a registry directory with decoy files.
