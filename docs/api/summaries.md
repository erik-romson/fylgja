# Summaries: `/summaries/{scope}/{ref}`, `story`, `fylgja story` and `fylgja history --story` (WP8.1, WP8.2)

Session, workspace, day and folder summaries written by a local model, in the daemon only. **Off by default**:
`[summaries] enabled = false` in `config.toml`. Turn it on with

```toml
[summaries]
enabled = true
ttl_seconds = 300     # a changed summary is rewritten at most this often per scope
max_concurrent = 3    # model calls at once
```

and restart the daemon. Each model call is `claude -p --model haiku --strict-mcp-config --tools "" --no-session-persistence` (no tools, no
stored transcript) with the prompt on stdin, run
in `~/.local/state/fylgja/summarizer/` with `FYLGJA_SUMMARY=1` and `LIDSKJALV_TRACK_SUMMARY=1`, and a 60 s timeout. The
summarizer session never shows up as a session: the hooks exit at once and its cwd is always ignored. Decision 0046
holds the details; decision 0053 adds the folder scope.

| Scope | `ref` | Summary | Made from |
|---|---|---|---|
| `session` | the session key (`claude:<id>`) | `arc` (the whole session, one sentence) and `now` (what it does now, two sentences), as Lidskjalv's `_story_one` | the facts block and the one-liner, as `/now` shows them |
| `workspace` | the workspace name | `text`: one short paragraph | the label, one-liner, projects, sessions (state and one-liner), top context items and open todos |
| `day` | `YYYY-MM-DD` (local) | `text`: the story of the day | the day's `/history` totals and ordered spans, the sessions active that day with their one-liners, the todos created or finished that day |
| `folder` | a navigation item `id` (16 hex digits, `GET /items`) | `text`: what was being done there and where it stands, at most three sentences | the item's name, tickets, branch, PR state, `dirty`, window places, sessions (one-liner and cached ARC) and open todos; no times |

## Responses

`GET` and `POST /summaries/{scope}/{ref}` answer `{scope, ref, state, arc?, now?, text?, generated_at?, input_digest?,
error_kind?}`; absent fields are left out, never `null`.

| `state` | Meaning |
|---|---|
| `fresh` | cached, and its inputs are the current ones (or, for a session, it is as new as its transcript) |
| `stale` | cached, the inputs changed since; rewritten on the next `POST` after the TTL |
| `pending` | a job is queued or running (the previous text, if any, comes with it) |
| `missing` | nothing cached and no job |
| `failed` | the latest attempt failed (`error_kind`); the previous text, if any, comes with it |
| `disabled` | `[summaries] enabled = false` |

- `GET` is a read (no authentication). It never starts a job. 404 when nothing is known about the ref.
- `POST` is a mutation (bearer, JSON content type; the body is ignored). **200** with the cached summary when it is
  fresh, or stale or failed inside the TTL; **202** `pending` when a job was queued or is running (concurrent requests
  share one job); **409** `{scope, ref, state: "disabled"}` while summaries are off; **404** for an unknown scope or a
  ref `/now` does not show (a day: one with nothing recorded, or one that has not begun; a folder: an id no current
  item has).
- `generated_at` is the instant the summary describes: when its job took its inputs.
- `error_kind`: `timeout`, `not_installed`, `spawn_failed`, `nonzero_exit`, `empty_output`, `cwd_unavailable`,
  `runner_error`.

A finished job publishes `summary.ready` `{"scope", "ref", "state"}` on `GET /events`, followed by `now.changed`.

## `/now` and `/items`

`sessions[].story = {arc, now, generated_at, state}` and `workspaces[].story = {text, generated_at, state}`, present only
while summaries are enabled and a summary with text is cached. `fylgja.now/1` is unchanged: the fields were there from
the start. `GET /items` gives `items[].story = {text, generated_at, state}` by the same rule (the folder scope, below).

## `fylgja story`

```text
fylgja story session <key|native id|prefix> [--wait 60] [--json]
fylgja story ws <name> [--wait 60] [--json]
```

Opens `/events`, posts the request, waits for `summary.ready` (up to `--wait` seconds), then fetches and prints the
summary with its state. A cached fresh summary prints at once. Exit codes: 0 a summary; 1 summaries disabled (409), the
summary failed or did not arrive in time; 3 the daemon is not running.

## The day scope and `fylgja history --story` (WP8.2)

`ref` is a local date `YYYY-MM-DD`; anything else is **422** (`{"detail": "a day is written YYYY-MM-DD"}`), and a day
that has not begun or has nothing recorded (no focus span, no active session, no todo created or finished) is **404**
`nothing was recorded on that day`. The inputs are read from the store, as stored (redacted): the `/history` totals per
workspace, project and app, the ordered spans (at most 150, local times, then a count of the rest), the sessions active
that day (first seen or changed in it) with the one-liner they ended the day with (at most 20) and the todos created
or finished that day (at most 20 each). The digest is the SHA-256 of those normalized inputs.

| Day | Rule |
|---|---|
| a **past** day | `fresh` while the digest is unchanged, however long ago: no TTL, no model call. A changed digest (a late import) is regenerated by the next `POST` at once. Only a *failed* attempt waits out the TTL. |
| **today** | the TTL rules of the other scopes: a changed digest is answered `stale` until `ttl_seconds` after the last attempt. |

```text
fylgja history --story [--date YYYY-MM-DD] [--wait 60] [--json]
```

Posts `POST /summaries/day/<date>` (today unless `--date`), waits for `summary.ready` (up to `--wait` seconds, default
60) and prints the paragraph with its state. A cached fresh story prints at once, without a model call. Exit codes as
`fylgja story`: 0 a story; 1 summaries disabled (with a message), nothing recorded that day, the attempt failed or it
did not arrive in time; 2 a date that is not `YYYY-MM-DD`, or `--date` without `--story`; 3 the daemon is not running.

## The folder scope

`ref` is the `id` of a navigation item of `GET /items` (decision 0051): 16 hex digits, the start of `sha256(key)`, so
no path is in the URL. Folders and apps both have one. The item is looked up in the current items (the default 14
days); an id no current item has is **404** `{"detail": "no item has that id"}`.

```text
POST /summaries/folder/208d0396f4f10d7f   →  202 {"scope": "folder", "ref": "208d0396f4f10d7f", "state": "pending"}
GET  /summaries/folder/208d0396f4f10d7f   →  200 {"scope": "folder", "ref": "…", "state": "fresh", "text": "…",
                                                  "generated_at": "2026-10-05T12:00:00Z", "input_digest": "…"}
```

The inputs are the item's kind, name, group and tickets; its project's branch, PR number and state and `dirty`; the
label and title of each window place; the agent, title, one-liner and cached ARC of each session place; and the open
todos of its project and sessions (at most eight). They hold **no seconds and no times**, and places and sessions are
sorted by content, so time passing or focus moving never makes a folder summary stale; a changed title, branch, PR,
todo or session story does. The TTL, the digest cache and the guard environment are those of the other scopes.

Nothing but the `POST` generates a folder summary. `GET /items` carries `items[].story` only while summaries are
enabled and one with text is cached, judged against the item's current inputs, and its `q` also matches the story text.
`fylgja story` has no folder subcommand.
