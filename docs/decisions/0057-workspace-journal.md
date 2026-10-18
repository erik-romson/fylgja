# 0057: Workspace journal

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A8, A12, A20, 0018, 0046, 0053, 0055, 0056
- Date: 2026-10-18

## Context

Fylgja knows what happens in each checkout (sessions, commits, todos, focus, runs) but keeps no account of it.
After a break the person reads cards and summaries and reconstructs the thread of work by hand. The audit of the
code found that the summary service shares neither its runner nor its slots, that `Facts` cannot be diffed by a
watermark, that a finished run is visible to the core only as a state, and that collectors reach the daemon only
through records. The journal is a persistent, per-thread account that a cheap model writes at natural boundaries
and that plain code keeps as a ledger of open items.

## Decision

- **Thread identity (J1).** A thread is one checkout plus its current branch. A checkout is a `projects` row whose
  `repo_root` equals its `path`. `thread_id` is `item_id("thread\0" + repo_root + "\0" + branch)`. A detached HEAD,
  a missing row or a path that is not a checkout has no thread. Paths are lexical (Assumption 24).
- **Input (J2) and boundaries (J3).** Changes in a checkout dirty its current thread. Soft boundary: a turn
  ended and went quiet. Hard boundaries: session end, run finished, focus left, branch change. A session with a
  `parent_key` dirties but never fires a boundary (D10). A manual refresh is the sixth trigger.
- **Focus visits (J4, D7).** Leaving means focus moving to a window of another project after at least
  `min_focus_seconds`.
- **Timing (J5, D2).** `quiet_seconds = 120`, `min_interval_seconds = 600`. A failure waits `min_interval_seconds`;
  `bad_output` waits for new input. There is no daily cap (D9).
- **Restart and window (J6, J7).** A restart makes only threads with newer activity than their watermark dirty
  (one bootstrap entry). The window is `(watermark, as_of]`; a new thread looks back one hour. An empty window
  makes no call.
- **Inputs, sources and caps (J8, J9).** Sessions, run sections, commits, todos, focus minutes, project state and
  a bounded ledger, in at most 8192 bytes. Every entry and item cites sources.
- **Prompt and reply (J10, J11).** Haiku (D9) writes JSON; `parse_reply` takes the first `{` to the last `}`
  (Assumption 28), validates every type and cap, redacts per field (Assumption 16) and rejects the whole reply
  on any violation.
- **Merge and write (J12, J13).** Plain code merges the ledger inside the writer job, so a person's change wins.
  One transaction per attempt writes thread, entry and items.
- **Failures and logs (J14).** Error kinds are the gate's plus `bad_output`. Logs carry ids, triggers and counts only.
- **Shared model gate (J15).** The journal and the summaries share one runner and one semaphore of
  `[summaries] max_concurrent`. The journal works with summaries off.
- **Reads and routes (J16, J17, D6, D8).** `fylgja.journal/1` routes; `NavItem.journal` carries "where you are"
  and three "Do next" items inside `/items`. "Done lately" covers 7 local days.
- **Actions (J18, J19).** Pin, edit, done, drop, add, refresh, and a Fylgja todo that closes its item.
- **"How do I do this?" (J20).** A checklist of at most six steps from the item's source, with a bounded,
  contained excerpt.
- **Run reports (J21, D4).** The norn collector cuts report sections into a typed `SessionActivity.report` field;
  the core stays generic (Assumption 7, 23).
- **Events and page (J22, D5).** `journal.changed {thread}` carries an id only; the page opens no second stream.
- **Off by default (J23).** `[journal] enabled = false` builds no service. The model gate is always built.
- **This step (store and identity).** Migration 0003 adds `journal_threads`, `journal_entries` and
  `journal_items`, no foreign key to sessions, projects or todos, and no `project_path` column (Assumption 10).
  `core/journal.py` holds the pure identity. Read queries serve the window sections 1 and 4 of J8.

Departures from the pre-plan: facts 1–16 changed its mechanisms (no amendment-table row, migrations do not set
`user_version`, commit subjects come from `git` at attempt time instead of a collector channel, branches come from
`projects.branch` alone); D3 and D6–D10 settle the commit window, the `/items` field, the leaving rule, "Done
lately", the absent cap and Haiku as the only model; Assumptions 6–9, 14–16, 21, 23, 28 and 29 set the rest.

## Consequences

- Journal rows are kept forever and sit outside A12's rebuild scope: a model call cannot be repeated, so nothing
  can recompute them. The retention job never touches them.
- There is no downgrade after migration 0003; an older Fylgja refuses a newer database.
- The branch can lag a switch by up to 5 minutes, so switched work may be logged under the old branch.
- A restart can give one bootstrap entry, `quiet_seconds` after start.
- Redaction covers patterns, not names.
- Decision 0018 is unchanged: the journal is for the human, not for agents.
