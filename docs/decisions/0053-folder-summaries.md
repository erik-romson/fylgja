# 0053: Folder summaries

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A5, A8, A12
- Date: 2026-10-13

## Context

The recall view lists navigation items, one per git-root folder and one per app (decision 0051). Each item has a
deterministic `summary` line, but "what was I doing in this folder, and where does it stand" is what a model-written
paragraph answers best. The summary service of WP8.1 (decision 0046) already has the runner, the queue, the TTL, the
digest cache, the guard environment and the `GET`/`POST /summaries/{scope}/{ref}` route, for the scopes `session`,
`workspace` and `day` (A8). `PLAN-final-plan.md` §4.1 creates `summaries` with `CHECK (scope_kind IN ('session',
'workspace', 'day'))`, which rejects any other scope, and SQLite cannot change a CHECK constraint in place. This record
refines §4.1 and the A8 scope list; the plan itself is not edited.

## Decision

- **A fourth scope, `folder`.** Its `ref` is the item `id`: the first 16 hex digits of `sha256(key)`. The id, not the
  path, is in the URL, so no path reaches an access log and no `/` is needed in a path parameter. Every item can be
  summarized, a folder or an app; the scope is named after the common case. The summary is one paragraph
  (`kind = text`).
- **The item is looked up in the current items**, the memoized ranker input of the default 14 days (the same items
  `GET /items` ranks). An id that no current item has is **404** `no item has that id` on `POST`, and on `GET` when
  nothing is cached or pending for it. A cached summary of an item that is gone is answered as `stale`, as an ended
  session's is.
- **The inputs hold no seconds and no times**, so the digest moves only when the content does: the item's kind, name,
  group and tickets; the branch, the PR number and state and `dirty` of its project; the label and title of each
  window place; the agent, title, one-liner and cached ARC (`sessions[].story.arc` of `/now`) of each session place;
  and the open and next todos of its project and of those sessions (at most eight). Places and sessions are sorted by
  content, not by focus, so a focus change alone never makes a summary stale. They come from `/now` and the stored
  spans, so every string is redacted already (A5). A session story that changes makes the folder summary stale.
- **The prompt** asks for one paragraph of at most three sentences: what was being done here and where it stands,
  naming the work, never repeating a title word for word beyond a ticket key.
- **Generation only by `POST /summaries/folder/{id}`**, behind the mutation guard (bearer, Origin, JSON content type,
  body limit, Host), and only while `[summaries] enabled = true`; while off it is 409 `disabled`. The runner, queue,
  TTL, digest cache, recursion guard (`FYLGJA_SUMMARY=1`, `LIDSKJALV_TRACK_SUMMARY=1`, the summarizer cwd) and the
  redaction and 600-character cut of decision 0046 apply unchanged. Output is never logged.
- **`/items` shows the cache only.** `NavItem.story = {text, generated_at, state}` is present only while summaries are
  enabled and a summary with text is cached for the item; its state is judged against the item's current inputs, the
  way `with_workspace_story` does it for `/now`. A read never starts a job. `q` also matches the story text, as the
  search rule of `GET /items` says.
- **Migration `0002_folder_summaries.sql`** rebuilds `summaries` with `CHECK (scope_kind IN ('session', 'workspace',
  'day', 'folder'))`: create `summaries_new`, copy every row with its id, drop `summaries`, rename. 0001 created no
  index, trigger or view on the table and no foreign key points at it, so nothing else is recreated. The runner applies
  the file in one transaction with `user_version = 2`.
- **Folder summaries are derived rows outside retention**, as decision 0046 says of every summary: kept until replaced,
  not rebuildable (A12).

## Consequences

- The store schema is at version 2. A database written by this version is refused by an older Fylgja ("newer than
  this Fylgja"), as for any migration.
- The recall view can show a folder's story and offer a Summarize button while summaries are enabled; the page reads
  `story` and posts, and the finished job's `summary.ready` and `now.changed` refresh it.
- With summaries off (the default) nothing changes: no job, no model call, and `/items` carries no `story`.
- An item's id follows its key, so a folder that moves or an app whose bundle id changes starts with no summary.
- `fylgja story folder` is not added; the CLI keeps `fylgja story session` and `fylgja story ws`.
