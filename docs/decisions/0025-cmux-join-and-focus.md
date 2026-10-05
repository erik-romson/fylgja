# 0025: cmux join and focus method

- Status: accepted
- Amendment: none
- Amends: —
- Answers: Q4
- Relates to: A1, A3
- Date: 2026-10-05

## Context

A1 makes cmux the primary host (Q4) and leaves the choice of join method to the WP0.4 spike: the cmux CLI if it can
list surfaces with their tty, pid or cwd and works without the token, otherwise the allowlisted `CMUX_*` ids from
the hook envelope (A3). The spike (`docs/spikes/0003-cmux.md`, `docs/revalidation/2026-10-05-cmux.md`, cmux 0.64.25
build 106) found:

- Outside a cmux terminal every CLI call is refused ("only processes started inside cmux can connect"). The default
  socket mode is `cmuxOnly`, an ancestry check, so a launchd daemon is refused whether or not it holds a token. The
  listing fields (tty, pid, cwd) could not be observed and the shipped docs do not name them.
- cmux keeps its own record of Claude sessions in `~/.cmuxterm/claude-hook-sessions.json` (also printed by
  `cmux sessions --json`, which needs no socket). It maps a session id and pid to a cmux workspace id and surface
  id. For the 8 cmux-hosted registry rows today, 8 of 8 match on pid and session id.
- cmux's AppleScript dictionary (`cmux.sdef`) has a `focus` command on a terminal ("bringing its window to the
  front") and a `select tab` command that selects a cmux workspace. Neither was run.
- The cmux window title is one bare token and is not a join key.

## Decision

**Join method.** The CLI is not used for the join. A cmux-hosted session is joined to its cmux workspace and surface
by, in this order:

1. **Hook-time ids (A3), from Phase 2 on.** The hook envelope's `CMUX_SURFACE_ID` (and `CMUX_WORKSPACE_ID`,
   `CMUX_TAB_ID`, `CMUX_PANEL_ID` when set) are exact (`via = cmux-hook-env`, `confidence = high`).
2. **cmux's session store, from Phase 1 (WP1.8).** The cmux adapter reads
   `~/.cmuxterm/claude-hook-sessions.json` read-only and matches a registry row by `session_id` and `pid`
   (`via = cmux-session-store`, `confidence = high`; a `session_id` match with a different pid is `medium`). The
   parser takes an allowlist of fields (`session_id`, `pid`, `workspace_id`, `surface_id`, `cwd`,
   `agent_lifecycle`, `stored_pid_exists`) and discards everything else at parse time. It never stores or logs
   `launch_arguments`, `stored_pid_arguments` or `transcript_path`: they can carry secrets. It does not use
   `cmux sessions`, so it needs no subprocess. The file is a private cmux format with `version: 1`, so the adapter
   requires `version == 1` and the allowlisted keys, and a record with a dead pid (`stored_pid_exists` false) is not
   joined.
3. **Window level**, through the host-generic join of A1 (`via = parent-chain`, `confidence = window-only`).

The cmux window title is never a join key.

**Focus method.** The CLI selection commands (`select-workspace`, `focus-panel`, `focus-pane`, `focus-window`)
cannot be used from the daemon under launchd in the default `cmuxOnly` mode, and the docs do not say whether they
raise the macOS window. So focus does not depend on the CLI. Step 29 focuses a cmux-hosted session in this order:

1. `aerospace focus --window-id` for the cmux window (this raises it and switches the AeroSpace workspace);
2. **recommended alternative, to be confirmed by hand before step 29:** the AppleScript `focus` command on the
   terminal whose id is the session's surface id, run with `osascript` under the plan's 3 s timeout and the
   doctor's TCC check (§4.4). The dictionary says `focus` brings the terminal's window to the front; whether a
   terminal `id` equals the cmux surface id, and whether it selects the right workspace and tab, are `unknown`.
   Run by hand: `osascript -e 'tell application "cmux" to focus (first terminal whose id is "<surface-id>")'`
   with a surface id from the store file;
3. optional, behind `[collectors.cmux] cli_focus = false` (off by default): the CLI, which works only if the daemon
   runs inside a cmux terminal, if cmux is started with `CMUX_SOCKET_MODE=allowAll` (which lets any local process
   drive cmux, so it is not recommended), or if the socket password lifts the ancestry check (`unknown`). Fylgja
   never reads `CMUX_SOCKET_CAPABILITY` from another process.

In the CLI/AppleScript vocabulary a cmux **workspace** is called a "tab" by `cmux.sdef` (`select tab` selects a
workspace). Fylgja's model and UI say *cmux workspace* and *surface*, per A1.

**Fallback.** Window-level focus through AeroSpace (`aerospace focus --window-id`, preceded by the AeroSpace
workspace), returned as `{focused: "window", via: "aerospace"}`. It is also the result whenever step 2 above fails
or times out, and while the AppleScript route is unconfirmed.

**Version detection.** The adapter reads the cmux version from `cmux --version` (works without a socket; first line
`cmux <semver> (<build>) [<sha>]`) or, with no subprocess, from `CFBundleShortVersionString` and `CFBundleVersion`
in `/Applications/cmux.app/Contents/Info.plist` read with `plistlib`. The path comes from the AeroSpace window's
bundle id `com.cmuxterm.app` and falls back to the default path. The version and build go into the collector's
status. Tested versions are a `[collectors.cmux] tested = ["0.64"]` list. On an **unknown version** (not in the
list, or unparseable) the adapter does not guess: it keeps the read-only store parse only if the file passes the
shape check above, otherwise it joins at window level; it disables the AppleScript and CLI focus steps unless
`[collectors.cmux] trust_unknown_version = true`; and it logs one WARN naming the version (never args or ids) and
marks the collector `degraded` with the reason `unknown cmux version`. A cmux upgrade therefore degrades to window
level rather than failing. The shape check is part of the contract tests.

## Consequences

- WP1.8 and step 13 add a cmux host adapter with the order above. A captured, sanitized store file goes into
  `tests/fixtures/` (ids replaced), and a test replays it. The join table of step 03 gets the cmux rows resolved
  for 8 of 8 on today's data.
- Step 14 and the model keep `via` and `confidence` values for the new ids: `cmux-hook-env`, `cmux-session-store`.
- Step 29 builds the focus actions from the order above. Its live smoke test is the by-hand confirmation of the
  AppleScript route; if the route fails, the focus result stays `window` and the decision is revisited with a
  new record.
- The privacy rules of §4.4 still hold: the store file is not on the protected list, but only allowlisted fields
  are kept, and the doctor reports the file's presence, mode and version without printing content.
- A cmux upgrade can rename the store file or change its format. The version gate and the shape check turn that into
  `degraded`, not into a wrong join. Hook-time ids (A3) remain the stable path once Phase 2 ships.
- Unconfirmed by hand: the launchd refusal (`launchctl submit` probe in the revalidation log), the AppleScript
  `focus` route, whether the window title follows the selected workspace, and the field names of `cmux tree --json`.
