# 0041: Read-only todo imports: lists, keys and reconciliation details

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A5, A20, A23
- Date: 2026-10-06

## Context

WP4.3 imports repo-root `TODO.md` checkbox lines and agent task lists (Claude's from the transcript, decision 0028;
OpenCode's `todo` table) as read-only todos, reconciled on every complete read. The plan fixes the rules (keys, upsert,
`source_absent`, restore, failed reads never reconcile, read-only) but leaves open how a snapshot names its list, the
`source` names, the `next` marker, what a key looks like for a list with no native ids, where the `project` collector
learns the known projects, and what "a session that ended with its task list removed" means when the source keeps the
rows. The revalidation (`docs/revalidation/2026-10-06-todo-sources.md`) found that `TodoWrite` items carry no id, that
the `TaskCreate` result names the id it gave, and that OpenCode keeps a session's `todo` rows after its process ends.

## Decision

- **One snapshot, one list** (`fylgja.collectors.todo_lists`). A TODOS snapshot's `entity_id` is
  `<import source>:<list ref>`; each item's `entity_id` is its key in the list (no `#`, no control characters); the
  stored `source_ref` is `<list ref>#<item key>`, unique per source. The import source must be the collector's name or
  start with `<collector>-` and is never `fylgja`, so a collector reconciles only its own lists and never Fylgja's own
  todos. The sources are `project-todo-md`, `claude-tasks` and `opencode-todo`. A todo snapshot whose `entity_id` is not
  of that form is no import and is kept in `events` as before.
- **Keys.**
  - `TODO.md`: the list is the file path; the key is the first 16 hex digits of the SHA-256 of the normalized text plus
    `:<ordinal>` among identical normalized lines, top to bottom. The text is hashed, not copied, because the ingest gate
    redacts the text and not the `source_ref`. A checkbox or `next` change keeps the key (an update); a text edit is a
    removal plus a new row, as WP4.3 documents.
  - Claude: the list is the session key. A task-tool task is `task-<id>`, the id taken from the `Task #<id> created`
    result of its `TaskCreate` call, falling back to the creation count of decision 0028. A `TodoWrite` item has no id
    and the tool rewrites the whole list, so it is `todo-<position>`.
  - OpenCode: the list is the session key; a row is keyed by its `position`, the table's own key within a session.
- **`TODO.md` grammar.** A task is a list item at any indentation with `-`, `*` or `+`, a space, `[ ]` (open) or
  `[x]`/`[X]` (done), a space and non-empty text. Lines in fenced code blocks are not tasks. An open task whose text
  starts with `next:` or holds a standalone `#next` tag (any case) is `next`; the marker is not part of the text. A file
  that is not UTF-8 text, holds NUL bytes, is over 1 MiB or has more than 1000 tasks is a failed read.
- **Agent statuses.** `pending` is `open`, `in_progress` is `next`, `completed` and `cancelled` are `done`; anything
  else is `open`.
- **Scopes and links.** `TODO.md` items are scoped to their project (`projects.path`) and link to the file path; agent
  tasks are scoped to their session and link to the session key (what `/sessions/{key}` and `fylgja focus session`
  take).
- **Known projects.** The `project` collector reads the `projects` rows of the daemon's own store read-only
  (`mode=ro`, `query_only`); only a project with a `repo_root` has a repo-root `TODO.md`. A collector has no other view
  of the live view, and this changes neither the collector API nor the single writer. A project the store stops listing
  is not read any more, which is not an empty read: its rows stay.
- **Sessions.** A session's own list ends with the session. The collectors emit a complete, empty snapshot for a
  session they stopped listing, and the writer also marks the imported todos scoped to a session `source_absent` when
  that session ends, unless a read restored them after the end. The second rule covers a session that ended while the
  daemon was down (the first complete session read after a restart ends it) and OpenCode, whose database keeps the
  rows.
- **Whole-snapshot refusal.** A snapshot with an invalid item (no text, no or a bad scope, a bad key or link), two items
  with one key, more than 5 000 items, or older than the newest change its list holds (the A20 staleness rule) changes
  nothing at all, and the writer logs the collector and the reason, never text. An incomplete snapshot upserts and
  restores but never removes. Reconciled lists are not copied into `events`.
- **Read-only.** `TodoService.update` and `delete` raise `TodoReadOnly` for any row whose source is not `fylgja`;
  `PATCH` and `DELETE /todos/{id}` answer 409 with a message naming the source, and the CLI prints it.
- **Obsidian** stays on hold (A23); nothing here reads a vault.

## Consequences

- The `fylgja.now/1` schema is unchanged: imported todos fill the existing `todos` fields with `source`, `source_ref`
  and `link`.
- Editing a `TODO.md` line's text gives the item a new id; anything that refers to the old id (a `/next` suggestion) no
  longer finds it. Reordering an OpenCode list or a `TodoWrite` list rewrites texts in place under the same ids.
- A failed read keeps the last good list and marks the `project` collector `degraded` with a short `error_kind`
  (`todo_md_unreadable`, `todo_md_not_a_file`, `todo_md_partial_read`, `todo_md_not_utf8`, `todo_md_binary`,
  `todo_md_too_large`, `todo_md_too_many_items`, or `store_unreadable`) until a read of that file succeeds.
- A clock that jumps backwards can hold a list for the length of the jump: a read older than what the list holds is
  refused as stale.
- Project todos appear in `/now` only under projects `/now` lists (those with a live session), as for native todos
  (step 33); `GET /todos?project=` lists them all.
