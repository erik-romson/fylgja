# Codex collector

The built-in `codex` collector (`src/fylgja/collectors/codex/`, capability `sessions`) lists the running Codex CLI
processes. It is enabled by default (`[collectors.codex]` in `defaults.toml`) and reads only; it installs nothing.

## What it reads

| Source | Used for |
|---|---|
| `pgrep -x codex` | the Codex processes of this user |
| `lsof -nP -a -p <pids> -d ^txt,^mem -Fpfn` | each process's working directory and open rollout (`rollout-<timestamp>-<thread id>.jsonl`) |
| `~/.codex/state_N.sqlite`, opened with `file:…?mode=ro` and `query_only` | the thread's cwd, branch, model, title, name and CLI version |
| the last 256 KiB of the rollout (widened up to 4 MiB) | busy or idle, the last user message, the current function call |
| `ps -A -o pid=,ppid=,lstart=,comm=` | the process key and ancestor chain the resolver uses to find the host app |

Sessions are complete snapshots, keyed `codex:<thread id>`, every `poll_interval_seconds` (2 s). Busy or idle is
`task_started` newer than `task_complete`/`turn_aborted`. The one-liner follows §4.6: the current function call while
busy, else the thread title and the last user message.

## Settings (`[collectors.codex]`)

`enabled`, `codex_home` (default `~/.codex`), `poll_interval_seconds`, `heartbeat_seconds`, `process_name`,
`pgrep_command`, `lsof_command`, `ps_command`, `command_timeout_seconds`, `tail_bytes`, `tail_max_bytes`.

## Health

`degraded` with an `error_kind` of: `pgrep_*` / `lsof_*` (command missing, failing or timing out), `codex_home_missing`,
`state_db_missing`, `unknown_state_schema` (a `state_N` this collector has not been revalidated for),
`state_schema_mismatch`, `state_db_unreadable`. Only the command failures stop the listing; the state-database
problems list the sessions from the process and rollout alone. See `docs/decisions/0035-codex-collector-details.md`.

## Optional hooks and `notify` (off by default)

Codex 0.154 has stable lifecycle hooks and a legacy `notify` command. Neither is needed for the collector, and
Fylgja does not install them. To spool events into `hook-inbox/codex/` by hand:

`~/.codex/hooks.json` (events: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`,
`PostToolUse`, `PreCompact`, `PostCompact`, `SubagentStart`, `SubagentStop`, `Stop`), one entry per event whose command
is

```text
fylgja hook --source codex <Event>
```

`fylgja hook` reads the event JSON on stdin, exits 0 whatever happens and writes an envelope (see
`docs/api/inbox-envelope.md`). `notify = ["…"]` in `config.toml` passes its JSON (`agent-turn-complete`, with
`thread-id`, `turn-id`, `cwd`) as the **last argument**, not on stdin, so it needs a small wrapper script that pipes
that argument into `fylgja hook --source codex agent-turn-complete`. The collector does not drain this inbox yet.

## Tests

`tests/unit/test_codex_parse.py`, `tests/unit/test_codex_collector.py`, `tests/contract/test_codex_contract.py`, the
Codex goldens in `tests/unit/test_one_liner.py` and, by hand, `tests/live/test_codex_live.py`
(`FYLGJA_RUN_LIVE=1`). Fixtures: `tests/fixtures/codex/`.
