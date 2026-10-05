# 0030: The `fylgja.now/1` contract: absent never null, absent sections, and the daemon wiring

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A6, A11, A19, A24
- Date: 2026-10-05

## Context

WP1.9 freezes the `/now` contract: every optional section and field later work fills must already be in
`fylgja.now/1`, and the schema must not change when they arrive. The plan asks for one serialization rule, says a
disabled or unavailable collector shows up as an *absent* section (`PLAN.md` §13), and leaves several shapes open:
how `null` and "not filled yet" differ, what "its section" means when several collectors feed one list, the name of
`next[]`'s list id, how much of the facts block the schema describes, and how the daemon keeps what the store has
no column for (facts, entrypoint, version, `via` and `confidence`).

## Decision

- **Absent, never null.** Every `/now` response (and every read endpoint that returns a slice of it) is dumped with
  `exclude_none`: a field without a value is left out, and `null` never appears. The generated schema
  (`docs/api/now.schema.json`) says the same: an optional field is not `required` and its type never includes
  `null`; a field is `required` exactly when every response carries it (no default, or a value default such as
  `false`, `0`, `[]` or the version string). Lists that are always provided are present even when empty.
- **Availability decides what is shown.** A collector is *available* when it is enabled and `starting` or `ok`.
  Rows whose source collector is not available are left out. A section disappears when no available collector has
  the capability that feeds it: `workspaces` and `focused` need `windows`; `sessions` needs `sessions`; `projects`
  needs `sessions` or `project_state`. `[]` therefore means "known and empty"; an absent key means "nobody can tell
  now" (Lidskjalv falls back to its own cache, A24). Every collector is listed in `collectors[]`; `degraded` names
  those that are `degraded` or `failed`. `/now` stays 200.
- **Vocabularies.** `sessions[].state` is one enum covering the registry (`idle`, `busy`, `waiting`), the hook state
  machine, norn (`running`, `waiting_input`, `stale`, `passed`, `failed`, `cancelled`, `paused`) and Codex and
  OpenCode; a state no entry matches is reported as `unknown`, never as an error. Session `kind` and host `kind` stay
  free strings so a plugin can add its own without a schema change. `one_liner_from` is the §4.6 rule name.
- **Shapes the plan left open.** `next[]`'s list id is the top-level `next_list_id`. The Claude facts block is typed
  (title, prompt, said, mode, branch, version, model, turns, started, last_at, prompts, tools, files, tasks), so
  `sessions[].facts.title` is a schema path the Lidskjalv mapping can name; `tasks` is Lidskjalv's `todos`.
  `children[]` holds full nested sessions (norn nesting). An optional `presence` section is reserved for the presence
  collector. `projects[]` are the projects of the live sessions; `workspaces[].projects[]` and `sessions[]` hold
  paths and keys. `lane_key` equals `process_key` (A11).
- **What `/now` keeps in memory.** The daemon puts a tap between the ingest gate and the writer: every accepted
  record goes to the resolver (ancestor chains) and to a session memory (the redacted facts block, entrypoint and
  version). `via`, `confidence` and the cmux ids come from the latest resolution, and only while it matches the
  stored join. The one-liner and its rule are recomputed from the stored activity and title with the §4.6 rules.
  All of these return with the next emission or refresh after a restart.
- **Composition order.** The daemon starts sinks before sources (writer, live view, state engine, supervisor) and
  stops in reverse, so nothing a collector emits is lost. `[api] host` exists and validation refuses anything but
  `127.0.0.1`; `/collectors` returns the `collectors list` rows (WP1.2), while `/now` `collectors[]` is the
  health view.

## Consequences

- A client reads optional values with a default (`.x // ""` in jq); there is no `null` to tell apart from absence.
- Later steps fill existing fields only. Anything else bumps `NOW_SCHEMA`, regenerates the schema with
  `scripts/gen_now_schema.py` and records a decision; `tests/unit/test_now_schema_guard.py` enforces it.
- A collector that degrades hides its last data until it recovers, even when that data was still right; its state
  explains the gap in `collectors[]`.
- The supervisor now marks a collector whose task was cancelled before it ever ran as `disabled` (stopped) at
  shutdown, so the stored health of a quick start and stop is not left at `starting`.
