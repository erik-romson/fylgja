# 0063: The journal sees a session go on after waiting for you

- Status: accepted
- Amendment: none
- Amends: 0057
- Answers: —
- Relates to: 0056, 0057
- Date: 2026-10-20

## Context

A norn run that stopped for the person's answer got a "Blocked" item ("the run waits for your input"). Once the person
answered, the run went back to `running`, and the session's status stopped saying it waited. The journal still showed
the item and the old "where you are", for two reasons:

- Leaving `waiting_input` or `waiting_permission` was input (J2), not a boundary (J3). A running run gives no Stop and no
  quiet, so the journal waited for the run to finish.
- An entry that a Claude session's end triggered later kept the item. The prompt let the model close an item only when
  a source showed it "done", and a blocker is resolved, not done. The session line also showed only the newest state,
  not that the session had waited.

## Decision

- **The daemon closes the blocker.** When a session, a child too, changes from `waiting`, `waiting_permission` or
  `waiting_input` to a state that goes on (not a waiting state, not `ended`, not none), every open `blocked` item whose
  source is that session and whose `created_at` is at or after the session's `state_since` of the wait is set `done`,
  with `closed_by = fylgja`, `closed_at` now and no `closed_entry`. An item the person pinned or edited stays open, as
  in the merge (J12). An item added before the wait began is about something else and stays open.
- **A soft boundary `resumed`.** The same change, for a session that is not a child, records the boundary `resumed`
  with the session as its source. It ranks below the hard boundaries and above `turn_done`. It is due once
  `min_interval_seconds` passed since the last attempt; it waits for no quiet, because the session runs on.
- **The prompt.** The close rule reads "done or no longer in the way", in the rules and in the reply form. A session that
  waited at the window's start or in it, and whose newest state goes on, gets the line "It waited for you, then went
  on." The input digest covers that fact.
- **Store.** Migration 0006 rebuilds `journal_entries` and `journal_items` so that `trigger` takes `resumed` and
  `closed_by` takes `fylgja`.

## Consequences

- A blocker about the wait leaves the journal the moment the run goes on, without a model call.
- A blocker that cites the session but is not about the wait, and was added during the wait, is closed too. The next
  entry can add it again.
- A run that goes on gets one entry soon after, so "where you are" follows it.
- `JournalItem.closed_by` and `JournalEntry.trigger` each have one more value; clients that list the known values
  should add them.
