# Project collector

The built-in `project` collector (`src/fylgja/collectors/project/`, capability `todos`) imports the repo-root `TODO.md`
of every project the daemon knows as read-only todos of that project. It is enabled by default
(`[collectors.project]` in `defaults.toml`), reads only and installs nothing. Step 41 adds the project state (git, PR,
builds) to it.

## What it reads

| Source | Used for |
|---|---|
| the daemon's own store (`fylgja.db`), opened with `file:…?mode=ro` and `query_only` | the known projects: the `projects` rows that have a `repo_root` |
| `<repo_root>/TODO.md` (`stat`, read, `stat` again) | the tasks |

Every `poll_interval_seconds` (5 s) each known project's `TODO.md` is looked at; a file whose `stat` is unchanged since
its last good read is not read again. When its tasks changed, one complete list is emitted, and the core reconciles it
(decision 0041).

## What counts as a task

```markdown
- [ ] an open task
- [x] a done task (also [X])
- [ ] next: a task to do next
- [ ] another task to do next #next
  - [ ] nested tasks count too (any indentation, and * or + bullets)
```

Headings, prose, plain bullets, `[ ]` without text and everything inside fenced code blocks are not tasks. The `next`
marker (a leading `next:` or a standalone `#next` tag, any case) is not part of the text, and a done task stays done.

## Identity and reconciliation

A line has no id, so a task is the file plus its text (checkbox and `next` marker stripped, whitespace collapsed) plus
its ordinal among identical lines. Ticking, unticking, marking `next` or moving a line updates the same todo. **Editing
the text of a line removes the old todo and adds a new one.** A removed line's todo gets `deleted_reason =
source_absent`; if the line comes back, the same todo is restored. A missing `TODO.md` is an empty list.

Imported todos are read-only in Fylgja: `fylgja todo done|rm` and `PATCH`/`DELETE /todos/{id}` refuse them (409).
Change the line in `TODO.md` instead.

## Failures

A file that cannot be read as a whole changes nothing: its todos stay as the last good read left them, and doctor
reports the collector `degraded` until it reads again. The codes are `todo_md_unreadable` (permission or other I/O
error), `todo_md_not_a_file`, `todo_md_partial_read` (the file changed while it was read), `todo_md_not_utf8`,
`todo_md_binary`, `todo_md_too_large` (over 1 MiB), `todo_md_too_many_items` (over 1000 tasks) and `store_unreadable`.
Nothing about a file's content or path is logged.
