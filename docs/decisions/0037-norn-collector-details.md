# 0037: norn collector: the inbox as run state, the session key, nesting and the stale rule

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A10, A11
- Date: 2026-10-06

## Context

A10 says norn pushes its run events into `hook-inbox/norn/` and Fylgja scans norn's files as the fallback, and WP3.3(b)
says the inbox machinery is shared with Claude. The revalidation (`docs/revalidation/2026-10-06-norn.md`) found things the
plan does not say:

- The Claude drain turns every envelope into a minimized `hook_event` (A5: session id, event name, tool name, ...). A run's
  state (pipeline, stage, index, `WaitingInput` kind, pid, session ids) is none of that, and a `hook_event` named
  `WaitingInput` would also be fed to the session state engine (decision 0033), which makes a session of its own for every
  hook entity.
- A history counter is per state key, so two projects can both have `plan_with_review` run 1. A key with a slash does not
  fit `/sessions/{key}`.
- `run.json` has no `history_run_id` before some norn version (203 of 519 files), `argv` is the path of
  `norn/__main__.py` while the process is `python3 -m norn ...`, and the journal never records `RunStarted`.
- The Claude session a run drives is already listed by the Claude collector; a second `session` record for it from norn
  would change the row's `source`, and the registry's next listing would change it back.
- A finished run's stage line ("`<pipeline>` `<terminal status>`") needs a state word, and the `/now` vocabulary says
  `passed`, not `done`.

## Decision

- **The inbox carries run state, not hook events.** `InboxDrainer` gets a `consumer` mode: it validates the envelope and
  the inbox source, hands it to the consumer, and removes the file once the consumer returned (the consumer raising
  `EnvelopeError` quarantines the file). It is still the one implementation of watching, ordering, tmp sweeping and
  quarantine. The norn collector's consumer reduces the envelope into the run's in-memory state, and the collector emits
  *complete snapshots* of the listed runs (a run no longer listed is ended in the store). Nothing is committed as a
  `hook_event`, so the state engine never sees norn. `read_in_thread=False` reads the few small envelopes on the event
  loop, so the state changes at deterministic points of the loop (the contract test compares two runs).
  *Consequence:* the file is deleted when the event is applied in memory, and the snapshot follows within one wake-up.
  A daemon that dies between the two loses that event; every envelope carries the run's identity, so the next heartbeat
  (30 s) rebuilds the run, and the file scan backfills what the files know.
- **The envelope payload** (`docs/collectors/norn.md`) names the event and repeats the run's identity on every event
  (`pipeline`, `state_key`, `history_run_id`, `run_id`, `pid`, `cwd`), plus `argv`, `provider`, `stage_total`, `stage`,
  `stage_index`, `status`, `success`, `session_id` and `kind` where they apply. No prompt, output or excerpt travels.
- **The key is `norn:<stem>@<h>:<run id>`.** `<stem>` is the state key's file stem, `<h>` six hex digits of the SHA-1 of
  its directory, `<run id>` the history run id (or `u` and eight characters of the run's uuid when norn kept no history).
  The plan's `norn:<state key>:<run id>` would collide across projects and put slashes in the key.
- **States:** `running`, `waiting_input`, `paused`, `stale`, `passed`, `failed`, `cancelled` (the `/now` vocabulary). A
  plugin run is `running` while its newest event is at most 90 s old and its pid is alive, `stale` otherwise and at once
  when the pid is gone; a file-only run is `running` only while the process it was matched to is alive. A finished run
  stays listed 900 s, a stale one 6 h (`finished_seconds`, `max_age_seconds`). `passed` and `stale` end rule 2's stage
  line ("`<pipeline>` `passed`", "`<pipeline>` `stale`"): a stale run's last stage says nothing about what it is doing.
- **The process match** compares a run's `argv` with the process line in order (the first compared by file name, and a
  `__main__.py` standing for `-m <module>`), and the working directory when both are known. It runs only for unfinished
  runs the plugin never reported, once per scan, and a failing `ps` keeps the last match and degrades the collector.
  The newest unfinished run takes a process first.
- **Nesting.** A live Claude run (provider `claude-code` or none named) emits one `session` *event* per agent session id
  it drove, with `parent_key` set to the run. The store's upsert no longer lets an event (a record that does not *list*
  the session) change `source`: the row stays the Claude collector's, and only the link is added; an event for a session
  nobody listed yet creates it with source `norn`, and the registry's listing takes it over. `/now` moves a session whose
  `parent_key` names a listed session into that session's `children[]` and fills `pipeline` for a session with a stage; a
  session whose parent is not listed stays a top-level session. The `entrypoint` of the registry entry is not checked: the
  ids are the ones norn drove.
- **The scan** walks `roots` (default `~/projects`, depth 3, hidden and heavy directories skipped, a cap of 50 000
  entries) and the directories of runs the plugin reported (depth 0), reads only files modified within `max_age_seconds`,
  and reads the last 512 KiB of a `.history` file (growing to 8 MiB for one huge line), caching by `(mtime, size)`.

## Consequences

- WP3.3(a) (norn repo) writes the payload of `docs/collectors/norn.md` and subscribes where `RunStarted` is visible: the
  journal does not record it. A heartbeat every 30 s with the full identity is what makes `running` exact.
- The store, the live view and the API gain no norn-specific code: the collector name is nowhere in them. The `source`
  rule and the nesting apply to any collector that links sessions.
- A run started without a state key has no history and no files; it exists only through the plugin.
- `stale` is shown for a crashed run for up to 6 h after its last sign of life; the person can see which run died.
- Not verified live: a process match with a real unfinished run, two TUIs in one directory, and the plugin's envelopes
  (norn side). `docs/revalidation/2026-10-06-norn.md` lists the commands to run by hand.
