# 0033: The session state engine sits in the ingest tap

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A11, A5, A20, R9
- Date: 2026-10-05

## Context

WP2.3 turns hook events into the live session state. The registry collector already writes a *complete*
`session_activity` snapshot every few seconds, and the writer stores the full current state of an interval entity
(A20), so two sources writing the state race: the registry's next poll would overwrite a hook state with `idle` or
`busy`. Hook events are stamped when the hook started, which can precede a registry poll that was written in
between, and the store ignores an observation older than its open interval. The `fylgja.now/1` schema already
holds every state value this step needs.

## Decision

- **A pure reducer** in `fylgja.core.sessions`: `reduce(state, event) -> state` over `HookSignal`,
  `RegistrySignal` and `FocusSignal`. It is the table of WP2.3, plus `PostToolUse`, `PostToolUseFailure` and
  `PermissionDenied` → `working` (the tool ran or was refused; without them a granted permission would stay
  `waiting_permission` until the turn ends). Events that name no state (`SessionStart`, subagent and task events,
  other notification types) change nothing and do not end the registry fallback.
- **An `idle_prompt` does not overwrite an unseen `turn_done`.** Claude sends it a minute after a Stop; idleness
  is already implied, and replacing the state would erase the "finished and not yet looked at" signal that
  waiting-on-me (step 43) and ranking (step 44) need. After `seen`, or from any other state, `idle_prompt` gives
  `idle` as the table says.
- **Ordering.** A hook event older than the newest one applied (by `received_at_ns`, then `observed_at`) changes
  nothing. The record carries `observed_at` only (nanoseconds are rebuilt from it at microsecond resolution), so
  two events inside one microsecond are applied in arrival order.
- **An engine in the tap** (`SessionStateEngine`, wired in `Daemon`) is a *filter* between the ingest gate and the
  writer. A `hook_event` is reduced and a change of state is written as a derived `session_activity` event merged
  over the last activity the registry reported. Every `session_activity` record of any source passes through the
  engine and has the hook state laid over it while the session has one, so the registry cannot undo it; before the
  first hook the registry's state stands. A `focus` event marks the `turn_done` sessions of the focused window
  `seen` (the window is the resolver's join), only when the focus is *after* the Stop.
- **Derived records never precede what was written.** Their `observed_at` is raised to the newest activity the
  engine has passed on for that session, because the store would drop them otherwise. The state's own `since`
  keeps the true event time, which the `seen` rule compares against.
- **Hooks-only sessions** are announced with a `session` event (pid, cwd, predecessor) at first sight. The
  registry stays the authority for liveness: its next complete snapshot ends a session it does not list. A session
  that ends with `SessionEnd` shows state `ended` until the registry drops it.
- **The A11 link by hook**: a new session id under the pid of a session that just ended with reason `clear` names it
  as predecessor. A registry row's `predecessor_key` (the step 11 continuity record) is taken as well.
- **After a restart** the engine seeds itself from the stored sessions in states only hooks produce
  (`running_tool`, `waiting_permission`, `waiting_input`, `turn_done`, `seen`, `error`, `ended`).
- **Hook events of ignored working directories** (`ignore_cwd`, such as Fylgja's own summarizer sessions) are
  drained and dropped by the Claude inbox, so they never create a session.

## Consequences

- `/now` shows `running_tool`, `waiting_permission`, `waiting_input`, `turn_done`, `seen`, `error` and `ended`.
  `docs/api/now.schema.json` is unchanged.
- One-liner rule 1 fires for `waiting_permission` ("Waiting for permission: <tool> <target>") and `waiting_input`
  ("Waiting for input: <reason>"); `running_tool` counts as working for rules 3 and 4.
- A window that is already focused when the turn ends keeps `turn_done` until it is focused again, as WP2.3 says
  (focus *after* the Stop). A later step may refine this.
- A second Stop stamped between a first Stop and the focus that marked it seen, but received after that focus,
  cannot be re-ordered (the engine reduces as events arrive). The effect is a `turn_done` that the next focus
  clears.
- The engine remembers at most 4096 sessions and 256 rotations in flight; `forget` runs when a session leaves the
  live view.
