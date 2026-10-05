# 0026: Store write semantics: intervals, snapshots, merges, batching and retention

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A5, A12, A20
- Date: 2026-10-05

## Context

WP1.4 builds the single writer, the ingest gate and the §4.1 schema. A20 says windows and session
snapshots are change intervals and A12 scopes rebuildability to the 30-day raw-event window, but
neither says what an interval write does with an unchanged, late or partial observation, whether
snapshots are also appended to `events`, or how "up to 500 records or 200 ms" behaves for a single
record. §4.1 sets no retention for `windows_snapshot`, and two of its column names (`group`, `end`)
are SQL keywords. Every later step depends on these answers.

## Decision

- **Intervals.** A record about an interval entity (window, session activity, usage) carries its
  full current state. An unchanged state writes nothing. A changed state closes the open interval
  at the observation time and opens a new one. An observation older than the open interval, or
  older than the end of the last closed one, is stale and ignored, so intervals never overlap.
- **Complete snapshots** close the open entities of that kind *reported by the same collector*
  that they do not list, as of their `observed_at` (sessions get `ended_at`, workspaces
  `closed_at`).
- **Entity rows merge.** `sessions`, `projects` and `workspaces` keep a stored value when a record
  leaves the field `None`, because the resolver, state machine and labels also fill those rows.
- **What reaches `events`.** Every Event, deduplicated on `dedup_key`; a duplicate changes nothing
  else, which makes replays idempotent. Snapshots of kinds with a projection are stored only as
  that projection (A20's purpose is to avoid per-tick copies); snapshots of kinds without one yet
  (todo, context item, presence, focus) are appended to `events` so a later projection can replay
  them.
- **Batching.** A transaction takes what is queued, at most 500 items, and stops collecting after
  200 ms; it never waits for more items, so a lone record commits at once.
- **Retention.** The cut-off is strict: a row exactly 30 days (events) or 365 days (archives) old
  is kept. `windows_snapshot` closed intervals share the one-year archive clock of spans and
  session and usage snapshots. Open spans and intervals are never deleted.
- **Names.** §4.1 `projects.group` is `group_name`; `focus_spans` start/end are
  `started_at`/`ended_at`.
- **Tooling.** mypy's cache is configured with `sqlite_cache = false`, so the only database files
  in the tree are the test databases under `target/tests/`.

## Consequences

- Under A12, state derived from events (spans, hook-driven session state) is rebuildable within
  30 days. Snapshot-derived state is not rebuilt from `events`; its interval rows are its record.
- Collectors must emit the whole state of an interval entity in each record, not a delta.
- Later steps that need a new projection (todos in step 35, presence, context items) add it to the
  writer's projection table; their raw snapshots are already retained for 30 days.
