# 0046: Summary service: details

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A8, A12, A24
- Date: 2026-10-06

## Context

WP8.1 adds the daemon-only summary service: a job queue keyed by `(scope_kind, scope_ref)`, a `claude -p --model haiku
--strict-mcp-config` runner in the summarizer cwd, a digest-and-TTL cache written through the single writer,
`GET`/`POST /summaries/{scope}/{ref}`, `summary.ready` on SSE, the cached `story` in `/now` and `fylgja story`. The plan
fixes the states, the status codes for "fresh" (200), "otherwise" (202) and "disabled" (409), the TTL, the newer-than-
transcript gate and the concurrency cap. It leaves open what a `POST` answers inside the TTL, what the inputs and the
"transcript time" are exactly, what a failed attempt stores, how the prompt reaches `claude`, where `claude` is found,
and what `GET` answers for a scope that is no longer shown. `docs/revalidation/2026-10-06-summaries.md` holds the flags
and the `_story_one` reference.

## Decision

- **Inputs come from the `/now` picture.** A session's inputs are its agent, project, title, branch, state, §4.6
  one-liner and norn stage, plus what `_story_one` shows from the facts block (turns, top six tools, edited file names,
  open tasks, last prompt and reply, the prompts so far) and `facts.last_at`. A workspace's are its name, effective
  label, one-liner, projects, each session's agent, state, one-liner and title, the top five context items of its
  windows (focused window first; a window without items gives its app and title) and its first eight open todos. They
  are normalized (empty values dropped, keys sorted) and hashed with SHA-256 into `input_digest`. Because they come
  from `/now`, they are redacted already and the digest moves exactly when the summarized picture does.
- **The transcript time is `facts.last_at`.** The "story newer than its transcript" gate compares the cached
  summary's `generated_at` with the transcript's last line (`facts.last_at`), not file times. A session without a facts
  block (Codex, OpenCode, norn) has no such time and follows the digest and the TTL alone. A story that passes the gate
  is answered as `fresh`, also when its digest differs (the state or one-liner moved, the transcript did not): it is
  never regenerated.
- **`generated_at` is the instant the summary describes**: when its job took its inputs (the request that started it),
  not when the model answered. A transcript line written while the model runs is then newer than the story, and the
  gate does not hide it.
- **The TTL counts from the last attempt**, failed or not (attempts are remembered in memory, the stored
  `generated_at` covers a restart). Inside it a changed digest is not regenerated.
- **`POST` answers 200 whenever it serves the cache without starting a job**: `fresh`, and also `stale` or `failed`
  while the TTL holds the next attempt back. 202 means a job is queued or running for the key (started now or joined);
  409 `{scope, ref, state: "disabled"}` while summaries are off; 404 for an unknown scope, a ref `/now` does not show,
  and for `day` until WP8.2 supplies its inputs. The body of the `POST` is ignored.
- **`GET` never starts a job.** It answers 404 only when nothing is cached, nothing is pending and `/now` does not show
  the ref. A cached summary of a scope that is no longer shown (an ended session) is answered as `stale`: without
  current inputs it cannot be called fresh. While summaries are off `GET` answers `disabled` and `/now` carries no
  `story`, even if rows from an earlier enabled period are stored.
- **`/now` shows a `story` only with text.** A pending job over an existing summary shows `state: pending` with the old
  text; a first attempt that failed has no text and shows no `story` (the `GET` still says `failed`).
- **A failure keeps the previous text.** The stored rows keep the previous `text`, `input_digest`, `model` and
  `generated_at`, and get the `error_kind`: `timeout` (60 s on the daemon clock, or the runner's own), `not_installed`,
  `spawn_failed`, `nonzero_exit`, `empty_output` (an empty reply, or labels with nothing after them),
  `cwd_unavailable` and `runner_error`. A success clears it. Model output is redacted with `[privacy] redact_patterns`
  and cut to 600 characters; it is never logged, and neither is the prompt.
- **The prompt goes to `claude` on stdin**, never on the command line, so session facts do not show in `ps -o args=`.
  The environment is the daemon's own, without the markers of a parent Claude session (`CLAUDECODE`,
  `CLAUDE_CODE_ENTRYPOINT`), plus `FYLGJA_SUMMARY=1` and `LIDSKJALV_TRACK_SUMMARY=1`. The call runs in its own process
  group, which is killed on timeout or when the daemon stops.
- **`claude` is `~/.local/bin/claude` when that is executable, else the one on `PATH`.** No `[paths] claude` key is
  added; launchd's minimal `PATH` is covered by the native installer's link. `--no-session-persistence` is not added:
  the summarizer cwd is already dropped by the registry poller, the inbox drainer and the history reader, and WP8.1
  fixes the flag set.
- **`fylgja story` opens `/events` before it posts**, so a `summary.ready` that arrives at once is not missed. It waits
  up to `--wait` seconds (default 60), then fetches the summary. It exits 0 with a summary, 1 when summaries are
  disabled, the summary failed or it did not arrive in time, and 3 when the daemon is down.
- **Summaries are derived rows outside retention.** They are kept until replaced; A12's rebuild scope does not cover
  them (a model call is not repeatable).

## Consequences

- A busy session is summarized at most once per TTL, an idle one never again, and polling `/now` or `GET` costs no model
  call. Disabled (the default) means no job, no `claude` process and no summarizer directory.
- WP8.2 adds the `day` scope by supplying its inputs and prompt; the queue, cache, API and CLI need no change.
- WP8.3 reads `sessions[].story.now` where Lidskjalv reads `story`, and `POST`s with the bearer when the story is missing
  or stale; a 409 tells it to fall back to `_story_one`.
- Joined requests use the inputs of the request that started the job; a request with newer inputs that joins it sees
  `stale` afterwards and is regenerated after the TTL.
