# 0064: The journal shows the recap a session wrote

- Status: accepted
- Amendment: none
- Amends: 0057
- Answers: —
- Relates to: 0057, 0063
- Date: 2026-10-21

## Context

Claude Code writes a recap when the person comes back to a session after being away: one or two sentences on what the
session is doing and what comes next. It shows it in the terminal and appends it to the transcript as a `system` line
with `subtype` `away_summary`, its `timestamp`, `gitBranch` and `content` ending in "(disable recaps in /config)". The
agent writes it with the whole conversation in view, so it is often closer to the point than an entry the journal model
writes from the activity window. Fylgja tailed the line already but ignored it.

## Decision

- **Collector.** `FactsAccumulator.recap()` keeps the newest `away_summary` that is not a sidechain and has text and a
  time: the text without the hint, whitespace collapsed, at most 600 characters, its time and its branch. It stays out
  of the facts block, which keeps parity with Lidskjalv. `SessionActivity.recap` (`SessionRecap`: `text`, `at`,
  `branch`) carries it, and the ingest gate redacts the text and cuts it to 600 characters.
- **Store.** A recap is an event, not session state, so it does not open an activity interval. Migration 0007 adds
  `session_recaps`, keyed by the session and the recap's time; the writer inserts a recap it has not stored yet.
- **Live view.** The `recaps` collection holds the newest recap of each live session. The writer publishes a recap it
  stored and removes it when the session ends, so a new recap also sends `now.changed`.
- **Journal.** The journal service caches the newest recap per checkout and branch: read from the store at start,
  then taken from the live view, so a recap outlives its session. A thread's recap is the newest of a session whose
  `project_path` is the thread's checkout, written on the thread's branch or on no known branch.
  `JournalThread.recap` and the Navigator's `ItemJournal.recap` (`text`, `at`, `session`) both come from that cache,
  so `/items` still does no I/O for the journal. `JournalThread.recap` is there also for a thread with no row yet;
  `ItemJournal` exists only for a thread with a row. Both are absent while there is none.
- **Not in the journal's input.** The journal model does not see the recap; "where you are" and the items stay its own.

## Consequences

- Raycast can show what the agent itself last said about the work, in the journal next to "Do next" and in the
  Navigator, without a model call.
- A recap only exists when the person was away, so it can be older than the newest entry; the answer carries `at` so a
  client can show its age.
- The `away_summary` line is not a documented format. A line of another shape is ignored, and no recap is shown.
