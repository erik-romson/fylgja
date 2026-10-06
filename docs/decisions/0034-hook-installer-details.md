# 0034: Hook and statusline installer details

- Status: accepted
- Amendment: none
- Amends: —
- Answers: Q5, Q10, Q11
- Relates to: A2, A4, A21, A22
- Date: 2026-10-06

## Context

WP2.4 adds `fylgja install hooks` and `fylgja install statusline`, plus doctor checks for them. The plan fixes
the event set (A2), the async guarded command (A22), dotfiles persistence and no commits (Q10), and independence
from Lidskjalv (A21). It leaves open how the installer edits JSON it does not own, how the Claude version filters
events, and what "Lidskjalv's entries are untouched" means for doctor.

## Decision

- The command is `[ -x "$HOME/.local/bin/fylgja" ] && "$HOME/.local/bin/fylgja" hook <Event>; exit 0`. A Fylgja
  entry is any hook whose command matches `fylgja"? hook <Event>`. Each event gets one group of its own with
  matcher `""`, appended after the entries already there; an existing Fylgja hook is repaired to the canonical
  command and `async: true` and keeps any other key.
- `settings.json` is rewritten with `json.dumps` at the file's own indent. When the unmodified file would not
  round-trip byte for byte, the installer refuses and touches nothing, so the diff can only add Fylgja entries.
- The version table (`CLAUDE_EVENT_MIN_VERSION`) holds 2.1.251 for the model-switch events, the only floor the
  hooks reference states, and 2.1.0 for the others. An event not in the table is never registered. An unknown
  Claude version refuses the install.
- The statusline block goes right after the `input=$(cat)` line, which the snippet reuses. A stale block is
  replaced in place; broken or duplicate markers, or a script without that line, refuse.
- Doctor adds `hooks:executable`, `hooks:async`, `claude-version`, `inbox:access`, `inbox:backlog`,
  `inbox:dropped`, `dotfiles:hooks-committed` and, only when Lidskjalv's hooks are present, `lidskjalv-hooks`.
  The last is `informational`: shown as `INFO`, never counted in the summary or the exit code. "Untouched" means
  the entries still have only `type`, `command` and `timeout` and their target exists.

## Consequences

Reruns are no-ops and make no backup. A user whose `settings.json` has unusual formatting must run `jq .` once.
Raising an event's version floor is a one-line change. Doctor's Lidskjalv check can be deleted with Lidskjalv
without touching any other check.
