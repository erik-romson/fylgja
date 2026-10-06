# Project collector

The built-in `project` collector (`src/fylgja/collectors/project/`, capabilities `todos` and `project_state`) does two
jobs for every project the daemon knows. It imports the repo-root `TODO.md` as read-only todos of that project, and it
keeps the **project state** current: git branch, dirty, ahead/behind and last commit time, the pull request, the draupnir
group and the GitHub Actions build. It is enabled by default (`[collectors.project]` in `defaults.toml`), reads only and
installs nothing.

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

## Project state (WP5.4, §4.7, decision 0044)

A state loop runs beside the `TODO.md` loop, so a slow `gh` never delays a todo read.

| What | Command (always `git --no-optional-locks -C <root>`, or gh with the repository named) | When |
|---|---|---|
| branch, dirty, ahead/behind | `git status --porcelain=v2 --branch --untracked-files=normal` | every known project, every `state_interval_seconds` (5 min), sooner on focus |
| last commit time | `git log -1 --format=%ct HEAD` | with the status |
| remote, repository name | `git config --get remote.origin.url` (credentials are dropped) | with the status |
| draupnir group | the parent directory's name when it holds `draupnir.toml` (the resolver's `ProjectLocator`) | with the status |
| pull request | `gh pr view <branch> -R <owner>/<repo> --json number,state,reviewDecision,statusCheckRollup` | active projects, every 5 min, own budget `gh_pr_calls_per_hour` (120) |
| build | `gh api -i "repos/<owner>/<repo>/actions/runs?branch=<branch>&per_page=30"` with `If-None-Match` | active projects, every 5 min, sooner on focus or a moved branch tip, budget `gh_calls_per_hour` (30) |

An **active** project has a live session or was focused in the last `active_window_seconds` (1 h); only active projects
with a github.com `origin` and a branch are given to gh. Both budgets are a rolling sixty minutes, count every call (a
304 too), and serve the most active projects first. A refused call is not made; the section ages.

**Build fold.** `builds.py` is a pure port of Lidskjalv's `_BUILDS_FOLD_JQ`: the runs of the newest head sha, re-runs
collapsed to the highest run number, `skipped` and `neutral` dropped, each workflow's state, duration and typical duration
(the median of its successful runs on the page), and the worst state as the aggregate (`fail` > `running` > `cancelled` >
`pass` > `none`). The result is `projects[].build` with `source: "gh"`, `observed_at` and `freshness`. Parity goldens for
ten response fixtures come from the jq program (`spikes/gen_lidskjalv_build_goldens.py`, run once and committed under
`tests/fixtures/lidskjalv/builds/`).

**Freshness.** `fresh` within twice the poll interval, `stale` after that, `unavailable` with an `error_kind` when the
last attempt failed (the last fold is kept). No data yet: no `build` key. The `pr` object has no freshness of its own.

**Failures** are short codes: `git_missing`, `git_timeout`, `git_not_a_repository`, `git_failed`, `git_parse` (these
degrade the collector in `doctor`); `gh_missing`, `gh_not_authenticated` (blocks gh for 15 min), `gh_rate_limited`
(blocks it for an hour), `gh_offline`, `gh_not_found`, `gh_forbidden`, `gh_server_error`, `gh_timeout`, `gh_failed`,
`gh_parse` and `build_parse` (these mark the build `unavailable`; a gh problem does not degrade the collector).

**Settings** (`[collectors.project]`): `state_interval_seconds` 300, `focus_refresh_seconds` 30,
`active_window_seconds` 3600, `gh_calls_per_hour` 30, `gh_pr_calls_per_hour` 120, `command_timeout_seconds` 20,
`git_command` and `gh_command` (`git`, `gh`; give an absolute path when the daemon's `PATH` lacks Homebrew).

**Not stored.** `dirty_count`, `pr.checks` and `pr.url` have no column; they stay empty in `/now`.
