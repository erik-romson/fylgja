# 0028: The Claude task list comes from the transcript, and the facts block gains `tasks`

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A21
- Date: 2026-10-05

## Context

R7 assumed `~/.claude/tasks/<sessionId>/*.json` holds a session's task list. It does not exist on 2.1.289, and no
`.claude/tasks` string is in the `claude` binary (`docs/revalidation/2026-10-05-claude-transcripts.md`). The binary
defines `TodoWrite` and the task tools `TaskCreate`, `TaskUpdate`, `TaskList` and `TaskGet`, but no local transcript
holds a task-tool call; `TodoWrite` is the only one observed. Lidskjalv's `_SUMMARY_JQ` reports `todos` from the last
`TodoWrite` only.

## Decision

`facts.tasks` (`collectors/claude/facts.py`) is read from the transcript's `tool_use` records. It has the shape of
Lidskjalv's `todos` (`{status, content}`, at most 20, `content` cut to 80) and is identical to it when a transcript
uses `TodoWrite` alone, which the parity test asserts. `TodoWrite` replaces the list, `TaskCreate` appends a `pending`
task, and `TaskUpdate` changes `status` or `subject` of the task with that id (ids count from 1 in creation order) or
removes it on `deleted`. No task directory is read. The open task for the one-liner (§4.6 rule 4) is the first task
whose status is `in_progress`.

The goldens remain Lidskjalv's own output (A21). The parity test compares every other field with them, and compares
`tasks` with the golden `todos` only for transcripts that use no task tool; the task tools have synthetic fixtures.

## Consequences

- The shape of a `TaskCreate` result and the id it assigns were not observed; the id rule is an assumption, recorded in
  the revalidation log with a by-hand check.
- Task tools called from a sidechain (a subagent) also count, as `todos` does in Lidskjalv.
- If Claude later persists tasks on disk, a new collector input replaces the transcript source; the `facts.tasks`
  shape does not change.
