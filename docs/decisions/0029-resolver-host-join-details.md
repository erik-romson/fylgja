# 0029: Resolver host join: process chains on the session record, cmux store keys, JetBrains project names

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A1, A3, A21
- Date: 2026-10-05

## Context

WP1.8 places each session in a project, a host app, a window and an AeroSpace workspace. Decision 0025 chose
cmux's session store for the cmux join and left the details open. Reading the store and the join spike
(`docs/spikes/0001-join.md`) while building the resolver showed three things the plan does not say.

- The resolver is pure, so the ancestor chain of a session has to arrive with the session. The registry row has
  no chain; only `ps` knows it.
- `~/.cmuxterm/claude-hook-sessions.json` is a `version: 1` object whose `sessions` is a map from session id to a
  record in **camelCase** (`sessionId`, `pid`, `workspaceId`, `surfaceId`, `agentLifecycle`, `cwd`). Decision 0025
  names the snake_case fields that `cmux sessions --json` prints, including `stored_pid_exists`, which the raw file
  does not have.
- Three of six JetBrains rows of the spike capture stay unresolved, because the IDE's project name is not the
  directory name and `frameTitle` is empty. The spike named `.idea/.name` as the first candidate fix.

## Decision

- **Process chains.** The Claude adapter runs `ps -A -o pid=,ppid=,comm=` in a poll that has a live session
  without a chain, and the session record carries `ancestry`, a tuple of `{pid, ppid, comm}` from the session
  process upwards (at most 64, pid 1 excluded). The chain is not a column and is not stored with snapshots. A
  failing call costs only the chains. The resolver walks the chain to the first pid that owns an AeroSpace window.
- **cmux store keys.** The parser accepts both spellings of each allowlisted key. A record whose `stored_pid_exists`
  is `false` is not joined; when the key is absent (the raw file), the pid match with the live session stands in
  for it. A match on session id and pid is `high`, a match on session id alone is `medium`.
- **`via` and `confidence`.** `cmux-hook-env` and `cmux-session-store` (decision 0025) for the exact cmux workspace
  and surface; `parent-chain` for the window level of every host (`window-only` for cmux and unknown hosts, `low`
  for iTerm2); `parent-chain+project-path` for JetBrains (`high` when the title shows `[<path>]`, `medium` for a
  name match); `parent-chain+title` for iTerm2. A workspace-only join is `low`. `host_ref` holds
  `<cmux workspace id>/<surface id>` for cmux and the IDE project path for JetBrains.
- **JetBrains project name.** The adapter also reads `<project>/.idea/.name` of each opened project and accepts it
  as the name a window title may show. Without the file the match is exactly the spike's, so the recorded-capture
  test agrees with the spike table row for row.
- **Project grouping.** A project is the git root of the cwd (found by `.git` existing, walking up), else the cwd.
  A git root whose parent directory holds `draupnir.toml` belongs to the group named after that directory; so does
  a cwd that is the draupnir workspace directory itself. The lookup is a cached `exists` check behind the
  filesystem seam; no file is read and `git` is not run.
- **Project to workspace.** The workspace with the most of the project's windows; sessions that share a window
  count it once; a tie goes to the workspace that sorts first, at `low` confidence.

## Consequences

- `SessionPayload` gained `ancestry`. `/now` schema and the store are unchanged (the schema is built in step 14).
- The daemon calls `Resolver.observe(record)` for every record that goes to the writer and
  `Resolver.refresh()` after sessions or windows change; step 14 and the daemon wiring own that trigger.
- A cmux upgrade that renames the store keys makes `parse_cmux_store` return no records or `None` (shape check),
  so the join falls back to the window level; `CmuxSource` reports `unknown cmux version` as `degraded`.
- The hook-time cmux ids are accepted through `Resolver.observe_cmux_ids` and need step 19 to deliver them.
