# "What next": `/next`, `next[]` and feedback (WP6.2)

The ranking scores every candidate in the `PLAN.md` §7 order and explains each in words. The list is
served by `GET /next`, `fylgja next` and the `next[]` and `next_list_id` fields of `/now`
(`fylgja.now/1` is unchanged: the fields were there from the start). `next[]` is present, possibly
empty, exactly while `waiting[]` is.

| Rule | `kind` | Score | Source |
|---|---|---|---|
| 1. an agent waits for permission or input | `permission`, `input` | 100 | `waiting[]` |
| 2. an agent turn finished and not seen, or a run finished and its project not focused since | `turn_done`, `run_finished` | 90 | `waiting[]` |
| 3. a todo marked `next` in the focused workspace | `todo` | 80 | todos (a project or session todo counts for the workspace its session sits in; no due date exists yet, so only the `next` mark counts) |
| 4. failed CI or a review requested, project touched today | `build_failed`, `review_requested` | 70 | `waiting[]` |
| 5. the way back: most focus today, an open todo, no session activity for an hour, not in focus | `context_switch` | 60 | focus spans of today, todos |
| 6. unread Teams or Slack, titles only (A15) | `unread` | 50 | `waiting[]` |

* Ties of score are broken by age: the oldest `since` first (a todo's `created_at`), no age last,
  then kind and target. Output is deterministic for a live view and a clock.
* `target_ref` is a session key, a project path, a window id, a todo id or a workspace name.
* A `reason` never holds a time that changes from call to call, so an unchanged situation is an
  unchanged list. At most 10 items.

## `GET /next`

`{"list_id": "nl-…", "items": [{suggestion_id, kind, target_ref, score, reason, rank}]}`, best first,
`rank` 1 being the top. No authentication. `suggestion_id` is `<list_id>.<rank>`.

Every list that is generated is written to `suggestions_log` (`id` = `suggestion_id`, `list_id`,
`generated_at`, `rank`, `kind`, `target_ref`, `score`, `reason`) through the writer. A list is
generated when the ranking differs from the one before it, so polling `/now` does not fill the log;
the ids come from the time of generation and the content, so they repeat on a fake clock. An empty
list has an id but no rows, so it cannot be rated.

## `POST /next/feedback`

Body `{"list_id", "rating": "good"|"bad", "suggestion_id"?}`; bearer, Origin, JSON and body limit as
for every mutation. The rating goes on the list's top item, or on `suggestion_id`, with
`feedback_at`; a second rating replaces the first. Answers `200 {list_id, suggestion_id, rank,
rating, feedback_at}`. An unknown `list_id` (or a `suggestion_id` that is not in that list) is
`404`; a body that is not exactly these fields is `422`.

## CLI

* `fylgja next [--json]` prints the list and remembers its `list_id` for today in
  `<state dir>/next-last.json` (`{"date", "list_id"}`).
* `fylgja next --feedback good|bad` posts the rating for the most recent list the CLI showed today.
  It exits 1 with "run `fylgja next` first" when there is none (or it is from another day). The
  daemon writes the rating; the CLI never touches SQLite (A7).

Logic: `src/fylgja/core/ranking.py` (pure), `src/fylgja/core/next_lists.py` (ids, log, feedback),
`src/fylgja/api/routes/next.py`.
