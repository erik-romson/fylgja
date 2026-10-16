# 0055: Attention alerts

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A6, A7, A8, A10, A17, A19
- Date: 2026-10-16

## Context

On 2026-10-15 one session of kind `input` sat among 11 waiting entries in `/now` `waiting[]` and went unnoticed for a
long time. The page showed the entry, but nothing told the person to look. Fylgja knows when an agent waits (the
state engine, the live view and `waiting[]`), so the daemon can say so itself.

## Decision

- **The notifier reads what `/now` shows** (R1). `shown_waiting(state)` in `core/shown.py` holds the availability
  filter that `build_now` used, so `waiting[]` and the notifier share one list. Only entries whose kind is in
  `[notify] kinds` count; the default is `permission` and `input` (D4).
- **Episodes** (R2, R10). A session episode is keyed by `(target_kind, target, kind, since)`. A project or window
  episode is keyed without `since` while it is listed, because `since` can move during one occurrence; it keeps
  `last_since`, and after it has left, a `since` later than `last_since` is a new occurrence. A closed episode is
  remembered for 24 h on the monotonic clock, and a return of the same occurrence continues its old state.
- **Grace** (R3). It counts from the entry's `since`, then runs on the monotonic clock, so sleep does not count. An
  entry first seen late is due after a 5 s floor (`LATE_SIGHT_FLOOR`), not at once and not a full grace later; the
  floor gives the collectors' first snapshots time to close rows that ended while the daemon was down.
- **Focus per window, by family** (R4–R6, D5). A family is a session with its `parent_key` ancestors. While the
  person is not away and the span tracker names a focused window, every family with a shown live session in that
  window is on screen, and its alerts, reminders included, are held until it leaves the screen.
- **One reminder** (R7), after `reminder_seconds` and only while the episode is open.
- **Channels** (R8). Away (`spans.tracker.suspended` non-empty) uses `away_command` when set; otherwise
  `desk_command`; with neither, due episodes stay unannounced. `desk_detail` defaults to `project` and `away_detail`
  to `kind` (D3), so a folder and a reason never reach the phone.
- **Coalescing and text** (R9, R11). Everything due in one evaluation is one alert, counted in families. Labels come
  from the session kind, never from a table of collector names.
- **Delivery** (R12–R14). Placeholders `{title}`, `{message}`, `{url}` and `{key}` are substituted in one pass per
  argv element, with no shell. `{key}` is empty unless the alert names one session with a safe key. A send goes
  through `Daemon.runner` with a timeout; the outcomes are `sent`, `command_timeout`, `command_missing` and
  `command_failed`. Episodes are marked announced before the send and a failed send is not retried. Logs carry
  counts, kinds and outcomes, never a title, message, folder, reason or argv element.
- **Evaluation** (R15). The notifier is its own task. It evaluates at start, after every live-view update, after
  every tick and at the next deadline, so a 20 s grace is not delayed by the 30 s tick.
- **Built only when enabled** (R16). `[notify] enabled = false` builds no notifier and starts no task. This departs
  from the summary service, which is always constructed.
- **Page signal** (R17). A new route, `GET /view/attention` (`fylgja.view.attention/1`), gives the tab title and the
  icon for the count of rank-0 waiting entries, and a second static icon, `/favicon-attention.png`, carries the dot.
  `fylgja.now/1`, the SSE payload and the CSP do not change. The signal does not depend on `[notify]`.
- **Status** (R18). `/inspect/collectors` and `/inspect/pipeline` carry a `notify` block. The Collectors tab shows it
  in an "Alerts" section with the last failure as `notify: <kind>`, and the Data flow tab shows the counters. It is
  not a collector health row, so `/now` `degraded[]` and `/health` are unchanged. Doctor adds `notify:*` checks only
  while alerts are enabled, and prints at most `argv[0]`.
- **Desk command** (D1). The documented tool is terminal-notifier 3.1.0 (3.0.0_1 at the least): it is in
  homebrew-core, current, has a macOS 27 bottle, and has a click action and a replace group.
- **Phone command** (D2). Public ntfy.sh through `curl`, with a random topic of at least 20 characters; doctor warns
  about a shorter one.
- **Restart seed fix** (D7). At start the stored hook state is re-applied only to sessions that have a stored hook
  event. Before, a norn run that waited across a restart stayed `waiting_input` after it resumed.
- **Click to focus, the page as fallback** (R19, D6). A new CLI command, `fylgja session focus <ID> [--else-open]`,
  beside `session message`, under A7 and A8. It posts `/focus/session/{key}` and, with `--else-open`, opens
  `/#recall` when the focus cannot be done. The click action runs through terminal-notifier's shell, and the
  character rule of R12 for `{key}` keeps it safe from injection.

## Consequences

- A restart gives one re-announcement of an entry that still waits: 5 s after the start when it is older than the
  grace, otherwise at the end of its remaining grace. Episode state is not persisted.
- Without the presence collector the person is never away, so there is no phone channel.
- No desk banner appears for any session in the focused window, other cmux tabs included, until focus leaves the
  window or the person goes away.
- A click on a coalesced banner opens the page, not a session, because `{key}` is empty.
- Whether banner clicks work at all on a given macOS cannot be detected by a program; the runbook checks it by hand,
  and the `-open {url}` variant is the fallback.
- Failed sends are not retried and there is no cooldown between alerts for a flapping run.
