# 0035: Codex collector: turn ends, the `busy` state and degraded listing

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A11, A5
- Date: 2026-10-06

## Context

WP3.1 reads Codex sessions from `pgrep`/`lsof`, the read-only `state_N.sqlite` and the rollout tail. The plan says
busy or idle is `task_started` against `task_complete`, and that an unknown `state_N` marks the plugin degraded
without guessing. The revalidation (`docs/revalidation/2026-10-06-codex.md`) found three things the plan does not
say:

- a turn the user interrupts ends with `turn_aborted` and no `task_complete` (49 aborts against 237 starts and 202
  completes in 117 rollouts);
- the one-liner's tool rule fires only for `working` and `running_tool`, while `fylgja.now/1` names the Codex states
  `busy` and `idle`;
- an unknown database version could mean "show nothing" or "show what the process and rollout still tell".

## Decision

- **Turn ends.** `task_complete` and `turn_aborted` both close a turn. The newest of `task_started`,
  `task_complete` and `turn_aborted` decides; a rollout with none of them has no state (`None`, shown as no state),
  never idle. A function call left over from an earlier turn is not current.
- **State names.** The collector emits `busy` and `idle`, as the `SessionState` vocabulary says. `busy` is added to
  `core/one_liner.WORKING_STATES`, so the tool rule gives "exec_command pytest tests/…" for a busy Codex session.
  Claude's `busy` is still mapped to `working` by its own collector.
- **Unknown or changed schema.** The newest `state_N.sqlite` decides. An `N` outside
  `parse.SUPPORTED_STATE_VERSIONS` is `degraded` with `error_kind` `unknown_state_schema` and the file is not opened;
  a known `N` whose `threads` table lacks a required column is `state_schema_mismatch`. In both cases the sessions
  are still listed from the process, the `lsof` working directory and the rollout (no title, branch, name or
  version). Nothing is read from a layout nobody revalidated; the rollout format is a different, revalidated source.
- **Reads stay inside `~/.codex`.** A rollout that the process has open but that lies outside the Codex directory
  (symlinks resolved on both sides) is never read. `auth.json`, `history.jsonl` and the `logs_*.sqlite` files are
  never opened.
- **Hooks and `notify`** are documented, off by default and not installed; the collector does not drain
  `hook-inbox/codex/`.

## Consequences

- A Codex upgrade that bumps `state_N` shows in `fylgja doctor` and `/collectors` the same day, and the sessions
  keep appearing without titles until a revalidation adds the version to `SUPPORTED_STATE_VERSIONS`.
- A process with no rollout open (a fresh `codex` with no conversation yet) is not a session: it has no thread id
  to key on. The doctor detail counts these.
- Adding `busy` to `WORKING_STATES` changes no existing golden: no other collector emits it.
