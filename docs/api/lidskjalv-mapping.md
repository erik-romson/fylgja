# Lidskjalv field mapping to `/now` (`fylgja.now/1`)

This is the Fylgja half of WP1.11: every field Lidskjalv reads from its own cache files, and the `/now` path
the Lidskjalv transform (`lib/fylgja_now.sh`, built in the Lidskjalv repository) reads instead. WP2.6 later wires
the same transform behind `LIDSKJALV_SOURCE_SESSIONS=fylgja`; WP5.4 extends this table with the builds, costs
and quota rows (A24, step 42); stories come later.

- **Where Lidskjalv reads the fields.** `plugins/lidskjalv_sessions.sh` (roster at lines 452–470, stories 600–607,
  summaries 615–624, lanes 626–700, records 1048–1069, `render_from` at 704) and `bin/lidskjalv.sh`
  (`_roster_live_sessions` at 496, `_panel_json` at 1418–1569). The read sites were revalidated against commit
  `ad327cc` in `docs/revalidation/2026-10-05-lidskjalv-read-sites.md`.
- **Paths** are dotted, with `[]` for an array: `sessions[].facts.title` is the `title` of the `facts` block of
  every element of `sessions`. A path ending in `[]` is an array of scalars.
- **`—`** in the `/now path` column means `fylgja.now/1` has no equivalent; the notes say why and what the
  transform does instead. `tests/unit/test_lidskjalv_mapping.py` checks every other path against
  `docs/api/now.schema.json` and the recorded documents in `tests/fixtures/now/`.
- **The session id** is `sessions[].native_id`; the session's key in `/now` is `sessions[].key`
  (`claude:<native_id>`). The transform keeps only sessions of kind `claude`.
- **Absent, never `null`.** `/now` leaves a field out when it has no value (decision 0030), so the transform
  reads each path with a default.

