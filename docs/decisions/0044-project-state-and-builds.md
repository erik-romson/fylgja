# 0044: Project state and builds: details

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A21, A24
- Date: 2026-10-06

## Context

WP5.4 gives the `project` collector the project state (git, PR, builds) on a slow timer, with the Actions call under an
ETag and a calls-per-hour budget (§4.7). The plan fixes the commands, the `projects[].build` shape, the freshness rule and
the default budget of 30. It leaves open how the budget counts, which projects get which calls, how a stored section
knows its poll interval, and how a value is cleared when a branch has no PR or no upstream. `docs/revalidation/
2026-10-06-project-state.md` found that gh exits 1 for a 304, that `gh pr view` without a PR is a stderr message, and
that a collector's process runner has no working directory.

## Decision

- **Git runs for every known project, gh only for active ones.** git (`--no-optional-locks`: status, last commit time,
  remote) is local and cheap, so it runs for every project on the slow timer (`state_interval_seconds`, 5 min) and
  sooner (after at least `focus_refresh_seconds`, 30 s) when focus moves into the project or the branch tip moved. The PR
  and the Actions runs are looked up only for a project with a live session or focus within the last hour, with a
  github.com `origin` and a branch. The commands name the repository and branch (`gh pr view <branch> -R o/r`,
  `repos/o/r/actions/runs?branch=…`), because the runner has no working directory.
- **The budget is a rolling window and counts every call, a 304 too.** `gh_calls_per_hour` (30, Lidskjalv's default) caps
  the Actions calls in any sixty minutes; `gh_pr_calls_per_hour` (120) does the same for `gh pr view`. Lidskjalv's hour
  bucket does not count a 304, because GitHub does not charge it to the rate limit. Here the budget is a ceiling on how
  often a process is started and a request sent, and "never exceeded" must be testable by counting calls. A refused call
  is not an attempt: no record, no error, the section ages to `stale`. The most active projects (focused, then live
  sessions, then most recent focus) are served first.
- **One timer, one meaning of fresh.** Lidskjalv's adaptive schedule (60 s while running, 15 min when done) is replaced by
  the slow timer plus the focus and branch-tip triggers, so a build is observed every `state_interval_seconds`. Each
  stored build carries that interval (`BuildPayload.poll_interval_s`, inside the `build` JSON), and `/now` judges
  freshness against it (`fylgja.core.freshness`): fresh within twice the interval, stale after that, unavailable with
  `error_kind` when the last attempt failed. A failed attempt keeps the last fold and flags it.
- **gh problems block gh, they do not degrade the collector.** A missing or unauthenticated gh blocks all gh calls for
  15 minutes, rate-limited (a 403 or 429, or fewer than 100 calls remaining) for an hour; while blocked each active
  project's build is emitted as `unavailable` with that `error_kind`. Any other failure backs off 300 s, doubling to an
  hour. `doctor` reports `degraded` for a git failure (`git_missing`, `git_timeout`, `git_not_a_repository`, …), and names
  a blocked gh in its detail.
- **"No value" overwrites, only when the observation is complete.** `upsert_project` kept stored values when a field was
  `None`, so a branch without an upstream, a detached HEAD or a branch without a PR would keep the old branch's numbers.
  A git observation always carries `dirty`, so one that does replaces `branch`, `ahead`, `behind` and `last_commit_at`
  even with `None`; a PR observation always carries `pr_number` (0 for "looked up, there is none") and then replaces
  `pr_state` and `review_decision`. Observations that carry neither (the resolver's) keep the old behaviour. `/now` shows
  no `pr` for `pr_number` 0 or `null`.
- **`ProjectPayload` gains `group_name`** (the draupnir workspace, from the resolver's `ProjectLocator`), and
  `BuildPayload` gains `poll_interval_s`. Neither changes `fylgja.now/1`: the `/now` models already held `group`, `build`
  and `freshness`.

## Consequences

- `/now` `projects[]` keeps being the projects of live sessions; the new fields fill from the stored rows, so a project
  shows `branch`, `dirty`, `ahead`, `behind`, `last_commit_at`, `pr` (number, state, review decision) and `build`.
  `dirty_count` and `pr.checks` and `pr.url` stay empty: the store has no column for them (the parser still aggregates the
  rollup, so a later migration could fill `checks`).
- A busy day with more active projects than the budget allows leaves the least active builds `stale`, which is what the
  plan's budget is for. Raising `gh_calls_per_hour` is a config change.
- The ETag lives in memory, so after a daemon restart the first call of each project is unconditional.
- Tests never need gh: the parity goldens are produced once by `spikes/gen_lidskjalv_build_goldens.py`.
