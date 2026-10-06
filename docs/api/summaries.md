# Summaries: `/summaries/{scope}/{ref}`, `story` and `fylgja story` (WP8.1)

Session and workspace summaries written by a local model, in the daemon only. **Off by default**:
`[summaries] enabled = false` in `config.toml`. Turn it on with

```toml
[summaries]
enabled = true
ttl_seconds = 300     # a changed summary is rewritten at most this often per scope
max_concurrent = 3    # model calls at once
```

and restart the daemon. Each model call is `claude -p --model haiku --strict-mcp-config` with the prompt on stdin, run
in `~/.local/state/fylgja/summarizer/` with `FYLGJA_SUMMARY=1` and `LIDSKJALV_TRACK_SUMMARY=1`, and a 60 s timeout. The
summarizer session never shows up as a session: the hooks exit at once and its cwd is always ignored. Decision 0046
holds the details.

| Scope | `ref` | Summary | Made from |
|---|---|---|---|
| `session` | the session key (`claude:<id>`) | `arc` (the whole session, one sentence) and `now` (what it does now, two sentences), as Lidskjalv's `_story_one` | the facts block and the one-liner, as `/now` shows them |
| `workspace` | the workspace name | `text`: one short paragraph | the label, one-liner, projects, sessions (state and one-liner), top context items and open todos |
| `day` | `YYYY-MM-DD` | `text` | WP8.2 |

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
  ref `/now` does not show.
- `generated_at` is the instant the summary describes: when its job took its inputs.
- `error_kind`: `timeout`, `not_installed`, `spawn_failed`, `nonzero_exit`, `empty_output`, `cwd_unavailable`,
  `runner_error`.

A finished job publishes `summary.ready` `{"scope", "ref", "state"}` on `GET /events`, followed by `now.changed`.

## `/now`

`sessions[].story = {arc, now, generated_at, state}` and `workspaces[].story = {text, generated_at, state}`, present only
while summaries are enabled and a summary with text is cached. `fylgja.now/1` is unchanged: the fields were there from
the start.

## `fylgja story`

```text
fylgja story session <key|native id|prefix> [--wait 60] [--json]
fylgja story ws <name> [--wait 60] [--json]
```

Opens `/events`, posts the request, waits for `summary.ready` (up to `--wait` seconds), then fetches and prints the
summary with its state. A cached fresh summary prints at once. Exit codes: 0 a summary; 1 summaries disabled (409), the
summary failed or did not arrive in time; 3 the daemon is not running.