| Lidskjalv file | field | /now path | notes |
|---|---|---|---|
| `sessions/<sid>.json` | `session_id` | `sessions[].native_id` | The file name is the id too. A record without it is skipped, as today. |
| `sessions/<sid>.json` | `state` | `sessions[].state` | The vocabularies differ, see "State vocabulary" below. A session whose `state` is absent is treated as `idle`. |
| `sessions/<sid>.json` | `cwd` | `sessions[].cwd` | The registry's working directory, which is the launch directory. `sessions[].project_path` is the git root, not used for the chip name. |
| `sessions/<sid>.json` | `cwd_now` | — | Needs the working directory of each prompt, which arrives with the hook events. Until then the transform writes `cwd_now` equal to `cwd`, so the popup and panel show no subdirectory. |
| `sessions/<sid>.json` | `window_id` | `sessions[].host.window_id` | A string in `/now`, a number in the record. The transform converts it and leaves `null` when absent, which is the unresolved host. |
| `sessions/<sid>.json` | `window_id` (workspace join) | `sessions[].host.workspace` | Lidskjalv joins the window id against `aerospace list-windows` to get the workspace; `/now` already carries the workspace of the window, so the transform needs no AeroSpace call. |
| `sessions/<sid>.json` | `iterm_session` | — | `/now` has no iTerm2 session id; an iTerm2 session has `host.kind` equal to `iterm2` and a window only. The transform writes an empty `iterm_session`, so the click focuses the window and not the tab. |
| `sessions/<sid>.json` | `updated` | `sessions[].last_seen` | The last time Fylgja observed the session, not the last hook. It feeds the age and the stale rule, so the numbers can differ by the poll interval. |
| `sessions/<sid>.json` | `since` | `sessions[].state_since` | When the current state began, the value the default layout sorts on. Absent until the first state is known, so the transform falls back to `last_seen`, as Lidskjalv falls back to `updated`. |
| `sessions/<sid>.json` | `pid` | `sessions[].pid` | Lane lookups by `pid:<n>` use it. |
| `sessions/<sid>.json` | `detail` | — | The error type of a `failed` record. `/now` carries no error detail in Phase 1, so the failed chip shows no text. |
| `sessions/<sid>.json` | `seq` | — | Write-side ordering of hook writes. The renderer and the panel never read it. |
| `sessions/<sid>.json` | `untracked` | — | Not stored: the renderer sets it for a roster session without a record. Every session in `/now` is tracked, so the transform writes it empty. |
| `lane.json` | `lane_id` | `sessions[].lane_key` | `lane_key` equals the process key (A11), so a lane survives `/clear` as it does in Lidskjalv. The transform builds the lane id from it; Lidskjalv's `pid-<pid>` ids are not reproduced. |
| `lane.json` | `pid` | `sessions[].pid` | Same field as the record. |
| `lane.json` | `started_at` | `sessions[].first_seen` | The registry's `startedAt` when known, else the first observation. |
| `lane.json` | `ended_at` | — | `/now` lists live sessions only. Ended lanes, which the panel keeps as history, stay on Lidskjalv's own lane files. |
| `lane.json` | `cwd` | `sessions[].cwd` | Same field as the record. |
| `lane.json` | `cwd_now` | — | Same gap as the record. |
| `lane.json` | `window_id` | `sessions[].host.window_id` | Same field as the record. |
| `lane.json` | `session_id` | `sessions[].native_id` | The session the lane is currently on. After `/clear` the lane keeps its `lane_key` and gets the new session; the old one is `sessions[].predecessor_key`. |
| `lane.json` | `key_source` | — | How Lidskjalv derived the lane id (`pid`, `tab`, `session`). Internal and never read by a renderer. |
| `lane.json` | `proc_start` | `sessions[].process_key` | The process key is the pid plus the process start time. |
| `lane.json` | `activity` (sibling file) | `sessions[].one_liner` | The popup line is, best first, a story, a title, then this activity text. The transform uses `sessions[].facts.title` for the title and `one_liner` for the activity. |
| `lane.json` | `reminders.jsonl` (sibling file) | — | Reminder injection stays in Lidskjalv's own hook (A18, A24). The bell on a chip keeps reading Lidskjalv's lane files. |
| `summaries/<sid>.json` | `session_id` | `sessions[].native_id` | Summaries are keyed by conversation, which is the session id. |
| `summaries/<sid>.json` | `title` | `sessions[].facts.title` | The facts block is a port of Lidskjalv's `_SUMMARY_JQ`. |
| `summaries/<sid>.json` | `prompt` | `sessions[].facts.prompt` | The last prompt, at most 300 characters. |
| `summaries/<sid>.json` | `said` | `sessions[].facts.said` | The last thing the model said. |
| `summaries/<sid>.json` | `mode` | `sessions[].facts.mode` | |
| `summaries/<sid>.json` | `branch` | `sessions[].facts.branch` | |
| `summaries/<sid>.json` | `version` | `sessions[].facts.version` | |
| `summaries/<sid>.json` | `model` | `sessions[].facts.model` | |
| `summaries/<sid>.json` | `turns` | `sessions[].facts.turns` | |
| `summaries/<sid>.json` | `started` | `sessions[].facts.started` | Epoch seconds of the first transcript line. |
| `summaries/<sid>.json` | `last_at` | `sessions[].facts.last_at` | Epoch seconds of the last transcript line. The panel orders a card by it. |
| `summaries/<sid>.json` | `prompts` | `sessions[].facts.prompts[]` | Every typed prompt, oldest first. |
| `summaries/<sid>.json` | `tools` | `sessions[].facts.tools` | A map from tool name to call count. |
| `summaries/<sid>.json` | `files` | `sessions[].facts.files[]` | Files edited or written, at most 20. |
| `summaries/<sid>.json` | `todos` | `sessions[].facts.tasks[]` | Lidskjalv's `todos` is `facts.tasks` in `/now` (the name `todos` is kept for Fylgja's own todos). |
| `summaries/<sid>.json` | `todos[].status` | `sessions[].facts.tasks[].status` | |
| `summaries/<sid>.json` | `todos[].content` | `sessions[].facts.tasks[].content` | |
| `roster.json` | `fetched_at` | `generated_at` | Lidskjalv's roster ages by `fetched_at`; the transform uses the time `/now` was generated, so the roster is never older than the document. |
| `roster.json` | `sessions[].sessionId` | `sessions[].native_id` | Every session in `/now` is on the roster; there is no separate roster-only list. |
| `roster.json` | `sessions[].kind` | `sessions[].entrypoint` | Lidskjalv keeps `kind` equal to `interactive`. The transform keeps a session when `entrypoint` is `cli`, which leaves out headless `claude -p` runs. |
| `roster.json` | `sessions[].status` | `sessions[].state` | `busy` and `idle` are the registry's own states, kept as `busy` and `idle` in `/now` until hooks give finer ones. A headless run has no status; the transform drops a session without `state`. |
| `roster.json` | `sessions[].cwd` | `sessions[].cwd` | `LIDSKJALV_IGNORE_CWD` is applied to it by the transform. |
| `roster.json` | `sessions[].startedAt` | `sessions[].first_seen` | Milliseconds in the registry, an ISO timestamp in `/now`. |
| `builds/builds.json` | `<root>` | `projects[].path` | The key of an entry: Lidskjalv keys it by repository root and joins a session to it when the session's `cwd` is the root or below it. `/now` lists the build on the project of the session (`sessions[].project_path`, `projects[].sessions[]`), so the transform needs no prefix match. Only a project with a GitHub remote has a `build`. |
| `builds/builds.json` | `state` | `projects[].build.state` | `pass`, `fail`, `running`, `cancelled` or `none`: the same five values, from a Python port of `_BUILDS_FOLD_JQ` that the parity goldens check. |
| `builds/builds.json` | `head_sha` | `projects[].build.head_sha` | The newest head sha of the branch's runs; absent when the branch has no runs. |
| `builds/builds.json` | `local_head_sha` | — | The sha of the local checkout, written by Lidskjalv's own git call, which `/now` does not carry. Without it the transform cannot set the panel's `stale` flag (local differs from remote) and writes `false`; `projects[].ahead` and `projects[].behind` say the same thing in another way. |
| `builds/builds.json` | `url` | `projects[].build.url` | The checks page of the head sha. |
| `builds/builds.json` | `workflows[].workflow` | `projects[].build.workflows[].workflow` | The workflow file's name (the basename of its path). |
| `builds/builds.json` | `workflows[].state` | `projects[].build.workflows[].state` | Same five values as the aggregate. |
| `builds/builds.json` | `workflows[].status` | `projects[].build.workflows[].status` | GitHub's own `status` of the run. |
| `builds/builds.json` | `workflows[].conclusion` | `projects[].build.workflows[].conclusion` | GitHub's own `conclusion`; absent while the run is not completed. |
| `builds/builds.json` | `workflows[].run_id` | `projects[].build.workflows[].run_id` | The run's id. |
| `builds/builds.json` | `workflows[].url` | `projects[].build.workflows[].url` | The run's page. |
| `builds/builds.json` | `workflows[].run_started_at` | `projects[].build.workflows[].run_started_at` | Epoch seconds in Lidskjalv's file, an ISO timestamp in `/now`; the panel's `age` is the time since it. |
| `builds/builds.json` | `workflows[].run_duration` | `projects[].build.workflows[].run_duration_s` | Seconds; absent for a run that is not completed. Renamed with its unit. |
| `builds/builds.json` | `workflows[].typical_duration` | `projects[].build.workflows[].typical_duration_s` | The median duration of the workflow's successful runs on the page, in seconds; absent when it has none. Renamed with its unit. |
| `builds/builds.json` | `checked_at` | `projects[].build.observed_at` | Epoch seconds in Lidskjalv's file, an ISO timestamp in `/now`. A failed attempt moves it too, so read `projects[].build.freshness` for the verdict. |
| `builds/builds.json` | `next_check_at` | `projects[].build.freshness` | Lidskjalv's schedule decides when an entry is looked at again. Fylgja has one slow timer (5 minutes) plus focus and branch-tip triggers and reports the result as `fresh` (within twice the interval), `stale` or `unavailable`, which replaces the schedule. |
| `builds/builds.json` | `etag` | — | The conditional-request tag is the collector's own memory (decision 0044); it is never part of `/now`. |
| `builds/builds.json` | `rate_remaining` | — | GitHub's remaining quota, used by the collector to stop calling when it is below 100. When gh is blocked the build is `unavailable` with `projects[].build.error_kind` (`gh_rate_limited`). |
| `builds/builds.json` | `owner_repo` | `projects[].repo_name` | Fylgja's `repo_name` is the repository's name without the owner; the owner is in `projects[].remote_url`. |
| `builds/builds.json` | `branch` | `projects[].branch` | The branch of the project's checkout, which is the branch the runs are read for. |
| `builds/builds.json` | `root` | `projects[].repo_root` | The repository root. |
| `costs/costs.json` | `<session_id>` | `sessions[].native_id` | The key of an entry. `costs.json` is keyed by session id; `/now` carries the tokens on the session itself, so the transform joins nothing. |
| `costs/costs.json` | `input` | `sessions[].tokens.input` | The sum of `input_tokens` over the transcript's assistant lines, by a Python port of `_COSTS_JQ`. |
| `costs/costs.json` | `output` | `sessions[].tokens.output` | The sum of `output_tokens`. |
| `costs/costs.json` | `cache_read` | `sessions[].tokens.cache_read` | The sum of `cache_read_input_tokens`. |
| `costs/costs.json` | `cache_write` | `sessions[].tokens.cache_write` | The sum of `cache_creation_input_tokens`. |
| `costs/costs.json` | `session_id` | `sessions[].native_id` | The session the sums belong to. |
| `showy-quota: claude` | `five_hour` used percentage | `usage.providers[].used_pct` | Element with `provider` equal to `claude` and `window` equal to `five_hour`. **Covered**: Claude's `rate_limits` from the statusline snapshot, which exists for claude.ai Pro and Max accounts once a session has had its first response. With no recent snapshot the element is `stale`. |
| `showy-quota: claude` | `five_hour` reset time | `usage.providers[].resets_at` | **Covered**, as the percentage. An ISO timestamp, where the statusline has epoch seconds. |
| `showy-quota: claude` | `seven_day` used percentage | `usage.providers[].used_pct` | Element with `window` equal to `seven_day`. **Covered**, as the five-hour window. |
| `showy-quota: claude` | `seven_day` reset time | `usage.providers[].resets_at` | **Covered**, as the weekly percentage. |
| `showy-quota: codex` | `five_hour` used percentage | `usage.providers[].used_pct` | Element with `provider` equal to `codex` and `window` equal to `five_hour`. **Covered** while CodexBar can read Codex (`codexbar usage --provider codex`); otherwise the element is `unavailable` with `error_kind`, and the transform keeps the `lidskjalv` source for the provider. |
| `showy-quota: codex` | `five_hour` reset time | `usage.providers[].resets_at` | **Covered**, as the percentage. |
| `showy-quota: codex` | `seven_day` used percentage | `usage.providers[].used_pct` | Element with `window` equal to `seven_day`. **Covered**, as the five-hour window. |
| `showy-quota: codex` | `seven_day` reset time | `usage.providers[].resets_at` | **Covered**, as the weekly percentage. |
| `showy-quota: copilot` | used percentage | `usage.providers[].used_pct` | **Not covered** on this machine: CodexBar 0.55.0 has no fetch strategy for Copilot without a token of its own, so Fylgja lists the provider `unavailable` with `error_kind` `codexbar_no_strategy` and no percentage. Lidskjalv keeps its own source (showy-quota) for `copilot` until CodexBar can answer; WP5.5 revalidates this. |
| `showy-quota: copilot` | reset time | `usage.providers[].resets_at` | **Not covered**, as the percentage. |
| (new in Fylgja) | `sessions[].cost_usd` | `sessions[].cost_usd` | The session's total cost from the statusline snapshot (`cost.total_cost_usd`, a list-price estimate that resets with `/clear`). Lidskjalv has no equivalent today; additive. |
| (new in Fylgja) | `sessions[].context_pct` | `sessions[].context_pct` | The context window's used percentage from the statusline snapshot. Additive. |
| (new in Fylgja) | `usage.cost` | `usage.cost.usd` | The spend over `usage.cost.period` (`30d`), summed over the providers CodexBar prices from local logs (`codex` and `claude`). A list-price estimate. Additive. |
| (new in Fylgja) | `usage.providers[]` freshness | `usage.providers[].freshness` | Every provider element also has `source`, `observed_at`, `freshness` and, when the last attempt failed, `error_kind`. The quota switch shows an `unavailable` or `stale` element dimmed instead of its percentage. Additive. |
| (new in Fylgja) | `sessions[].tokens` freshness | `sessions[].tokens.freshness` | `tokens` also has `source` (`transcript`), `observed_at` and `freshness`. Additive. |

