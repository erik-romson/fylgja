# 0003: Allowlist of environment variables in the hook envelope

- Status: accepted
- Amendment: A3
- Amends: PLAN.md §4.3 envelope
- Answers: —
- Relates to: A1
- Date: 2026-10-05

## Context

This gives exact host joins without scraping other processes' environments (`ps eww` exposes secrets such as `CMUX_SOCKET_CAPABILITY`).

## Decision

**Allowlist of environment variables in the hook envelope.** Beyond the session id, pid and `CLAUDE_CODE_MESSAGING_SOCKET`, the envelope may copy these terminal identifiers: `CMUX_WORKSPACE_ID`, `CMUX_SURFACE_ID`, `CMUX_TAB_ID`, `CMUX_PANEL_ID`, `ITERM_SESSION_ID`, `TERM_SESSION_ID`, `TERM_PROGRAM`, `CLAUDE_CODE_BRIDGE_SESSION_ID`. Nothing else is copied. Values are length-capped.

## Consequences

- `hook.py` copies only the listed variables, each length-capped, and nothing else.
- Tests assert that other variables (for example `CMUX_SOCKET_CAPABILITY`) never appear in an envelope.
- Host joins use these ids and never scrape other processes' environments.
