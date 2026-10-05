# 0002: Hook event set is a superset of Lidskjalv's

- Status: accepted
- Amendment: A2
- Amends: PLAN.md §4.3 event list
- Answers: —
- Relates to: A21
- Date: 2026-10-05

## Context

Phase 2 requires that "both systems receive equivalent events" (R8).

## Decision

**The Fylgja hook event set is a superset of Lidskjalv's.** It covers SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, PermissionRequest, PermissionDenied, Notification (matcher `""`, type read from the payload), Stop, StopFailure, SubagentStart, SubagentStop, TaskCreated, TaskCompleted, PreCompact, PostCompact, PostModelSwitch, CwdChanged and SessionEnd (matcher `""`, reason read from the payload). High-rate events such as `MessageDisplay` and `FileChanged` are excluded. The set is configurable, and the installer registers only events the installed Claude version supports.

## Consequences

- The installer (WP1.10, WP2.4) registers the configurable event set, limited to events the installed Claude version supports.
- High-rate events such as `MessageDisplay` and `FileChanged` stay out of the default set.
- Phase 2's "equivalent events" criterion can be checked against this list.