## State vocabulary

`sessions[].state` is one of the `SessionState` values (`fylgja.now/1`). Lidskjalv's records use `running`,
`compacting`, `blocked`, `failed`, `idle`, and the renderer adds `stale`. The Lidskjalv transform translates:

- `busy`, `working`, `running_tool`, `running` become `running`;
- `waiting`, `waiting_permission`, `waiting_input` become `blocked`;
- `idle`, `turn_done`, `seen`, `passed`, `paused` become `idle`;
- `error`, `failed` become `failed`;
- `ended` and `cancelled` are dropped (the session is not live);
- `stale` and `unknown` become `idle`, and the renderer's own stale rule then applies to `updated`.

Lidskjalv's `compacting` has no `/now` equivalent in Phase 1; a compacting session reads as `running`.

## Builds, costs and quota

These rows come with WP5.4 (A24). Lidskjalv keeps its own builds, costs and quota paths; WP5.5 adds a switch
(`lidskjalv|fylgja`, default `lidskjalv`) that reads the `/now` paths instead.

- **Builds** (`builds/builds.json`, one entry per repository root, folded by `_BUILDS_FOLD_JQ`) are
  `projects[].build`. The fold is a Python port checked against goldens generated once from the jq program.
  Fylgja's budget differs on purpose (decision 0044): it is a rolling sixty minutes and counts a `304`.
