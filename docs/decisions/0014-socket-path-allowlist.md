# 0014: Socket path allowlist

- Status: accepted
- Amendment: A14
- Amends: PLAN.md §4.3.1
- Answers: —
- Relates to: A9
- Date: 2026-10-05

## Context

R6

## Decision

**Socket path allowlist.** A target socket must be in `/tmp/cc-socks` or `/tmp/cc-socks-<uid>`. The parent directory must be owned by the uid with mode 0700, neither path may be a symlink (checked with `lstat`), and the path must equal the registry row's path.

## Consequences

- WP3.6 validates parent directory ownership and mode, uses `lstat` to refuse symlinks, and compares the path with the registry row.
- Any failed check blocks the send action and is reported truthfully in the receipt.
