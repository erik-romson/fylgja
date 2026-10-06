# OpenCode collector

The built-in `opencode` collector (`src/fylgja/collectors/opencode/`, capability `sessions`) lists the running
OpenCode sessions. It is enabled by default (`[collectors.opencode]` in `defaults.toml`) and reads only; it installs
nothing. An optional TypeScript plugin makes it faster.

## What it reads

| Source | Used for |
|---|---|
| `pgrep -x opencode` | the OpenCode processes of this user |
| `lsof -nP -a -p <pids> -d ^txt,^mem -Fpfn` | each process's working directory |
| `~/.local/share/opencode/opencode.db` (or `$XDG_DATA_HOME/opencode/opencode.db`), opened with `file:…?mode=ro` and `query_only` | the sessions of those directories, their newest messages, the tool parts of the open turn and the `todo` table |
| `ps -A -o pid=,ppid=,lstart=,comm=` | the process key and ancestor chain the resolver uses to find the host app |

Sessions are complete snapshots, keyed `opencode:<session id>`, every `poll_interval_seconds` (2 s). Message and part
text is never read: the queries return roles, times, tool names and six short tool-input fields.

- **Which session.** A process runs the newest top-level (`parent_id` null), not archived session whose `directory`
  is the process's working directory. Several processes in one directory take that directory's newest sessions,
  matched by age (older process, older session). A process with no session row is not listed (doctor counts it).
- **Busy or idle.** The newest assistant message with no `time.completed` is a turn in flight, unless it carries an
  `error` or nothing about the session was updated for `busy_stale_seconds` (30 min): the database keeps dead turns
  for months.
- **Activity (§4.6).** The session title (OpenCode's `New session - <timestamp>` placeholder is dropped), the tool of
  the open turn's newest `pending` or `running` tool part with a short target (first command line, file name,
  pattern, URL, task description) and the first `in_progress` todo as the open task.
- **Own task list.** The `todo` table, as `facts.tasks` (`{status, content}`, at most 20, in position order). Step 35
  turns it into imported todos.

## Settings (`[collectors.opencode]`)

`enabled`, `db_path`, `poll_interval_seconds`, `heartbeat_seconds`, `process_name`, `pgrep_command`, `lsof_command`,
`ps_command`, `command_timeout_seconds`, `busy_stale_seconds`, `inbox_poll_seconds`, `inbox_watch` (`auto` or `poll`).

## Health

`degraded` with an `error_kind` of: `pgrep_*` / `lsof_*` (command missing, failing or timing out), `db_missing`,
`db_unreadable`, `schema_mismatch` (a column the collector queries is gone; nothing is guessed). While the database
cannot be read the last listed state stays; with no OpenCode process the empty snapshot is still emitted.

## The optional TypeScript plugin

`fylgja.ts` ships as package data beside the collector. Copy it **by hand**; Fylgja installs nothing:

```sh
cp "$(uv run --frozen python -c 'import fylgja.collectors.opencode as m, pathlib; print(pathlib.Path(m.__file__).parent / "fylgja.ts")')" \
   ~/.config/opencode/plugins/fylgja.ts
```

(or copy it from `src/fylgja/collectors/opencode/fylgja.ts` in the repository). OpenCode loads it at the next start.

- It forwards `session.status`, `permission.asked` and `todo.updated` as `fylgja.hook/1` envelopes with
  `source: "opencode"` into `hook-inbox/opencode/` (`$FYLGJA_DATA_DIR`, default `~/.local/share/fylgja`), following the
  rules of `fylgja hook`: an `O_EXCL` temporary file with mode 0600, fsync, rename; an inbox with mode 0700 owned by the
  user (nothing is written to a symlink, a foreign or a wrongly moded directory); nothing while `.backlog-stamp` says
  `full`. It never throws, prints or uses the network.
- The payload has `session_id`, `hook_event_name`, `cwd` and, per event, `reason` (the status type: `busy`, `idle`,
  `retry`) or `tool_name` (the permission). No prompt, reply, todo text or tool input leaves OpenCode, and the only
  environment variables copied are the terminal identifiers of `docs/api/inbox-envelope.md`.
- The collector drains the inbox with the Claude collector's `InboxDrainer` and treats each committed envelope as a
  wake-up: it polls the database **at once** instead of after the interval. The database stays the source of truth
  for state; the events are stored as `hook_event` records and do not drive the session state machine
  (`docs/decisions/0036-opencode-collector-details.md`). Without the plugin the inbox stays empty and the state is at
  most one poll interval old.
- `opencode --pure` runs without external plugins. The current documentation names the permission event
  `permission.asked`; the older SDK types name it `permission.updated`, which the plugin does not forward.

`tests/fixtures/envelope/opencode-session-status.json` is the plugin's golden envelope
(`scripts/gen_opencode_envelope.py` regenerates it with `bun`).

## Tests

`tests/unit/test_opencode_parse.py` (rows, turn state, join and the read-only adapter over a database built from
`tests/fixtures/opencode/`), `tests/unit/test_opencode_collector.py`, `tests/contract/test_opencode_contract.py`,
`tests/unit/test_opencode_envelope_compat.py` (also runs the plugin when `bun` is on `PATH`) and the OpenCode goldens in
`tests/unit/test_one_liner.py`. The opt-in smoke test is
`FYLGJA_RUN_LIVE=1 uv run --frozen pytest -m live tests/live/test_opencode_live.py`.
