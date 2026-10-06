# OpenCode collector fixtures

Everything here is synthetic or sanitized (`docs/revalidation/2026-10-06-opencode.md`). Paths use the placeholders
`/work/example/…`; session, message and part ids are made up.

| File | What it is |
|---|---|
| `opencode.schema.sql` | The DDL of `project`, `session`, `message`, `part` and `todo` in `~/.local/share/opencode/opencode.db` on OpenCode 1.18.26 (`sqlite3 -readonly .schema <table>`), schema only |
| `opencode.rows.json` | Synthetic rows for those tables. `NOW_MS` is the instant the test clock is set to; every other time is relative to it. A `data` value that is an object or a list is stored as JSON text |
| `process/pgrep.txt` | `pgrep -x opencode` output |
| `process/lsof-fields.txt` | `lsof -nP -a -p … -d ^txt,^mem -Fpfn` output: `p` pid, `f` fd, `n` name |
| `process/ps-table.txt` | `ps -A -o pid=,ppid=,lstart=,comm=` output |

The sessions cover what the collector must tell apart:

| Session | Directory | Case |
|---|---|---|
| `…01` | `api-service` | busy: the newest assistant message is incomplete, a `bash` tool part is running; three todos, one `in_progress` |
| `…02` | `web-app` | idle: the assistant message is complete |
| `…03` | `api-service` | an older session in the same directory (loses to `…01`) |
| `…04` | `api-service` | archived and newer than `…01` (never chosen) |
| `…05` | `api-service` | a sub-agent session (`parent_id` set) with an incomplete assistant message (never chosen) |
| `…06` | `docs` | an incomplete assistant message three days old: stale, so idle |
| `…07` | `scratch` | OpenCode's placeholder title and no messages |
| `…08` | `errored` | an incomplete assistant message that carries an `error`: not busy |

Message, part and todo text holds the canary `CANARY-OPENCODE-TEXT`; a test asserts it never reaches a record.
No database file is committed: tests build `opencode.db` under `target/tests/` from the DDL and the rows
(`tests/opencode_support.py`).