- **Costs** (`costs/costs.json`, keyed by session id, from `_COSTS_JQ`) are `sessions[].tokens`. The port is exact,
  so the numbers are the ones Lidskjalv shows. That includes the known overcount: Claude writes one transcript
  line per content block, each with the message's usage, and `_COSTS_JQ` sums every line (revalidation log
  `2026-10-06-usage.md`, decision 0045).
- **Quota** (showy-quota providers `claude`, `copilot` and `codex`) is `usage.providers[]`. Claude comes from the
  statusline's `rate_limits`, Codex and Copilot from the CodexBar CLI. The table says per provider whether
  Fylgja's sources cover it: `claude` and `codex` are covered, `copilot` is not on this machine. A provider that
  Fylgja cannot supply is listed `unavailable` with its `error_kind`, and the transform keeps Lidskjalv's own
  source for that provider.
- **Absent, never `null`.** A section with no data yet is absent. Before the first statusline snapshot there is
  no Claude element, and with CodexBar missing every other provider is `unavailable` with
  `error_kind` `codexbar_missing`; the reason also shows in the `project` entry of `collectors[]`.

## Not mapped here

- Stories come later (A24).
- The panel's `build` and `costs` objects read `builds/builds.json` and `costs/costs.json`; the transform
  rebuilds them from the rows above. Ended sessions are not in `/now`, so their token sums stay on
  Lidskjalv's own files.

## The recorded documents

`tests/fixtures/now/` holds the `/now` documents the transform is tested against. They come from
`scripts/record_now_fixtures.py`, which builds the real daemon and API in-process over synthetic collectors, a
fixed clock and a fake filesystem. Run it by hand after a deliberate model change; the tests never write them.

| Document | Covers |
|---|---|
| `states-and-hosts.json` | Session states `idle`, `busy` and `waiting`; host kinds `cmux` (with its exact surface), `jetbrains`, `iterm2` and an unresolved host; the facts block. |
| `clear-predecessor.json` | A session that replaced another in the same process: `lane_key` and `predecessor_key`. |
| `degraded-sessions.json` | The sessions collector is degraded, so `sessions[]` is absent and `degraded` names it. |
| `phase5-usage-builds.json` | A Claude session with `tokens`, `cost_usd` and `context_pct`; a project with a failing `build` of two workflows; `usage` with Claude's five-hour and weekly windows, Codex's windows, Copilot `unavailable`, and the spend. |
