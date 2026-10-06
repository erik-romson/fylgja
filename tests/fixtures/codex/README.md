# Codex collector fixtures

Everything here is synthetic or sanitized (`docs/revalidation/2026-10-06-codex.md`). Paths use the placeholders
`/work/example/…`; thread ids are made up.

| File | What it is |
|---|---|
| `state_5.schema.sql` | The DDL of `~/.codex/state_5.sqlite` on Codex 0.154.0 (`sqlite3 -readonly .schema`), schema only |
| `state_5.rows.json` | Sanitized rows of `threads` (six, one archived) and `thread_spawn_edges`; `{rollouts}` in `rollout_path` stands for the rollout directory of the test |
| `rollouts/*.jsonl` | Rollout files in the shapes Codex writes: `idle`, `busy-tool` (pending `exec_command`), `busy-patch` (pending `apply_patch`, legacy `user_message`), `aborted` (`turn_aborted`), `fresh` (no turn yet), `torn` (garbage lines and a torn last line) |
| `process/pgrep.txt` | `pgrep -x codex` output |
| `process/lsof-fields.txt` | `lsof -nP -a -p … -d ^txt,^mem -Fpfn` output: `p` pid, `f` fd, `n` name |
| `process/ps-table.txt` | `ps -A -o pid=,ppid=,lstart=,comm=` output |

No database file is committed: tests build `state_5.sqlite` under `target/tests/` from the DDL and the rows
(`tests/codex_support.py`).
