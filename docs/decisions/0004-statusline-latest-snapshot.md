# 0004: Statusline is a coalesced latest snapshot

- Status: accepted
- Amendment: A4
- Amends: PLAN.md §4.3 statusline forwarder
- Answers: —
- Relates to: A22
- Date: 2026-10-05

## Context

Claude allows only one statusLine command, and it renders often. Appending would flood the inbox.

## Decision

**The statusline is a coalesced latest snapshot, not an event stream.** `fylgja hook StatusLine` atomically replaces `statusline/<session_id>.json`, and the daemon reads it when its mtime changes. A one-line snippet in `statusline.sh` runs it in the background with output discarded, at most once per 2 s per session.

## Consequences

- `fylgja hook StatusLine` atomically replaces `statusline/<session_id>.json`; the daemon reacts to mtime changes.
- The statusline snippet runs in the background, output discarded, at most once per 2 s per session.
- The hook inbox never receives statusline traffic.
