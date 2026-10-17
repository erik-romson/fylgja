# 0056: Runs on cards and in Next, and Next items that say what to do

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A10, A15, 0037, 0047, 0055
- Date: 2026-10-17

## Context

A norn run has no window of its own, so the resolver places it on no workspace. The Overview showed its agent
session on a workspace card but not the run, so a running norn pipeline and its stage were visible only in the
Live data table. A run that finished never reached `waiting[]`: only `waiting_input` did. A Next item showed only
its kind and a generic reason ("finished a turn nobody has looked at"), so six unseen turns looked the same and
named neither the session, the folder nor how long they had waited.

## Decision

- **`run_finished` waits** (`core/waiting.py`). A session row that is not ended and whose state is `passed`,
  `failed` or `cancelled` is a `run_finished` entry, rank 1 like `turn_done`, `since` its `state_since`, reason
  `<name> <state>`. It leaves when the run's project gains focus at or after `since` (the `touched` time that
  waiting-on-me already receives), or when the run leaves the live view after `[collectors.norn]
  finished_seconds`. It scores 90 in `rank_next`, counts as "turn done" in `frecency`, and is a valid
  `[notify] kinds` value with the verb "finished a run". `fylgja.now/1` is unchanged: `waiting[].kind` is a string.
- **A run sits above its session on the card** (`api/views.py`). When a card lists a session whose parent (a
  `children[]` link in `/now`) is not on the card, the parent is listed right above it and the session gets
  `child: true`. The family sorts as waiting when either waits. A top-level session on no workspace that drives
  none on one (a run between agent sessions, or after its last one ended) goes on the card of its project's
  workspace. A run line counts against `MAX_SESSIONS`.
- **A click on a windowless session focuses what it drives.** A card line or a Next item for a session without
  `host.window_id` focuses the first session below it that has one, else the window of its card (a card line)
  or of its project's workspace (a Next item). Next items now match child sessions too, not only top-level
  ones.
- **Next items say what, where, since when and what to do.** `NextEntry` gains `title`, `folder` (cut to its last
  40 characters), `since` (from the matching `waiting[]` entry, a todo's `created_at`) and `action` (one phrase
  per kind, plus the waiting reason for `permission` and `input`). The page shows them; `reason` moves to the
  hover text. The daemon computes all of it, so the page still only renders.

## Consequences

- A finished run is offered for at most `finished_seconds` (900 s by default). A person away for longer misses it
  in Next; raising `finished_seconds` keeps finished runs listed, and in Next, for longer.
- Focusing any window of the run's project counts as having seen the result, even a window of another session.
- `fylgja.view.overview/1` gains optional fields only; older pages ignore them.
