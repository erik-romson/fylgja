# 0036: OpenCode collector: stale turns, the inbox as a wake-up and shared scan code

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A5, A11
- Date: 2026-10-06

## Context

WP3.2 says an incomplete assistant message means busy, and that when the optional TypeScript plugin is installed the
collector drains `hook-inbox/opencode/` with the same machinery as Claude. The revalidation
(`docs/revalidation/2026-10-06-opencode.md`) found three things the plan does not say:

- `opencode.db` keeps dead turns: 3 of 1500 assistant messages have no `time.completed` and no error, and all three are
  at least 33 days old (the process died mid-turn). Read literally, the rule would show such a session busy for ever.
- The hook state machine (decision 0033) maps Claude's event names to states. The plugin's events (`session.status`,
  `permission.asked`, `todo.updated`) have no counterpart there, and a hook state that nothing ever clears would
  override the database state for the rest of the session.
- The process scan (`pgrep`, `lsof`, `ps` and their parsers) is the one Codex already has.

## Decision

- **Stale turns.** A message is a turn in flight only if it is the newest assistant message of the session, has no
  `error`, and the newest of the message, the session and the newest tool part was updated within
  `busy_stale_seconds` (default 1800). Anything else is `idle`.
- **The inbox is a wake-up, the database is the truth.** The OpenCode collector drains its inbox with
  `claude.inbox.InboxDrainer` (parameterized by source and kind, so no copy) through a sink wrapper that wakes the poll
  loop after every committed envelope. The envelopes become `hook_event` records (event name, session id, cwd, status
  `reason` or permission `tool_name`) and are stored, but the plugin's events name no state in the reducer, so they
  change no session state. The state in `/now` is always what the database says, at most one DB read after the event.
- **Shared scan code.** The OpenCode adapter imports the process-scan helpers (`list_pids`, `list_open_files`,
  `read_process_table`, the real runner and filesystem) from the Codex collector's adapter and its pure process
  parsers, and only adds the database reader. The collectors do not depend on each other for anything else. If a third
  process-scanning collector appears, the helpers move to a shared module.
- **Several processes in one directory.** They take the directory's newest sessions, matched by age. This is a
  heuristic; a wrong pairing swaps two sessions of the same project, never a project.
- **Todos.** The `todo` table travels as `facts.tasks` with the shape of Claude's list; the `in_progress` row is the
  open task. Step 35 imports from it.
- **Plugin file.** One export (`FylgjaPlugin`), because OpenCode calls every export. Only the three events of the plan
  are forwarded. No installer is added.

## Consequences

- A session whose process was killed mid-turn shows idle after at most 30 minutes, and a turn longer than that with no
  tool output and no database update would show idle early. Raise `busy_stale_seconds` for such work.
- A TypeScript plugin that is installed gives a faster `/now`, never a different one, so removing it changes nothing but
  latency, and a broken plugin cannot corrupt the state.
- Permission waits are still invisible (they are not in the database); a future change that maps `permission.asked` to
  `waiting_permission` needs the matching `permission.replied` event too, which the plan does not forward.
- Both sides of the inbox contract are tested through `tests/fixtures/envelope/opencode-session-status.json`.
