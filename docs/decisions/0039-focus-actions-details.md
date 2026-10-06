# 0039: focus actions, details

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A1, A6
- Date: 2026-10-06

## Context

WP3.5 adds `POST /focus/window/{id}` and `POST /focus/session/{key}`. Decision 0025 left the cmux selection route open
(AppleScript `focus` on the terminal whose id is the surface id, unconfirmed; the CLI only if reachable). The
`[actions.applescript]` table has `chrome` and `iterm2` but no cmux switch. A session's iTerm2 id is not part of its
host record.

## Decision

- **cmux** tries, in order, the AppleScript `focus` (`[collectors.cmux] applescript_focus`, default true) and the CLI
  `select-workspace` plus `focus-panel` (`[collectors.cmux] cli_focus`, default false). Each runs with a 3 s timeout
  through the process runner. A failure of every enabled route is window level and `via` reads
  `aerospace (cmux: <reason>)`. The two keys sit in the free `[collectors.cmux]` table; a future cmux collector that
  validates its settings must accept them.
- **JetBrains** and every other host: window level, `via = "aerospace"`.
- **iTerm2** runs only with `[actions.applescript] iterm2 = true`. The UUID comes from the `ITERM_SESSION_ID` of the
  session's latest hook event, remembered in memory by `SessionMemory.terminal_id` (not persisted). Without it the
  result is window level (`iterm2: no session id`).
- ids reach `osascript` as arguments (`on run argv`), never inside the script text, and only after a shape check.
- A failing `aerospace workspace` is ignored (focusing the window switches the workspace); a failing
  `aerospace focus` is a 502. A session without a window is a 409; an unknown window id or session key is a 404.
- `Daemon.runner` exposes the process runner so the focus routes use the same seam as the host adapters.

## Consequences

- Tab-level cmux focus is unconfirmed until Erik runs `tests/live/test_focus_live.py`; until then a wrong guess shows up
  as window level with a reason, never as a wrong tab.
- `via` is a short reason vocabulary (`timeout`, `exit N`, `not installed`, `could not run`, `no surface id`,
  `no session id`, `session not found`, `applescript disabled`, `tab focus disabled`); command output is never put in it.
