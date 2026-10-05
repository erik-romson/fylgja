# 0032: Hook events as a record kind, and how the inbox is drained

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A4, A5, A9
- Date: 2026-10-05

## Context

WP2.2 turns the envelopes of `fylgja hook` into "minimized core events" and reads the statusline snapshots, but the
core models had no record that carries a hook event, and none that carries a statusline reading. WP2.3 (the state
machine) and the later `context_pct`, `cost_usd` and `usage.providers[]` fields need both, so they have to be
stored now. The existing kinds do not fit: a `session_activity` record holds the *full current state* of an
interval entity, so an event with only a tool name would overwrite the registry's state with nulls.

## Decision

- **A new entity kind, `hook_event`**, with `HookEvent` and `HookEventSnapshot` and the payload `HookEventPayload`.
  It belongs to the `sessions` capability. Hook events are appended to `events` (like `focus`) and have no
  projection yet; WP2.3 adds one. The payload holds the A5 allowlist only: session id, event, tool name,
  notification type, reason, cwd, permission mode, edited paths, the prompt (cut to 300 characters by the
  normalizer and redacted by the privacy filter), Claude's pid, the allowlisted terminal ids, the envelope id and,
  for a statusline reading, `context_pct`, `cost_usd` and the pull request. `entity_id` is the session key
  `claude:<session id>`.
- **The dedup key derives from the envelope id** (with the source, entity id and the envelope's timestamp), so the
  replay of a spool file after a crash between commit and unlink stores nothing twice. A statusline reading uses
  the snapshot's envelope id too.
- **Delete after commit, in batches.** The drainer reads a batch of up to 256 files (in parallel threads, because
  opening freshly written files costs up to a millisecond on a busy machine), submits the events in name order
  with `emit_committed` concurrently so the writer groups them into few transactions, and unlinks each file only
  after its own commit has resolved. A file whose commit failed stays and is tried again at the next pass.
- **Quarantine.** A file moves to `quarantine/` when it is not a JSON object of schema `fylgja.hook/1`, names no
  session or event, belongs to another source, is larger than 32 MiB, is not a regular file, or when the ingest gate
  rejects its event (for example a timestamp older than 30 days). The nightly maintenance job deletes quarantined
  files older than 7 days (strictly older) and reports the count.
- **The watcher.** `select.kqueue` with a vnode watch on the inbox directory on macOS; a 1 s poll everywhere else,
  and as a safety net while the watch is active. The watch is opened before each pass, so an event between the
  listing and the wait is not lost. If the directory is replaced the watch is reopened; if kqueue is unavailable
  the drainer polls.
- **Statusline readings** become one `hook_event` named `StatusLine` and one incomplete `usage` snapshot with a
  row per rate-limit window (`five_hour`, `seven_day`, …; provider `claude`, source `statusline`). Both are
  stamped with the file's mtime. The `claude` plugin therefore also declares the `project_state` capability, which
  is where the `usage` kind lives; without it the ingest gate rejects the rows. The reader skips snapshots older
  than 7 days and never writes, deletes or quarantines.
- **Hook-time cmux ids** reach the resolver through `Resolver.observe`: a `hook_event` with `CMUX_WORKSPACE_ID` or
  `CMUX_SURFACE_ID` calls `observe_cmux_ids` (the `cmux-hook-env` join of decision 0025) and asks the daemon for a
  refresh when the ids are new.

## Consequences

- Adding an entity kind touched `models.py`, the capability map and the privacy filter's text-field table, as
  `models.py` documents; the model tests cover it. `docs/api/now.schema.json` is unchanged.
- A hook event of a session that no registry row has yet is stored but not shown until WP2.3 projects it.
- The statusline directory is not cleaned up by the maintenance job (only the quarantine is). A snapshot of an
  ended session stays on disk; it is small, and the reader skips it after 7 days.
- `claude` carries a broader capability than strictly needed (`project_state` also allows `project` and `build`
  records). A narrower `usage` capability would be a change to the collector API, which is out of scope here.
