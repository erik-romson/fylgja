# 0043: Application context, second tier: details

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A1, A15
- Date: 2026-10-06

## Context

WP5.3 adds the `appcontext` collector: title parsers on every window and a second tier on focus changes. The plan fixes
the sources (cmux, Chrome, JetBrains, Obsidian, iTerm2), the 3 s AppleScript timeout and "a timeout disables only its
own switch, until doctor is rerun" (§4.4). It leaves open where the collector gets windows and focus, how cmux's selected
workspace is found, where "disabled until doctor is rerun" is kept, and how context reaches `/now` without a schema
change. `docs/revalidation/2026-10-06-appcontext.md` found that cmux's store has no selection and no `active_for_*` keys.

## Decision

- **Windows and focus come from the store, read-only,** like the presence and project collectors: the open windows and the
  latest focus event, polled every 0.5 s (`[collectors.appcontext] poll_interval_seconds`). The titles are therefore already
  redacted when they are parsed; the ingest gate redacts the record again, which is idempotent. A focus event is a focus
  change: the second tier runs once per event, on a window that is open, and never between events.
- **cmux.** The selected workspace is not recorded anywhere the daemon can read (the store lists the active session of
  every workspace; the CLI is refused outside cmux, decision 0025). The collector reads `activeSessionsByWorkspace` and
  guesses, stating its confidence: a workspace whose working directory has a path component equal to the window title is
  `medium`, a lone workspace is `medium`, the most recently active is `low`. Surface (tab) and cwd come from the session.
  `launchCommand`, argument arrays and `transcriptPath` are never read.
- **Switch state.** The switches stay in `[actions.applescript]`, read when the collector starts. "Disabled until doctor
  is rerun" is a small file, `<state dir>/applescript-switches.json`, with `{ok, at, reason}` per switch. Doctor is its only
  writer (a timeout or a refused permission writes `ok = false`, an answer writes `ok = true`); the collector only reads it.
  A switch the collector disabled itself on a timeout comes back when the file holds an `ok = true` record newer than that
  moment. Doctor therefore changes one file, and only for a switch that is on.
- **Chrome URLs** keep scheme, host, port and path; the query, fragment and user information are dropped. Only http and
  https URLs are kept. History backfill (off by default) lists at most `history_limit` recent visits the same way.
- **Context in the daemon.** The store has no table for window context, so `ContextMemory` (`core/context_items.py`) keeps
  the current item per window and key from the records after the gate, like the session facts. A closed window forgets its
  items; a record with `data.gone` withdraws one; `/now` shows items of available collectors only. After a restart the
  items return with the collector's next snapshot. Records carry `data.window_id`, `key`, `tier` and `fields`, so any plugin
  with the `context` capability can attach context to a window.
- **The one-liner.** The title-tier item's `title` is a short summary of the parsed title (`parse.title_summary`); the
  workspace holding the focused window uses it as the third source of its one-liner (§4.6).

## Consequences

- `/now` and `docs/api/now.schema.json` are unchanged: `focused.context[]` and `workspaces[].windows[].context[]` already
  exist.
- cmux context can be wrong when several workspaces qualify; `confidence` says so, and the by-hand check in the revalidation
  log decides whether the guess needs a better signal (a cmux upgrade that exposes the selection, or the CLI from inside
  cmux).
- Tier 2 sees a window's title when focus moves, not when the title changes later; the next focus event refreshes it.
- A switch disabled by a timeout stays disabled across daemon restarts until `fylgja doctor` has answered.
